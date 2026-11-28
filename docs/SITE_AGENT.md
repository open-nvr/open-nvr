# Sites: cameras at other locations

An OpenNVR in the cloud, or at head office, can record and show cameras that sit at other sites (a shop, a branch, a warehouse) behind their own router. A small **site agent** at each site relays its cameras to the server. The agent only dials out, on port 443, so nothing is forwarded at the site and the server never connects into it.

```
site LAN                                     internet               OpenNVR server
camera ──RTSP──▶ opennvr-host site ──TLS 443 (ALPN opennvr-rtsps)──▶ nginx ──▶ MediaMTX
                       │         ──HTTPS 443 /api/v1/site-agent ──▶ core
```

- **Video** is relayed as it is: the agent copies each camera's stream (`-c copy`), it does not re-encode, so a small box is enough. It goes over TLS to port 443; nginx on the server ends TLS and hands the stream to MediaMTX. Each stream needs a short-lived token for that one camera.
- **Control** is HTTPS to `/api/v1/site-agent` with the site's credential. A camera added on the Sites page starts within a second.
- **Camera control** (ONVIF, PTZ, settings): core's HTTP requests for a site camera ride the same long poll; the agent sends them on the site's LAN and posts the answers back. Core keeps all the protocol logic.
- **Camera credentials** are stored encrypted on the server and sent only to that site's agent, over TLS. They never leave the agent's memory.

## What the server needs

