# Sites: cameras at other locations

An OpenNVR in the cloud, or at head office, can record and show the cameras of other places: a shop, a branch, a warehouse, each behind its own router. At each place, a small program on a computer there, the **site agent**, sends that place's cameras to the server.

- **Nothing is opened at the site.** The agent only connects out, to the server on port 443, the same as a web browser. No port forwarding, no VPN, and the server never connects into the site.
- **Any computer on the cameras' network will do:** a Raspberry Pi, a small Linux box, or a Windows PC. It must stay switched on.
- **Cameras work like local ones:** live view, recording, playback and detection, all at full quality. Video is passed on as it is, never re-encoded.

```
the site's network                          internet              OpenNVR server
camera ──▶ site agent ──── 443, outgoing only ────▶ nginx ──▶ recording, live view
                     └──── 443, control (HTTPS) ──▶ core
```

This page is for whoever runs the OpenNVR server. [SITE_SETUP_AT_THE_SITE.md](SITE_SETUP_AT_THE_SITE.md) is the one page for the person at the site. [SITE_SECURITY.md](SITE_SECURITY.md) says what the agent trusts, what a compromised server can and cannot do, and what is left.

## Before you start: the server

- **Reachable on 443 from the sites, at a DNS name, with a publicly trusted certificate.** Set `PUBLIC_HOSTNAME` in `.env`, which gets a Let's Encrypt certificate (see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)). With that, sites are set up with one link. A self-signed certificate works too, with a short code and a fingerprint instead ([Set up a site with a code](#set-up-a-site-with-a-code)).
- **Nothing in front of the server that ends TLS.** A proxy or load balancer in front of OpenNVR must pass TLS through to it: the video route needs OpenNVR's own nginx to see the connection.
- **The default `NGINX_SINGLE_PORT_WEBRTC=on`.** The 443 layer carries the video route.

## Set up a site

1. **Administration → Sites → Add site.** Give it a name ("Shop 12, MG Road").
2. **Send the setup link** to the person at the site (Copy, Share, or the QR code). It works once, for 3 days by default (1 hour to 7 days).
3. **The person at the site opens it** on a computer there, and follows the page:

   | The site's computer | What they do |
   |---|---|
   | **Linux or Raspberry Pi** (Ubuntu, Debian, Raspberry Pi OS 32/64-bit) | Paste one line into a terminal, then paste the setup code. It installs the agent as a service that starts again after a reboot, and says when it is done ("3 cameras answered on this network"). |
   | **Windows** | Download the agent, then in PowerShell run `.\opennvr-host.exe setup` (paste the code) and `.\opennvr-host.exe site`. A one-click Windows installer is coming. |
   | **macOS** | The same in Terminal: `./opennvr-host setup`, then `./opennvr-host site`. |
   | **A box supplied by us** | Plug in power and network. *Coming: see the plan.* |

