# Sites: cameras at other locations

An OpenNVR in the cloud, or at head office, can record and show cameras that sit at other sites (a shop, a branch, a warehouse) behind their own router. A small **site agent** at each site relays its cameras to the server. The agent only dials out, on port 443, so nothing is forwarded at the site and the server never connects into it.

```
site LAN                                     internet               OpenNVR server
camera ──RTSP──▶ opennvr-host site ──TLS 443 (ALPN opennvr-rtsps)──▶ nginx ──▶ MediaMTX
                       │         ──HTTPS 443 /api/v1/site-agent ──▶ core
```

- **Video** is relayed as it is. The agent's built-in relay takes the RTP packets of the camera's first video track and passes them on unchanged, without decoding or re-encoding, so any codec the camera sends (H.264, H.265, MJPEG...) works and a small box is enough. Audio is not sent.
  - **Nothing to install:** ffmpeg is not needed. If the built-in relay fails three times in a row on a camera before it goes live and ffmpeg is installed, that camera uses ffmpeg (`-c copy`) for 15 minutes, then the built-in relay again. The Sites page shows **via ffmpeg** for such a camera.
  - **Where it goes:** over TLS to port 443; nginx on the server ends TLS and hands the stream to MediaMTX.
  - **Credentials:** each stream needs a short-lived token for that one camera. The camera's password and the token stay in the agent's memory: they are on no command line.
- **Control** is HTTPS to `/api/v1/site-agent` with the site's credential. A camera added on the Sites page starts within a second.
- **Camera control** (ONVIF, PTZ, settings): core's HTTP requests for a site camera ride the same long poll; the agent sends them on the site's LAN and posts the answers back. Core keeps all the protocol logic.
- **Camera credentials** are stored encrypted on the server and sent only to that site's agent, over TLS. They never leave the agent's memory.

## What the server needs

