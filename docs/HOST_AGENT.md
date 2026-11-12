# The host agent

OpenNVR runs in Docker. Most of what it does needs nothing outside the
containers, but some things only the host machine itself can do:

- **USB webcams.** Docker Desktop (Windows and macOS) cannot see USB
  cameras. On Linux, passing devices into a container ties the stack to
  whichever webcam was plugged in when it started.
- **The machine's own numbers.** Inside Docker Desktop, OpenNVR sees Docker's
  Linux VM. It sees the VM's memory and CPUs, not the PC's, and it has no
  temperatures.

The host agent (`opennvr-host`) is one small program that runs on the host and
does these things. It is optional: without it, everything else works, and the
dashboard says its numbers are the container's.

## What it does

**Discovery on Linux needs no agent either:** `LAN_DISCOVERY=on` (the
installer's default on Linux) runs the agent's discovery code as the
`lan-probe` container on the host's network (`docker-compose.lan-discovery.yml`),
so Add Camera → Discover finds every ONVIF camera on the LAN by multicast
within seconds, with nothing installed on the host.

On Linux you do not need it for webcams: MediaMTX serves a USB webcam
itself when `WEBCAMS=on` (see [WEBCAM.md](WEBCAM.md)). The agent is for
Docker Desktop on macOS and Windows, which cannot see USB, and for the
host's own CPU, memory, disk and temperature readings anywhere.

- **Streams USB webcams.**
  - It finds the webcams plugged into the machine, every 10 seconds, so plugging and unplugging is noticed.
  - It streams the ones assigned to cameras into OpenNVR (ffmpeg, H.264, RTSPS to MediaMTX on `127.0.0.1:8322`).
  - It restarts a stream that drops.
  - See [WEBCAM.md](WEBCAM.md).
- **Finds ONVIF cameras with multicast.** Every minute, and at once when someone opens Add Camera → Discover, it sends a WS-Discovery probe to `239.255.255.250:3702` on each of the host's LAN interfaces.
  - Every camera on that network answers within a few seconds with its address, name and model. No subnet has to be configured.
  - OpenNVR can't do this from inside Docker, where multicast never reaches the LAN; without the agent it can only sweep a subnet address by address.
  - Multicast doesn't cross routers, so the dialog still runs that sweep beside it, for camera networks behind a router, and merges the two lists.
  - Printers and other devices that answer probes are left out: only answers that declare ONVIF video or serve an ONVIF device service count.
- **Reports host metrics:** CPU, memory, every disk, the free space where recordings are stored, and temperatures where the OS allows.
  - The dashboard's Host Resources widget then shows **host** instead of **container**.
  - CPU and memory alerts then use the machine's numbers.
  - Retention still acts on what OpenNVR itself sees of the recordings folder.

## What it can't do

The agent is built to be a narrow, one-way bridge:

- **It opens no port.** Every 5 seconds it calls OpenNVR's core directly on
  `http://127.0.0.1:8000` (`POST /api/v1/host-agent/sync`). It reports what it
  sees and gets back which webcam feeds which camera. Nothing can connect to it.
  The only socket it binds is the one a discovery round uses: a short-lived
  UDP port for the ~3 seconds the cameras' replies take. Windows Firewall's
  default rule lets replies to a multicast in during that window.
- **Core can't make it run anything.** The answer is data, not commands, and the agent acts only on webcams it found itself. ffmpeg is started with fixed arguments and no shell.
- **Its key works only on this machine.**
  - The key file is readable only by the user the agent runs as, and no container mounts it. Core gets only its SHA-256.
  - Core also refuses the agent's routes for any request that came through the web proxy. A copied key is useless from the network.
- **It never needs root to update.** After an OpenNVR update, `up` replaces the binary next to the running one. The agent sees that core is a newer build, exits, and is restarted on the new binary.

## Install, status, remove

**A new installation asks** ("Install the OpenNVR host agent?", default yes)
and installs it when OpenNVR first starts. On Linux that start asks for `sudo`
once, to register the service.

**An existing installation**, or one that said no, installs it with the
command below. In the OpenNVR folder:

| | Windows | Linux / macOS |
|---|---|---|
| Install | `.\start.ps1 host-agent install` | `./start.sh host-agent install` |
| Status and the last log lines | `.\start.ps1 host-agent status` | `./start.sh host-agent status` |
| Follow the log | `.\start.ps1 host-agent logs` | `./start.sh host-agent logs` |
| Remove | `.\start.ps1 host-agent uninstall` | `./start.sh host-agent uninstall` |

`install` does four things:
1. copies the agent for this machine out of the running OpenNVR core image;
2. creates its key;
3. starts it in the background;
4. sets `HOST_AGENT=on` in `.env`. From then on every `up` (and every update) keeps the agent at the same build as core.

| | Runs as | Starts | Needs |
|---|---|---|---|
| Windows | Scheduled Task "OpenNVR host agent", your user, your session | at logon | nothing; Windows shows the camera only to a logged-in user, so an NVR PC should sign in automatically |
| Linux | systemd service `opennvr-host`, your user, group `video` | at boot | `sudo` once to write the service |
| macOS | LaunchAgent `org.opennvr.host` | at login | camera permission, below |

### macOS

macOS asks before a program may use the camera, and a background agent can't
show that question. If USB webcams stay in an error state, run the agent once
from Terminal and allow it:

```bash
./bin/opennvr-host -log-file -      # allow camera access when asked, then Ctrl+C
```

Or add `bin/opennvr-host` under System Settings → Privacy & Security → Camera.

## Files

| Path | What |
|---|---|
| `bin/opennvr-host` (`.exe` on Windows) | the agent |
| `bin/opennvr-host.build` | its build, compared with core's on every `up` |
| `data/host-agent-key/agent.key` | its key (only the agent's user can read it) |
| `data/host-agent-key/agent.log` | its log (5 MB, one old copy kept) |
| `data/host-agent/agent.key.sha256` | the key's hash, mounted read-only into core |

To rotate the key, uninstall and install again.

## For developers

- **Source:** `host-agent/` (Go, no cgo), tested with `go test ./...`.
- **Build:** the core `Dockerfile` cross-compiles it for Windows, Linux and macOS (amd64 and arm64) into `/opt/opennvr-host/` in the core image, stamped with core's version and commit.
- **Core side:** `server/services/host_agent.py` and `server/routers/host_agent.py`.