4. **The site turns Online** on the Sites page, usually within a minute. Then [add its cameras](#add-the-sites-cameras).

The setup page shows nothing secret on screen and sends nothing secret to the server. The agent checks it is talking to this server before it sends the code ([SITE_SECURITY.md](SITE_SECURITY.md#setup-links)).

## Add the site's cameras

On the site's card, click **Add cameras**:

1. **The cameras the agent found are listed.** *Scan the network* finds more, such as cameras with ONVIF switched off, and NVRs. It looks only at camera ports, on the networks the site's computer is on, and lists only what answers like a camera: no routers, NAS boxes or printers.
2. **Type the cameras' user and password once and click Check.** Each ticked device then shows one of:
   - **found:** with a picture, its resolution, and whether it has a sub stream. An NVR shows one row per channel;
   - **wrong password**;
   - **no stream found:** add it by its stream URL instead;
   - **not reachable.**
3. **Untick or rename what you don't want, then click Add N cameras.** They start within seconds.

Before you add, the dialog compares what the cameras would send with what the site can upload. When it won't fit, **Use sub streams only** switches the site to the cameras' smaller streams in one click (see [Upload](#upload)). If no test has run yet, **Test the uplink** measures it first.

For a camera no check finds, **Add one by its stream URL** at the bottom of the dialog takes its stream URL as the camera serves it on the site's network (`rtsp://192.168.1.64:554/…`, without the password), plus its user and password.

The password goes only to the devices you tick, through the site's agent, and each check is in the audit log with the devices it went to. A camera then appears on the Cameras page as **Site · \<its address at the site\>** and is recorded, shown and analysed like any other.

## Day to day

### The site's card

- **Status:** Online, Offline (the agent has not checked in for a minute), Waiting for agent, or Revoked.
- **Each camera:** live, or what is wrong in plain words. For example: "the camera refused its user and password", "the site's agent cannot reach the camera", "the camera stopped sending", "paused: the site is over its uplink cap", or "not relayed" (with the reason).
- **The site's computer:** a warning when its clock is more than 2 minutes off (recordings would carry the wrong time), when it is hot (80 °C or more), or when its disk is nearly full.
- **↑ kbps:** what the site sends now, and per camera.

### Alerts

- **A site offline for 5 minutes, or a camera down for 10 minutes** (with the reason), becomes an alert like a full disk.
  - It is in the system events and shown live on the dashboard, once when it starts and once when it is over.
  - While a whole site is offline, only the site's alert is raised.
- **The gear on the site's card** turns either alert off for that site.
- **Not yet:** these alerts are not sent by e-mail or Telegram.

### Run checks

**Run checks** asks the site's agent to test, from the site: the server's name, the server (and that its certificate is the one it trusts, which catches a firewall that inspects HTTPS), the video route, and each camera with its password. The answers appear within seconds. Look here first before asking anyone at the site to check something.

### Upload

Every camera sends its stream all the time, because recording needs it, so the site's upload speed limits how many cameras it can have. **Uplink** on the site's card sets:

- **What the site sends.**
  - *Main and sub streams* (the default): full quality.
  - *Sub streams only:* each camera's smaller stream, for live view, recording and detection alike, at sub-stream quality. Cameras without a sub stream keep sending their main one.
- **An uplink cap** in kbps.
  - Over it, the agent pauses main streams, newest camera first; a paused camera shows **paused (cap)** and records nothing meanwhile.
  - With room again they come back one at a time.
  - Sub streams are never paused.
- **Test the uplink:** the agent uploads throwaway data to this server for 5 seconds (at most 10 MB, at most once an hour), and the server measures how much upload is left beside what the site already sends.

Rough needs per camera: 1080p 4 Mbit/s, 4 MP 6 Mbit/s, 4K 10 Mbit/s, a sub stream about 0.5 Mbit/s.

### Camera passwords

After the password was changed on the cameras, click **Camera password** on the site's card and give the new one, for all its cameras or the ticked ones. The agent reconnects them within seconds.

### Updates

On Linux the agent keeps itself up to date with the agent builds this server ships, **and only with builds the OpenNVR release signed**. It checks about every 6 hours. A root updater installs the new build and puts the old one back if the new one does not reach the server within 2 minutes. The gear on the site's card turns this off for a site. Details: [SITE_SECURITY.md](SITE_SECURITY.md#updates), and [The signing keys](#the-signing-keys).

### Replace, revoke, remove

- **A new computer at the site** (the old one broke, a reinstall): click **Set up agent** on the site's card and send the new link.
  - The old agent stops working the moment the new one uses the link.
  - While the site has cameras, the new agent gets none of them (nor their passwords) until you click **Approve** on the card, which shows the new agent's machine and address.
  - If you don't recognise it, click **Not mine: revoke**.
- **Revoke** cuts the agent off at once and ends its streams. Its cameras and recordings stay, and a new setup link lets an agent back.
- **Unlink** stops one camera being relayed; the camera and its recordings stay. A site with no cameras left can be deleted.
- **On the site's computer**, `sudo ./opennvr-host uninstall` removes the agent, its service and its credential.

## Limits

- **The upload carries every camera all the time.** Use sub streams where the upload is thin.
- **If the site's internet drops, nothing is recorded** until it comes back. A local buffer at the site is planned.
- **PTZ, camera settings and ONVIF events work through the agent.** They have not been tried against many real cameras yet.
- **Vendor event streams are not relayed:** Hikvision alertStream and Dahua eventManager. Use the camera's ONVIF events or OpenNVR's own detection.
- **A camera whose control address differs from its stream's** (an NVR channel, an encoder) needs **Control address** set when adding it by URL.
- **HTTPS-inspecting firewalls block the agent:** the site shows offline, and Run checks (if the agent can reach the server at all) says why. Ask the site's IT to exempt the OpenNVR server's name from HTTPS inspection.
- **A site whose only way out is an HTTP proxy is not supported yet.**

## Advanced

### Set up a site with a code

For a server without a publicly trusted certificate, or behind a front proxy that ends TLS. The setup dialog says why a link can't be offered, and shows this instead.

1. **Add site** (or **Set up agent**) gives a one-time code (15 minutes, single use) and three commands per system: download the agent, enrol it, start it. On Linux or macOS:

   ```sh
   (curl -fso opennvr-host "https://nvr.example.com/api/v1/site-agent/download/$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/;s/armv7l/arm/')" \
     || curl -fsSko opennvr-host --pinnedpubkey "sha256//…" "…same URL…") && chmod +x opennvr-host
   ./opennvr-host enrol -pin sha256/… https://nvr.example.com ABCDE-FGHJK
   sudo ./opennvr-host install-service
   ```

2. **The `-pin`** is the server certificate's fingerprint, as the Sites page shows it.
   - With a self-signed certificate the download accepts the server only if its key matches the pin; curl checks `--pinnedpubkey` even with `-k`.
   - Without `-pin`, `enrol` refuses a self-signed server and prints the fingerprint it saw. Compare that with the Sites page.
3. **On Windows**, start `opennvr-host site` at startup as SYSTEM with a scheduled task, with the config under `C:\ProgramData\opennvr-site\config.json` (`enrol -config` and `site -config`).

### Getting the agent

- **From your own server.** It serves the builds it ships at `/api/v1/site-agent/download/<os>-<arch>`, so the agent always matches the server's version:
  - `<os>-<arch>` is `linux-amd64`, `linux-arm64`, `linux-arm` (32-bit Raspberry Pi OS), `darwin-amd64`, `darwin-arm64`, `windows-amd64` or `windows-arm64`;
  - `GET /api/v1/site-agent/download` lists them with their SHA-256;
  - on Windows the download is a console build, so it prints its results in the terminal.
- **From GitHub.** Every [GitHub release](https://github.com/open-nvr/open-nvr/releases) carries the same builds as `opennvr-host-<os>-<arch>`, with `SHA256SUMS` and the signed update manifest. Check one with `gh attestation verify opennvr-host-linux-amd64 -R open-nvr/open-nvr`, and use the release that matches your server's version.

### The installed service (Linux)

`setup` (as root) and `install-service` put:

| | |
|---|---|
| `/opt/opennvr-site/bin/opennvr-host` | the agent, root-owned; the service cannot change it |
| `/etc/opennvr-site/config.json` | the site's credential, `0600` root; systemd hands the service a private read-only copy |
| `/etc/systemd/system/opennvr-site.service` | the agent's service ([host-agent/packaging/opennvr-site.service](../host-agent/packaging/opennvr-site.service)) |
| `/etc/systemd/system/opennvr-site-update.{path,service}` | the updater, which installs signed updates the agent staged |
| `/var/lib/opennvr-site/` | the only place the agent may write |

- **The agent's account:** a throwaway user with no privileges.
- **Logs:** `journalctl -u opennvr-site -f`, and `journalctl -u opennvr-site-update` for updates.
- **Running setup again** replaces the agent, the credential and the services.
- **One agent per site:** a second one on the same machine (one started by hand next to the service, say) refuses to start.

### Cameras on another network, or an unusual port

The agent only connects to cameras on the networks its computer is directly on, and on usual camera ports. The server cannot change that ([SITE_SECURITY.md](SITE_SECURITY.md#what-the-agent-connects-to)). For a routed camera VLAN, or a camera on an unusual port, allow it **on the site's computer**, in the agent's config (`/etc/opennvr-site/config.json` for the service), then restart the agent:

```json
"allow_subnets": ["192.168.20.0/24"],
"allow_ports": [9000]
```

- **A subnet listed there still keeps its router out.**
- **A single address listed there** (`"192.168.1.1"`) is let through even if it is a router or this machine's own address: for an NVR that is also the camera network's router, or a test camera on the box itself.

### The signing keys

For whoever releases OpenNVR. Agents update themselves only to builds signed with a key compiled into them, and while `host-agent/internal/update/keys.go` lists none, they don't update at all. There are two key pairs:
- **the release key**, which CI signs with;
- **a standby key**, kept offline, to replace the release key if it is lost or leaks.

To set them up:
1. **On a trusted machine,** run `go run ./cmd/opennvr-sign keygen` (in `host-agent/`) twice. Each run prints a private key and its public key.
2. **Store the private keys.**
   - Put the release private key in the repository's Actions secrets as `AGENT_SIGNING_KEY`.
   - Keep both private keys offline, for example an encrypted file plus a password manager entry, and nowhere else.
3. **Commit both public keys** to `trustedKeysB64` in `host-agent/internal/update/keys.go`.

**If the release key leaks:**
1. Make a new pair.
2. List the new public key and the standby key in `keys.go`, and drop the leaked one.
3. Sign that one release with the standby key: set `AGENT_SIGNING_KEY` to the standby key for it.
4. Agents take that release, and trust the new key from then on.

Ship it quickly: agents that never got it still trust the leaked key. Builds from a fork or a local build are unsigned, and agents never take them. A fork can build its agents with its own key: `-ldflags "-X github.com/open-nvr/open-nvr/host-agent/internal/update.buildKeysB64=<public key>"`.

### How video and control travel

- **Video:**
  - The agent's built-in relay takes the RTP packets of each camera's first video track and passes them on unchanged, so any codec works (H.264, H.265, MJPEG…). Audio is not sent.
  - The packets go over TLS to port 443 with the ALPN `opennvr-rtsps`; nginx ends TLS and hands them to MediaMTX.
  - Each stream needs a short-lived token for that one camera.
  - If the built-in relay keeps failing on a camera and ffmpeg is installed, that camera uses ffmpeg (`-c copy`) for 15 minutes, and the card shows **via ffmpeg**.
- **Control:** HTTPS to `/api/v1/site-agent` with the site's credential, as a long poll, so a change on the Sites page reaches the agent within a second.
- **Camera control** (ONVIF, PTZ, settings): core's requests for a site camera ride the same long poll. The agent sends them on the site's network and returns the answers; core keeps all the protocol logic.