- Reachable on **443** from the sites, at a DNS name (`PUBLIC_HOSTNAME` in `.env` gives it a Let's Encrypt certificate, see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)). A self-signed certificate works too: the agent pins it.
- The default `NGINX_SINGLE_PORT_WEBRTC=on` (the 443 stream layer carries the ingest route).

## Add a site with a setup link (servers with a public certificate)

When `PUBLIC_HOSTNAME` is set and this server has a publicly trusted certificate for it (Let's Encrypt, or the company's own public certificate), **Add site** also makes a **setup link**. Send it to the person at the site, by WhatsApp or e-mail, or show its QR code.

1. **They open the link.** The page needs no login. It shows what to download for their computer (Windows, Linux / Raspberry Pi, macOS) and a **setup code**.
2. **On Linux or a Raspberry Pi, they paste one line** into a terminal, which downloads the agent and runs `sudo ./opennvr-host setup`, and they paste the setup code when it asks. `setup` enrols the machine, installs the agent as a service (see [Keep it running](#keep-it-running)) and starts it. It then says what it found ("3 cameras answered on this network"). Nothing else needs to be done at the site.
3. **On Windows or macOS** (until their installers exist), they run `opennvr-host setup`, then `opennvr-host site`.

The site turns **Online**, and you add its cameras as below.

Nobody types a code against the clock or compares a fingerprint. In detail:

- **Lifetime.** A link works once, for 3 days by default (1 hour to 7 days, chosen in the dialog). It can be revoked from the site's card, and the card shows when a link was used, and from which machine and address.
- **One way in.** Using a link voids the site's other links and its short code.
- **The secret is in the part of the link after `#`.** Browsers never send that part to the server, so it is in no log. The page reads it, asks the server about the link by its id only, and removes it from the address bar.
- **The agent checks the server first.** Before it sends the secret, `setup` makes the server prove it made this link. The server answers with an HMAC over the agent's nonce and the certificate key the agent sees, keyed by a key derived from the secret. A firewall that inspects HTTPS, or anything else in the path, cannot make that proof. The agent then stops and sends nothing, and from then on it trusts that key only.
- **The code is never on the command line.** `setup` reads it from the terminal, so it stays out of shell history and process lists.
- **Only derived values are stored.** The server keeps a hash to enrol with, and the proof key encrypted with its vault key. The remaining risk: someone who has both read the database with that key *and* sits between an agent and the server could pose as the server to an agent that has not enrolled yet.

When the server cannot offer links, the dialog says why, and you use the code below:

- **No `PUBLIC_HOSTNAME`.**
- **A self-signed certificate.** The person at the site would have to click through a browser warning, and anyone in the path could read the link.
- **A front proxy that ends TLS.** The server can't prove the proxy's key.

**Replacing a site's agent** (a new box, a reinstall) with a link or a code, while the site has cameras: the new agent gets **no cameras, and none of their passwords**, until you click **Approve** on the site's card. The card shows the new agent's machine and address. If it is not yours, **Not mine: revoke**. A first agent on a new site needs no approval.

## Add a site with a code

1. **Administration → Sites → Add site.** Name it; you get a one-time code (15 minutes, single use) and the command to run at the site.
2. **At the site**, on any machine that can reach the cameras (a Linux box, a Raspberry Pi, a Windows PC, a Mac), run the three commands the dialog shows for that machine's OS: download the agent, enrol it, start it. On Linux or macOS they look like:

   ```sh
   (curl -fso opennvr-host "https://nvr.example.com/api/v1/site-agent/download/$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')" \
     || curl -fsSko opennvr-host --pinnedpubkey "sha256//…" "…same URL…") && chmod +x opennvr-host
   ./opennvr-host enrol -pin sha256/… https://nvr.example.com ABCDE-FGHJK
   ./opennvr-host site
   ```

   **Getting the agent.** The server serves the build it ships (`/api/v1/site-agent/download/<os>-<arch>`, `<os>` = `linux`, `darwin` or `windows`, `<arch>` = `amd64` or `arm64`; `GET /api/v1/site-agent/download` lists them with their SHA-256), so the agent always matches the server's version and the site needs nothing but the server. The download first trusts the normal certificate chain; if the server's certificate is self-signed, the second `curl` accepts it only if its key matches the pin (curl checks `--pinnedpubkey` even with `-k`). The dialog also has plain download links, to fetch the file in a browser and copy it over. On Windows the download is a console build, so `enrol` prints its result in the terminal.

   The same builds are attached to every [GitHub release](https://github.com/open-nvr/open-nvr/releases) (`opennvr-host-<os>-<arch>`, plus `SHA256SUMS`), with a build provenance attestation: `gh attestation verify opennvr-host-linux-amd64 -R open-nvr/open-nvr`. Use the release that matches your server's version.

   **Enrolling.** The `-pin` is the server certificate's fingerprint, as the Sites page shows it. If you leave it out and the server's certificate is self-signed, `enrol` refuses and prints the fingerprint it saw: compare it with the Sites page before using it.
3. **Keep it running** as a service, so it starts again after a reboot (see [Keep it running](#keep-it-running)): on Linux, `sudo ./opennvr-host install-service` after `enrol`. On Windows, a scheduled task that runs `opennvr-host site` at startup as SYSTEM, with the config under `C:\ProgramData\opennvr-site\config.json` (`enrol -config` and `site -config`).
4. Back on the Sites page the site turns **Online**. Add its cameras as described below.

## Add the site's cameras

On the site's card, click **Add cameras**:

1. **The cameras found are listed.**
   - **Already listed:** what the agent found by ONVIF discovery.
   - **Scan the network** looks for more, such as cameras with ONVIF switched off and NVRs. The agent tries camera ports only (RTSP 554 and 8554, and the Dahua, Xiongmai and Hikvision ports) on the networks its machine is on, at most a /24 each.
   - **What stays out:** only devices that answer like a camera are listed. Routers, NAS boxes and printers are not.
2. **Type the cameras' user and password once and click Check.** Each ticked device is tried through the agent:
   - **By ONVIF** (Digest or WS-Security). This gives its model, its channels (one for a camera, one per channel for an NVR), each channel's main and sub stream, and a picture.
   - **Otherwise by the usual RTSP stream paths** (Hikvision/Uniview, Dahua/CP Plus).

   Each device ends up as **found**, **wrong password**, **no stream found** or **not reachable**.
3. **Untick or rename what you don't want, then click Add N cameras.** They are added one after another and the agent starts relaying them within seconds.

**Add one by its stream URL**, at the bottom of the dialog, is for a camera that no check finds. You enter the stream URL (as the camera serves it on the site's network, without credentials), plus its user and password.

**Where the password goes:**
- Only to the devices you tick, never to everything on the network.
- Only to devices the agent itself found.
- Each check is in the audit log, with the devices it went to.

A camera then appears on the Cameras page as **Site · \<its LAN address\>** and is recorded, shown and analysed like any other camera.

## Keep it running

On Linux with systemd (Ubuntu, Debian, Raspberry Pi OS 32- or 64-bit), `sudo ./opennvr-host setup` installs the agent as a service by itself. For an agent enrolled with a code, use `sudo ./opennvr-host install-service [-config FILE]`. Either puts:

| | |
|---|---|
| `/opt/opennvr-site/bin/opennvr-host` | the agent, root-owned; the service cannot change it |
| `/etc/opennvr-site/config.json` | the site's credential, `0600` root; systemd hands the service a private read-only copy |
| `/etc/systemd/system/opennvr-site.service` | the service ([host-agent/packaging/opennvr-site.service](../host-agent/packaging/opennvr-site.service)) |
| `/var/lib/opennvr-site/` | the only place the service may write (the server keys it proved, `state.json`) |

The service runs as a throwaway user (`DynamicUser`) with no privileges. It can't write to the system, read home folders or load kernel modules. It may only open network connections:
- outgoing ones;
- a multicast socket for ONVIF discovery;
- netlink, to see which networks the machine is on. The agent only relays cameras on those networks.

Running setup again (a new link, a replaced box) replaces the agent, the credential and the service, and restarts it.

- **Logs:** `journalctl -u opennvr-site -f`.
- **Remove everything** (service, agent, credential, state): `sudo ./opennvr-host uninstall`. Revoke the site on the server too if it isn't going to be used again.
- **One agent per site** runs on a machine. A second one, for example started by hand next to the service, refuses to start, and `setup` warns about any it finds.

## Updates

On Linux, the installed agent keeps itself up to date with the builds its server ships, **only to builds the OpenNVR release signed.** A compromised server can't use updates to run its own code on the site's machine.

1. **The release signs.** The workflow that builds the core image signs a manifest of the agent builds: version, build time, a hash of the agent's sources, and the SHA-256 of each file. It uses an ed25519 key that only that workflow holds (`AGENT_SIGNING_KEY`). The server relays the manifest and its signature (`GET /api/v1/site-agent/update`); it can't sign one.
2. **The agent checks, about every 6 hours** (the first time 2 minutes after it starts). It takes a build only if all of these hold:
   - the signature verifies with a key compiled into the agent;
   - it is newer than itself: built later, its version not lower, and not below the manifest's own minimum. This means an old signed build can't be pushed back onto it;
   - its agent sources differ, so a core release that didn't change the agent downloads nothing;
   - the downloaded file's SHA-256 is the one listed for this machine.

   The agent runs unprivileged and can't replace itself. It leaves the checked build in its state folder.
3. **A root updater installs it.** `opennvr-site-update.path` sees the staged build and starts `opennvr-site-update.service`, which runs `opennvr-host apply-update`. The updater trusts nothing the agent staged:
   - it checks the signature again, with the keys of the installed agent;
   - it checks the rules again, and the hash of the exact bytes it installs;
   - it reads no file through a link.

   It then keeps the previous agent as `opennvr-host.prev`, installs the new one and restarts the service.
4. **A broken update is undone.** If the new agent hasn't reached the server within 2 minutes, the updater puts the previous one back. It notes the build in `/var/lib/opennvr-site-update/failed`, so the agent doesn't fetch that build again.

Logs: `journalctl -u opennvr-site-update`. Builds from a fork or a local build are unsigned, and agents never update themselves to them. A fork can build its agents with its own key (`-ldflags "-X github.com/open-nvr/open-nvr/host-agent/internal/update.buildKeysB64=<public key>"`).

### The signing keys (for whoever releases OpenNVR)

There are two key pairs:
- **The release key.** The workflow signs with it.
- **A standby key.** It stays offline and replaces the release key if that is lost or leaks.

Both public keys are compiled into every agent. While none are (`internal/update/keys.go` is empty), agents don't update themselves at all.

1. **On a trusted machine,** run `go run ./cmd/opennvr-sign keygen` (in `host-agent/`) twice. Each run prints a private key and its public key.
2. **Store the private keys.**
   - Put the release private key in the repository's Actions secrets as `AGENT_SIGNING_KEY`. A GitHub environment with required reviewers, if you use one, also works.
   - Keep both private keys offline: an encrypted file, and a password manager entry. Don't keep them anywhere else.
3. **Commit both public keys** to `trustedKeysB64` in `host-agent/internal/update/keys.go`.

**If the release key leaks:**
1. Make a new pair.
2. List the new public key and the standby key in `keys.go`, and drop the leaked one.
3. Sign that one release with the standby key, by setting `AGENT_SIGNING_KEY` to the standby key for it.
4. Agents take it and trust the new key from then on.

Agents that never got that release still trust the leaked key, so ship the release quickly.

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

  Camera streams must be `rtsp://` or `rtsps://`, and the fallback relay, ffmpeg, is held to network protocols only (`-protocol_whitelist`). A compromised server can therefore reach what looks like a camera on that site's camera network, and nothing else.
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
- **Two agents for one site** on one machine are refused: `opennvr-host site` takes a lock for its site, which holds across users and the service's private filesystem.

## Uplink

Each site card shows what the site sends upstream now (↑ kbps) and, per camera, what each stream costs. **Uplink** sets two things:

- **What the site sends.** *Main and sub streams* (the default) sends both, at full quality. *Sub streams only* sends each camera's sub stream into its main path: live view, recording and detection all work, at sub-stream quality. Cameras with no sub stream keep sending their main stream.
- **Uplink cap** in kbps. Over it, the agent pauses main streams, newest camera first, until the total fits; a paused camera shows **paused (cap)** and records nothing meanwhile. With room again (a camera went quiet, the cap was raised), paused streams come back one at a time, each after at least a minute. Sub streams are never paused: they keep every camera visible.

A main stream on demand only while someone watches it is not offered: recording needs the main stream all the time.

## Limits

- The site's uplink carries every relayed stream all the time (recording needs the main stream). Add a sub stream for each camera where you can.
- PTZ and the camera's settings work through the agent, and ONVIF (PullPoint) events take the same path, though they have not been tried against a real camera yet: core sends each HTTP request to the site's agent, which sends it to the camera (only to cameras of that site, within the agent's own rule above, no redirects) and returns the answer. Long-lived vendor event streams (Hikvision alertStream, Dahua eventManager) are not relayed; use the camera's ONVIF events or OpenNVR's own detection. If a camera's control API answers on another address than its stream (an NVR channel, an encoder), set **Control address** when adding it.
- If the uplink drops, nothing is recorded until it comes back.