- Reachable on **443** from the sites, at a DNS name (`PUBLIC_HOSTNAME` in `.env` gives it a Let's Encrypt certificate, see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)). A self-signed certificate works too: the agent pins it.
- The default `NGINX_SINGLE_PORT_WEBRTC=on` (the 443 stream layer carries the ingest route).

## Add a site

1. **Administration → Sites → Add site.** Name it; you get a one-time code (15 minutes, single use) and the command to run at the site.
2. **At the site**, on any machine that can reach the cameras (a Linux box, a Raspberry Pi, a Windows PC, a Mac), install `ffmpeg`, then run the three commands the dialog shows for that machine's OS: download the agent, enrol it, start it. On Linux or macOS they look like:

   ```sh
   (curl -fso opennvr-host "https://nvr.example.com/api/v1/site-agent/download/$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')" \
     || curl -fsSko opennvr-host --pinnedpubkey "sha256//…" "…same URL…") && chmod +x opennvr-host
   ./opennvr-host enrol -pin sha256/… https://nvr.example.com ABCDE-FGHJK
   ./opennvr-host site
   ```

   **Getting the agent.** The server serves the build it ships (`/api/v1/site-agent/download/<os>-<arch>`, `<os>` = `linux`, `darwin` or `windows`, `<arch>` = `amd64` or `arm64`; `GET /api/v1/site-agent/download` lists them with their SHA-256), so the agent always matches the server's version and the site needs nothing but the server. The download first trusts the normal certificate chain; if the server's certificate is self-signed, the second `curl` accepts it only if its key matches the pin (curl checks `--pinnedpubkey` even with `-k`). The dialog also has plain download links, to fetch the file in a browser and copy it over. On Windows the download is a console build, so `enrol` prints its result in the terminal.

   The same builds are attached to every [GitHub release](https://github.com/open-nvr/open-nvr/releases) (`opennvr-host-<os>-<arch>`, plus `SHA256SUMS`), with a build provenance attestation: `gh attestation verify opennvr-host-linux-amd64 -R open-nvr/open-nvr`. Use the release that matches your server's version.

   **Enrolling.** The `-pin` is the server certificate's fingerprint, as the Sites page shows it. If you leave it out and the server's certificate is self-signed, `enrol` refuses and prints the fingerprint it saw: compare it with the Sites page before using it.
3. **Keep it running** as a service, so it starts again after a reboot:
   - Linux (systemd): `host-agent/packaging/opennvr-site.service`, steps in its header. Logs: `journalctl -u opennvr-site -f`.
   - Windows: a scheduled task that runs `opennvr-host site` at startup as SYSTEM, with the config under `C:\ProgramData\opennvr-site\config.json` (`enrol -config` and `site -config`).
4. Back on the Sites page the site turns **Online**. Cameras the agent finds by ONVIF on the site's network are listed: click one, enter its stream URL (as the camera serves it on the site's LAN, without credentials) and its user and password. Or add one by hand with **Camera**.

The camera then appears on the Cameras page as **Site · \<its LAN address\>** and is recorded, shown and analysed like any other camera.

## Revoke, re-enrol, remove

- **Revoke** cuts the agent off at once and ends its live streams. The site's cameras and recordings stay.
- **Enrol code** gives a new one-time code: to move the agent to another machine, or to let a revoked site back. The old credential keeps working until the new code is used; then it stops at once, and the old agent's running streams are ended.
- **Unlink** stops relaying one camera (the camera and its recordings stay). A site with no cameras left can be deleted.

## Security

The agent sits on the customer's network and takes its instructions from the server, so it is built not to trust the server further than relaying that site's cameras.

- **The agent decides where it connects, not the server.** It only connects to:
  - addresses on a private network its machine is directly connected to (`10.x`, `172.16-31.x`, `192.168.x`, `100.64.x`, IPv6 ULA)
  - on usual camera ports (80, 443, 554, 8000, 8080, 8554, 37777, 34567 and a few more), or a port the camera announced itself in ONVIF discovery

  It never connects to:
  - its own machine
  - a router (any next hop in the machine's routing table; on Linux and Windows)
  - another network
  - the internet

  Camera streams must be `rtsp://` or `rtsps://`, and ffmpeg is held to network protocols only (`-protocol_whitelist`). A compromised server can therefore reach what looks like a camera on that site's camera network, and nothing else.
- **Cameras on another subnet** (a routed camera VLAN) or on an unusual port are allowed on the site machine itself, never from the server. Add them to the agent's config file (the one `enrol` wrote) and restart the agent:

  ```json
  "allow_subnets": ["192.168.20.0/24"],
  "allow_ports": [9000]
  ```

  A subnet listed there still keeps its router out. A single address listed there (`"192.168.1.1"`) is let through even if it is a router or this machine's own address: for an NVR that is also the camera network's router, or a camera simulator on the box itself. A stream the agent refuses shows **not relayed: …** on the Sites page with the reason.
- **The server is trusted by its key only.** `enrol` saves the key of the certificate it met, and from then on the agent trusts that key, not the machine's certificate store. When the server's key changes (a renewed certificate with a new key), the server proves to the agent that it holds the site's credential before the agent trusts the new key. The agent sends nothing secret before that proof.
- **HTTPS-inspecting firewalls.** Some offices replace certificates in their firewall; the machine then trusts that firewall's certificates. The agent refuses such a connection: it logs that the certificate changed and the server could not prove it, and the site goes offline. Ask the site's IT to exempt the OpenNVR server's name from HTTPS inspection.
- **Behind a front proxy** (a load balancer in front of OpenNVR) that ends TLS, the agent pins the proxy's key, and a renewal of the proxy's certificate with a new key cannot be proven. The site then stops until it is enrolled again. Video ingest needs the TLS connection to reach OpenNVR's own nginx anyway (see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)), so pass TLS through instead. Also add the proxy to `TRUSTED_PROXY_CIDRS`, so per-address rate limits see each site's own address.
- **Limits on the public routes.**
  - **Request sizes:** small request bodies (enrol 16 KB, reports 1 MB, camera answers 8 MB).
  - **Enrolment:** limited per address and in total.
  - **Per site:** limits for each kind of call.
  - **Long polls:** at most one held per site, and they hold no database connection while they wait.
- **Logs.** nginx's access log records paths without query strings, so the short `?jwt=` tokens of playback and HLS URLs are not written to it.
- **Two agents on one config** are refused: `opennvr-host site` takes a lock for its config file.

## Uplink

Each site card shows what the site sends upstream now (↑ kbps) and, per camera, what each stream costs. **Uplink** sets two things:

- **What the site sends.** *Main and sub streams* (the default) sends both, at full quality. *Sub streams only* sends each camera's sub stream into its main path: live view, recording and detection all work, at sub-stream quality. Cameras with no sub stream keep sending their main stream.
- **Uplink cap** in kbps. Over it, the agent pauses main streams, newest camera first, until the total fits; a paused camera shows **paused (cap)** and records nothing meanwhile. With room again (a camera went quiet, the cap was raised), paused streams come back one at a time, each after at least a minute. Sub streams are never paused: they keep every camera visible.

A main stream on demand only while someone watches it is not offered: recording needs the main stream all the time.

## Limits

- The site's uplink carries every relayed stream all the time (recording needs the main stream). Add a sub stream for each camera where you can.
- PTZ and the camera's settings work through the agent, and ONVIF (PullPoint) events take the same path, though they have not been tried against a real camera yet: core sends each HTTP request to the site's agent, which sends it to the camera (only to cameras of that site, within the agent's own rule above, no redirects) and returns the answer. Long-lived vendor event streams (Hikvision alertStream, Dahua eventManager) are not relayed; use the camera's ONVIF events or OpenNVR's own detection. If a camera's control API answers on another address than its stream (an NVR channel, an encoder), set **Control address** when adding it.
- If the uplink drops, nothing is recorded until it comes back.
