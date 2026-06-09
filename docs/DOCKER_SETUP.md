# OpenNVR - Docker Setup Guide

Complete guide for deploying OpenNVR using Docker containers.


---

## 🌐 Network Strategy: Windows/Mac vs Linux

OpenNVR requires specific network configurations depending on your operating system, specifically related to **ONVIF Camera Auto-discovery**. ONVIF relies on UDP Multicast packets which do not route through default Docker Bridges.

### Windows & macOS (BRIDGE MODE)
*This is the default configuration out-of-the-box.*
Docker on Windows/Mac runs inside a hidden VM. It **cannot** bind directly to your host's physical network card.
- **How it runs:** Services communicate via Docker's internal DNS (e.g., http://mediamtx:8889 or 	cp://db:5432). 
- **Limitation:** ONVIF Auto-discovery will NOT work. You must add cameras manually via their IP address.

### Linux (HOST MODE)
Because Linux runs Docker natively, containers can attach directly to your physical network interface, perfectly enabling UDP Multicast rules for fast ONVIF auto-discovery!
**To enable Host Mode on Linux:**
1. Open \docker-compose.yml\.
2. On **EVERY** service (\db\, \mediamtx\, \opennvr-core\, \i-adapters\):
   - Uncomment: etwork_mode: host   - Comment out: the entire \ports:\ array block.
   - Comment out: the entire etworks:\ array block.
3. Under the \opennvr-core\ environment section:
   - Comment out all the variables under \============ BRIDGE MODE ============   - Uncomment all the variables under \============ HOST MODE ============
This switches the internal routing from Docker's virtual DNS back to raw W.0.0.1\ binding.


---

## Prerequisites

- **Docker Desktop** installed and running
- **4GB RAM** minimum (8GB recommended)
- **10GB free disk space** (plus storage for recordings)
- **Windows 10/11**, **Ubuntu 22.04+**, or **macOS**

---

## Quick Start (2 Minutes!)

### 1. Clone Repository

```bash
git clone https://github.com/open-nvr/open-nvr.git
cd opennvr
```

### 2. Configure Environment (Optional)

**Use defaults** (recommended for first-time setup):
```bash
# Copy pre-configured environment file with working defaults
cp .env.docker .env
```

**Or customize** (if you want to change settings):
```bash
# Copy and edit the environment file
cp .env.docker .env

# Windows
notepad .env

# Linux/Mac
nano .env
```

The `.env.docker` file includes:
- ✅ **Pre-generated Fernet encryption key** (valid and tested)
- ✅ **Default database credentials** (change in production!)
- ✅ **Working MediaMTX webhook secret**
- ✅ **Cross-platform recording paths** (Windows/Linux/macOS)

**Important settings you may want to customize:**

```env
# Database password (recommended to change)
POSTGRES_PASSWORD=opennvr_secure_db_pass_2024

# Recording storage location
RECORDINGS_PATH=D:/Recordings                    # Windows
# RECORDINGS_PATH=/var/lib/opennvr/recordings      # Linux/Mac

# For production, also change:
# SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, MEDIAMTX_SECRET
```

### 3. Create Recordings Directory

```bash
# Windows PowerShell
New-Item -ItemType Directory -Force -Path "D:\Recordings"

# Linux/Mac
mkdir -p /var/lib/opennvr/recordings
```

### 4. Pull and Start Containers

```bash
# Pull latest images from Docker Hub
docker compose pull

# Start all services in background
docker compose up -d

# Check status
docker compose ps
```

**That's it!** 🎉 All services are now running.

### 5. Access Application

OpenNVR ships an nginx TLS reverse proxy (ISSUE-6) so the UI is reachable from any device on your LAN over HTTPS. Plain HTTP is never served — the `80 → 443` redirect ensures cleartext JWTs never traverse the network.

**From the host machine:** open <https://localhost/>

**From a phone, tablet, or laptop on the same LAN:** open `https://<server-ip>/` (e.g. `https://192.168.1.100/`). You can find the server's LAN IP with `hostname -I` on Linux or `ipconfig getifaddr en0` on macOS. The `./start.sh up` output prints it for you.

**About the browser warning.** On first visit the browser will warn that the certificate is not trusted. **This is expected.** OpenNVR generates a self-signed certificate on first boot and stores the keypair under `./nginx-certs/` on the host machine — it never leaves your network and is never committed to git. Click:

- **Chrome / Edge:** *Advanced → Proceed to &lt;host&gt; (unsafe)*
- **Firefox:** *Advanced → Accept the Risk and Continue*
- **Safari:** *Show Details → visit this website → Visit Website*

The warning sounds scary but the security model is straightforward: in a flat-LAN deployment with no public DNS or Let's Encrypt access, self-signed certs are the only way to encrypt the connection. The alternative is plaintext HTTP, which would leak your JWT auth tokens to anyone sniffing the LAN — strictly worse.

**Silence the CN/IP-mismatch warning (optional).** The default cert has Subject Alternative Names for `localhost`, `127.0.0.1`, `opennvr`, `opennvr.local`, and `::1`. If you access the UI by IP from the LAN, the browser will also warn that the hostname doesn't match the cert. To fix that:

```bash
# Add your server's LAN IP to .env
echo "OPENNVR_HOST_IP=192.168.1.100" >> .env

# Regenerate the cert
rm -rf ./nginx-certs/
./start.sh up
```

The CA-not-trusted warning will still appear (one-click bypass), but the CN/IP-mismatch error will be gone.

**Permanently trust the cert (optional).** For a cleaner UX, you can add `./nginx-certs/server.crt` to your operating system's trust store (or to your browser's certificate manager). After that, the browser shows the padlock with no warning at all.

**Default Credentials:** OpenNVR no longer ships with a default password. The admin account is created with `password_set=False` and gated by a **one-time setup token** that is minted at first boot and printed to stdout (and also surfaced by `./start.sh up`). Paste the token into the first-time-setup page along with your chosen admin password. The token is consumed on first successful use; restart opennvr-core to re-arm if you miss it.

⚠️ **Never expose plaintext HTTP (port 8000) to the LAN.** The compose intentionally binds opennvr-core to `127.0.0.1:8000:8000` so the only LAN-reachable surface is the TLS-terminating nginx on `:443`. Changing this defeats the security model — use the nginx proxy.

### 5a. NIC topology — single-NIC vs dual-NIC vs VLAN

OpenNVR's trust model assumes the *camera network* and the *operator network* can be told apart. The three deployment shapes:

**Shape 1 — Single-NIC (default, home / small office).** One Ethernet/WiFi interface on the host. Cameras and operator devices share the same `192.168.x.0/24`. nginx binds to `0.0.0.0:443` — the one and only NIC. `start.sh` auto-detects this and configures it. Trust model: "I trust my LAN as a whole." Sufficient for most home installs.

**Shape 2 — Dual-NIC (recommended for businesses and security-sensitive deployments).** Two physical interfaces. One feeds the camera network (no default route, DNS blackholed); the other is the operator uplink where users reach the UI. nginx binds *only* to the uplink NIC's IP, so a compromised camera physically cannot probe the UI or reach other devices on your LAN through this host. Declare the topology in `.env`:

```bash
CAMERA_NETWORK_INTERFACE=eth0
MGMT_NETWORK_INTERFACE=eth1
```

Find your interface names with `ip -4 -o addr show scope global` (Linux) or `ifconfig` (macOS). On next `./start.sh up`, the script binds nginx to the management NIC's IPv4 and prints which NIC is which. Refuses to boot if `MGMT_NETWORK_INTERFACE` has no IPv4 address (typo guard).

**Shape 3 — Single physical NIC with VLAN tagging (Shape 2 on one wire).** If you have a managed switch but only one Ethernet port on the host, 802.1Q VLAN tagging gives you the same isolation. The single physical NIC carries multiple tagged VLANs; the Linux kernel presents each as a separate interface:

```bash
# On the host (one-time setup; varies by distro / network manager):
ip link add link eth0 name eth0.10 type vlan id 10    # camera VLAN
ip link add link eth0 name eth0.20 type vlan id 20    # uplink VLAN
ip addr add 10.10.0.1/24 dev eth0.10
ip addr add 192.168.1.100/24 dev eth0.20
ip link set eth0.10 up
ip link set eth0.20 up
```

Then declare the same as Shape 2:

```bash
CAMERA_NETWORK_INTERFACE=eth0.10
MGMT_NETWORK_INTERFACE=eth0.20
```

From OpenNVR's perspective `eth0.10` and `eth0.20` are indistinguishable from two physical NICs; the isolation is enforced at L2 by the managed switch. Requires (a) a VLAN-aware switch and (b) the switch ports for cameras and operators configured for the correct VLAN IDs. Most consumer $30 switches can't do this; typical SMB managed switches (Netgear GS308E, TP-Link TL-SG108E, MikroTik, Ubiquiti) can.

**What to do if `start.sh` reports "multi-NIC, undeclared".** You have ≥2 NICs but haven't declared which is camera-LAN and which is uplink. nginx falls back to `0.0.0.0:443` (reachable from all NICs) with a loud warning. Either set the two env vars above, or accept that single-NIC trust applies and the warning is informational.

⚠️ **What auto-detection cannot do.** There's no universal way to tell which NIC is "the camera NIC" vs "the uplink NIC" automatically — both could be RFC1918, both could have default routes set incorrectly, and the kernel boot order may shuffle names (`eth0` may not be the first cable you plugged in). The operator must declare the topology; `start.sh` will refuse to assume.

### 5b. Interactive topology walkthrough (ISSUE-6 v3)

When `./start.sh up` runs against a multi-NIC host whose topology hasn't been declared in `.env` AND a terminal is attached (interactive run, not CI/scripted), the script presents a guided menu instead of just warning. Sample session:

```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NIC topology: I see 2 routable interfaces.
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Detected interfaces:
    1) eth0           192.168.1.100
    2) eth1           192.168.2.150

  How is your network set up?

    1) Simple — one network for cameras, phone, and computer.
       Most home / small-office setups.
       Tip: change every camera's default password before connecting.

    2) Advanced — cameras on a separate network from operators.
       Needs two network cables or a VLAN-aware managed switch.
       Stronger isolation if a camera gets hacked.

    3) Not sure — I'll pick the safe default (Simple) for you.

  Your choice [1/2/3]: 2
  Which number is the CAMERA-LAN side?     [1-2]: 1
  Which number is the OPERATOR-UPLINK side? [1-2]: 2

  ✓ Dual-NIC mode saved.
    camera network : eth0  (UI not exposed here)
    operator uplink: eth1 (192.168.2.150)  ← UI bound here

    Web UI: https://192.168.2.150/

  Apply host firewall rules to enforce the camera/uplink separation?
  This installs Linux firewall (nftables) rules that block IP
  forwarding between eth0 (cameras) and eth1 (uplink).
  Effect: a compromised camera cannot use this host as a
  stepping stone to reach your LAN. Requires sudo once.
  Every command is printed before running. Reverse with:
      ./scripts/revert-camera-vlan-hardening.sh

  Apply hardening now? [y/N]:
```

The choice persists to `.env` so subsequent runs skip the prompt. Selecting **s** writes `NGINX_BIND_HOST=0.0.0.0`; **d** writes `CAMERA_NETWORK_INTERFACE` + `MGMT_NETWORK_INTERFACE`; **l** writes nothing (you'll see the prompt again next boot). Same-NIC-for-both-sides is rejected with no `.env` write.

### 5c. Optional: enforce the camera/uplink separation with host firewall rules

Picking **dual-NIC** in the walkthrough surfaces a second prompt offering to install Linux firewall (nftables) rules that block IP forwarding between the camera network NIC and the uplink NIC. This is the only thing in OpenNVR's install path that asks for sudo, and it's gated behind explicit consent.

**What the hardening does.**

- Adds a dedicated nftables table `inet opennvr-vlan` with a single forward-chain rule that drops packets in both directions between the two declared NICs.
- A compromised camera on the camera-LAN cannot pivot through the OpenNVR host to reach LAN devices on the uplink side.
- An attacker on the LAN cannot reach cameras through the OpenNVR host either.

**What it doesn't do (intentionally).**

- Does not remove the default route from any NIC. Routing manipulation is a separate decision tracked under V-016.
- Does not modify your existing firewall (UFW, firewalld, fail2ban, bare iptables) — `inet opennvr-vlan` is an isolated table; removing it never touches anything else.
- Does not change DNS resolution. Operators with Pi-hole, AdGuard, dnsmasq, or systemd-resolved keep their current setup unchanged.
- Does not run input/output filtering on the NICs. This avoids the "I just SSH'd in via eth0 and you blocked my session" failure mode.

**Reversal is one command.**

```bash
./scripts/revert-camera-vlan-hardening.sh
```

The revert script lists the active rules, asks for confirmation, then runs `sudo nft delete table inet opennvr-vlan`. The rest of your firewall is untouched.

**Run it yourself later, outside the walkthrough.**

```bash
./scripts/apply-camera-vlan-hardening.sh \
    --camera-iface eth0 --mgmt-iface eth1 [--dry-run]
```

`--dry-run` prints the generated nftables ruleset and the exact commands that would execute, then exits without running them. Use it to audit before granting sudo.

**Snapshots.** Every apply creates `./host-hardening/snapshot-<timestamp>/` containing the pre-apply `nft list ruleset`, `ip route show`, and `ip -4 -o addr show` output. Survives across reverts so you can compare before/after.

### 5d. Live streams + recording playback from LAN devices

After `./start.sh up` finishes, live WebRTC and HLS streams, plus recording playback, work from any device on the same LAN — phones, tablets, laptops. No additional configuration needed: nginx proxies `/webrtc/`, `/hls/`, and `/playback/` to MediaMTX over the Docker bridge, and the WebRTC media UDP port (`:8189`) is published on the same NIC the UI binds to.

In dual-NIC mode the camera-LAN side stays isolated — every published port (UI 443, WebRTC 8189 UDP/TCP) binds only to the uplink NIC, so cameras on the camera-LAN cannot reach the management plane or the media plane.

### 5e. Refreshing the TLS cert when your IP changes

The self-signed cert generated on first boot includes your LAN IP in its SAN list. If your IP changes (DHCP renewal, you moved the box to a new network, you swapped NICs), the cert no longer matches and the browser shows an extra "CN/IP mismatch" warning. Fix:

```bash
./start.sh refresh-certs
```

This stops nginx + MediaMTX, deletes `./nginx-certs/` and `./mediamtx-certs/`, re-detects your current LAN IP, and brings the stack back up — the cert-init containers regenerate fresh certs with the new SAN. You'll need to accept the new cert in your browser once. Confirmation prompt appears on interactive shells; CI/scripted runs skip it.

---

## Container Architecture

The Docker Compose stack builds or pulls the following images:

| Service | Image | Purpose |
|---------|-------|---------|
| **opennvr_core** | Built locally from `Dockerfile` | FastAPI backend + React frontend + Kai-C AI |
| **opennvr_db** | `postgres:15-alpine` | PostgreSQL database |
| **opennvr_mediamtx** | Built from `bluenviron/mediamtx:1.15.4` + curl | Streaming server (RTSP/HLS/WebRTC) |
| **opennvr_ai** | Built locally from `../ai-adapter/Dockerfile` | AI inference engine with model adapters |

**Networks:**
- `sentinel_internal` - Internal communication between services
- `public_uplink` - AI adapters internet access for cloud inference

**Volumes:**
- `${RECORDINGS_PATH}:/app/recordings` - Recording storage (path from `.env`)
- `./mediamtx.docker.yml:/mediamtx.yml` - MediaMTX configuration for Docker
- `opennvr_db_data` - Database persistence
- `shared_frames` - AI frame processing between services

**Port Mappings:**
- `8000` - Web UI and API
- `8554` - RTSP streaming
- `8888` - HLS streaming
- `8889` - WebRTC streaming
- `9997` - MediaMTX Admin API
- `9996` - MediaMTX Playback API

**Security Architecture:**
- ✅ **No hardcoded secrets** - All secrets loaded from `.env` file
- ✅ **Official MediaMTX base** - Pinned to `bluenviron/mediamtx:1.15.4`, curl added via Alpine package manager
- ✅ **Configuration via environment variables** - `mediamtx.docker.yml` uses `${MEDIAMTX_SECRET}`, `${BACKEND_HOST}`, etc.
- ✅ **Single source of truth** - All configuration in `.env` file

---

## Verification

### Check Services Status

```bash
docker compose ps
```

Expected output:
```
NAME              IMAGE                          STATUS
opennvr_core      opennvr-opennvr-core           Up
opennvr_db        postgres:15-alpine             Up (healthy)
opennvr_mediamtx  opennvr-mediamtx               Up (healthy)
opennvr_ai        opennvr-ai-adapters            Up
```

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f opennvr-core
docker compose logs -f mediamtx
docker compose logs -f ai-adapters
```

### Test Streaming

1. Login to web UI
2. Add a camera (RTSP URL)
3. Enable streaming
4. Click "View Stream" - should see live video

### Test Recording

1. Enable recording for a camera
2. Wait 5 minutes
3. Check recording directory: `D:\Recordings\cam-1\`
4. Should see `.mp4` segment files

### Test AI Detection

1. Add AI model via UI
2. Enable inference for a camera
3. Check AI results: `http://localhost:8000/api/v1/ai-model-management/inference/running`

---

## Configuration

### Customizing .env File

All configuration is done in the `.env` file (copied from `.env.docker`). No need to edit `docker-compose.yml`!

**Common Customizations:**

**Database Configuration:**
```env
POSTGRES_USER=opennvr_user
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_DB=opennvr_db
```

**Backend Security Secrets:**
```env
SECRET_KEY=your_64_character_hex_secret_here
CREDENTIAL_ENCRYPTION_KEY=your_fernet_key_here==
INTERNAL_API_KEY=your_base64_api_key_here
MEDIAMTX_SECRET=your_mediamtx_webhook_secret_here
```

**MediaMTX External URL** (for remote access):
```env
MEDIAMTX_EXTERNAL_BASE_URL=http://192.168.1.100:8889
```
Change to your server's IP if accessing from other devices on the network.

**Recording Storage Path:**
```env
# Windows
RECORDINGS_PATH=D:/Recordings

# Linux/Mac
RECORDINGS_PATH=/var/lib/opennvr/recordings
```

**Optional Settings:**
```env
DEBUG=False
LOG_LEVEL=INFO
```

### Custom Recording Path

1. Create directory on host:
```bash
# Windows
New-Item -ItemType Directory -Force -Path "E:\MyRecordings"

# Linux/Mac
mkdir -p /mnt/storage/recordings
```

2. Update `.env`:
```env
RECORDINGS_PATH=E:/MyRecordings           # Windows
# RECORDINGS_PATH=/mnt/storage/recordings  # Linux
```

3. Restart services:
```bash
docker compose restart
```

### Network Configuration

To expose on local network (access from other devices), you can create a `.env.local` override or modify docker-compose.yml ports directly:

In `docker-compose.yml`, change:
```yaml
ports:
  - "0.0.0.0:8000:8000"  # Allow external access
```

⚠️ **Security Warning**: Only expose if behind a firewall!

---

## Maintenance

### Update to Latest Version

```bash
# Pull latest images
docker compose pull

# Restart with new images
docker compose up -d

# Verify
docker compose images
```

### Backup Database

```bash
# Create backup
docker exec opennvr_db pg_dump -U opennvr_user opennvr_db > backup.sql

# Restore backup
cat backup.sql | docker exec -i opennvr_db psql -U opennvr_user opennvr_db
```

### Backup Recordings

Simply copy the recordings directory:
```bash
cp -r D:/Recordings D:/Recordings-backup
```

### View Resource Usage

```bash
docker stats
```

### Restart Services

```bash
# All services
docker compose restart

# Single service
docker compose restart opennvr-core
```

---

## Troubleshooting

### "Cannot find .env file" or "Missing required environment variables"

**Cause**: `.env` file not created

**Fix**:
```bash
# Copy the default environment file
cp .env.docker .env

# Restart services
docker compose up -d
```

### "Cannot connect to database"

**Check database health:**
```bash
docker compose ps db
docker compose logs db | grep "ready to accept connections"
```

**Fix**: Wait 30 seconds and retry, or restart database:
```bash
docker compose restart db
```

### "Login Failed" or "500 Internal Server Error"

**Check backend logs:**
```bash
docker compose logs opennvr-core | grep -i error
```

**Common causes:**
- Database migration needed (automatic on first start)
- Invalid SECRET_KEY (regenerate and restart)

### Stream Not Loading

**Check MediaMTX:**
```bash
docker compose logs mediamtx | grep -i error
```

**Verify camera connectivity:**
```bash
docker exec opennvr_core curl http://mediamtx:9997/v3/config/paths/get/cam-1
```

### AI Inference Not Working

**Check frames directory permissions:**
```bash
docker exec opennvr_core ls -la /app/AI-adapters/AIAdapters/frames/
```

**Check AI adapter logs:**
```bash
docker compose logs ai-adapters
```

### Port Already in Use

**Change port in docker-compose.yml:**
```yaml
ports:
  - "8001:8000"  # Use 8001 instead of 8000
```

Then access at `http://localhost:8001`

### Permission Denied (Linux)

```bash
sudo chown -R $USER:$USER Recordings
chmod -R 755 Recordings
```

---

## Stopping and Cleanup

### Stop Services

```bash
# Stop all containers (data preserved)
docker compose down

# Stop and remove volumes (⚠️ DELETES ALL DATA!)
docker compose down -v
```

### Complete Uninstallation

```bash
# Stop and remove containers
docker compose down -v

# Remove images
docker rmi opennvr-opennvr-core
docker rmi opennvr-mediamtx
docker rmi opennvr-ai-adapters
docker rmi bluenviron/mediamtx:1.15.4
docker rmi postgres:15-alpine

# Remove project directory
cd ..
rm -rf opennvr
```

---

## Security Checklist

Before exposing to any network:

- [ ] Copied `.env.docker` to `.env`
- [ ] Changed database password in `.env` (POSTGRES_PASSWORD)
- [ ] Generated unique secrets in `.env` (SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY)
- [ ] Changed admin password via web UI
- [ ] Set `DEBUG=False` in `.env`
- [ ] Verified all ports bound to `127.0.0.1` (localhost only) in docker-compose.yml
- [ ] Enabled firewall on host system
- [ ] Regular backups configured

---

## Advanced Configuration

### Custom Docker Network

```bash
# Create external network
docker network create opennvr_network

# Update docker-compose.yml
networks:
  external_network:
    external: true
    name: opennvr_network
```

### Resource Limits

Add to service in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      cpus: '2'
      memory: 4G
    reservations:
      memory: 2G
```

### Health Checks

Already configured for database. Add for other services:
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
  interval: 30s
  timeout: 10s
  retries: 3
```

---

## Getting Help

- **Issues**: https://github.com/open-nvr/open-nvr/issues
- **Logs**: Always include output of `docker compose logs` when reporting issues
- **System Info**: Include OS, Docker version (`docker --version`), and compose version

---

**Last Updated**: February 2026
