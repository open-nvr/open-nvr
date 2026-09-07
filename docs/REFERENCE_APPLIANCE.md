# Reference appliance

A known-good shape for an OpenNVR site: the hardware, the network, the
storage arithmetic and the hardening checklist an operator follows to
get a deployment whose posture the
[evidence pack](ENTERPRISE.md#the-evidence-pack) reports as all-pass.
Nothing here is required — OpenNVR runs on a laptop — but a regulated
site that follows this page can say *why* it looks the way it does, and
a support engagement starts from it.

## Three sizes

| | **S — edge** | **M — site** | **L — campus** |
|---|---|---|---|
| Cameras (Tier-0 detection on all) | up to 8 | up to 32 | up to 96 per node |
| CPU | 4-core ARM64 or x86-64 (Raspberry Pi 5 8 GB, Intel N100) | 8-core x86-64 with an iGPU (Intel Core i5/i7 12th gen+, AMD Ryzen 5/7) | 16-core x86-64, ECC RAM |
| Decode | Hardware: `DETECT_HWACCEL=rpi` / `vaapi` / `qsv` | Hardware: `vaapi` / `qsv` | Hardware: `vaapi` / `qsv` / `nvidia` |
| Inference | Tier-0 on CPU (substream, `DETECT_FPS=2`) | Tier-0 on CPU or iGPU (OpenVINO); one adapter GPU optional | NVIDIA GPU, 8 GB+ VRAM, for adapters (plates, faces, VLM) |
| RAM | 8 GB | 32 GB | 64 GB+ |
| System disk | 64 GB NVMe / eMMC | 256 GB NVMe | 512 GB NVMe (mirrored) |
| Recording storage | 1–2 TB (USB/SATA SSD) | 8–16 TB (2× SATA/NVMe, mirrored) | 32 TB+ (RAID-6 / ZFS raidz2, or NAS over the storage VLAN) |
| Network | 2× 1 GbE (camera + management) | 2× 2.5 GbE or 1× 10 GbE + 1 GbE | 2× 10 GbE (camera + management), 10 GbE storage |
| Power | 15–25 W | 45–90 W | 200–400 W (GPU) |

The numbers are for Tier-0 detection on every camera at 2 fps on the
substream — the default, CPU-honest configuration
([DETECT_CPU.md](DETECT_CPU.md)). Raising `DETECT_FPS`, analysing main
streams, or adding face/plate/VLM adapters moves a site up a size.
Decode dominates: an iGPU or a hardware decoder is worth more than
extra cores.

## Storage arithmetic

Recording is continuous by default (one segment per hour,
`RECORDING_SEGMENT_SECONDS=3600`). Per camera:

```
GB per day  ≈  bitrate (Mbit/s) × 10.8
```

| Stream | Typical bitrate | GB / day | 30 days | 90 days |
|---|---|---|---|---|
| 1080p H.264 | 4 Mbit/s | 43 | 1.3 TB | 3.9 TB |
| 1080p H.265 | 2 Mbit/s | 22 | 0.65 TB | 1.9 TB |
| 4K H.265 | 8 Mbit/s | 86 | 2.6 TB | 7.8 TB |

Multiply by cameras, add 15 % for filesystem overhead and evidence
stills, and size to the **retention the policy requires**, not to the
disk you have — the retention setting (Settings → Storage,
`retention_days`) is the control an auditor reads.
Prefer H.265 on the camera; it halves everything above. Keep
recordings on their own filesystem (`RECORDINGS_PATH`) so a full disk
never stops the database or the audit log.

## Network: three segments

The [security architecture](SECURITY_ARCHITECTURE.md#3-the-three-tier-architecture-in-code)
is a three-tier model; the appliance realises it with three VLANs on a
dual-homed host.

```
   cameras ──── VLAN 10 (camera LAN, RFC1918, NO gateway, no DNS) ──── NIC 1
                                                                        │
                                                          ┌─────────────┴─────────────┐
                                                          │  appliance                │
                                                          │  MediaMTX ingress (RTSP)  │
                                                          │  core · Tier-0 · KAI-C    │
                                                          │  nats · nats-apps · apps  │
                                                          │  nginx :443 · WebRTC UDP  │
                                                          └─────────────┬─────────────┘
                                                                        │
   operators / SIEM / NAS ── VLAN 20 (management), VLAN 30 (storage) ── NIC 2
```

* **Camera VLAN**: cameras and NIC 1 only. No default gateway, no route
  to the internet, no public DNS. Cameras authenticate to the appliance
  and to nothing else. `CAMERA_NETWORK_INTERFACE` names this NIC.
* **Management VLAN**: where operators browse to `https://<appliance>`
  (nginx, self-signed or your CA), where the SIEM subscribes to
  `opennvr.audit.*`, where a VPN concentrator terminates for remote
  operators. `NGINX_BIND_HOST` pins the UI to this NIC; WebRTC media
  (`WEBRTC_ICE_PORT`, UDP 8189) is published on the same NIC.
* **Storage VLAN** (M/L): NAS traffic, backups. Optional; on S it is
  the management VLAN.
* **Internet**: none by default. `DEPLOYMENT_MODE=offline` refuses
  every cloud route; `AI_SOVEREIGNTY=local_only` refuses adapters that
  declare egress; apps live on an internal Docker network and reach
  only the hosts their listing declared or the operator allowed
  ([APP_NETWORK.md](APP_NETWORK.md)). Updates arrive as pulled images
  through the management VLAN's egress proxy, or by sneakernet.

Ports the appliance exposes, and to whom:

| Port | Service | Segment |
|---|---|---|
| 443/tcp | nginx — UI, API, HLS, WebRTC signalling, playback | management |
| 8189/udp+tcp | WebRTC media (DTLS-SRTP) | management |
| 8322/tcp | RTSPS re-stream (VLC / downstream recorders) | loopback by default; management if published |
| 554/tcp (inbound to cameras) | RTSP from cameras | camera VLAN only |
| 8000, 8100, 4222, 3128, … | core, KAI-C, NATS, egress proxy | Docker bridge only — never published |

## Host

* Linux LTS — Ubuntu 24.04 LTS or Debian 12; Raspberry Pi OS (64-bit)
  on S. Unattended security updates on; kernel from the distribution.
* Docker Engine from Docker's repository (not a snap), Compose v2.
  The compose project runs as the distribution's `docker` group; no
  container is privileged except the opt-in one-click installer,
  which is off unless `APPS_INSTALL_ENABLED=true`.
* Time: `chrony` against an internal NTP source (the audit log's
  timestamps are evidence).
* Disk encryption (LUKS) on the system and recording volumes where
  the site policy asks for encryption at rest; the key held by the
  operator, entered at boot or via TPM.
* Firewall on the host (`nftables`/`ufw`): default deny inbound; allow
  443 and 8189 from the management VLAN, 8322 if published; nothing
  from the camera VLAN except established RTSP replies.
* Secrets in `.env` only, generated by `make secrets`; the boot
  validator refuses placeholders. Back `.env` up to the site's secrets
  store — losing `CREDENTIAL_ENCRYPTION_KEY` loses the camera
  credentials.

## Hardening checklist

What `scripts/evidence_pack.py` reports as PASS when all of it holds.

- [ ] `DEPLOYMENT_MODE=offline`, `AI_SOVEREIGNTY=local_only`,
      `MEDIAMTX_ALLOW_PLAINTEXT_OUTPUTS` unset (the defaults).
- [ ] Camera VLAN has no default gateway; `CAMERA_NETWORK_INTERFACE` set.
- [ ] Every camera on RTSPS where the camera supports it
      (`transport_security: rtsps_required`), and on a non-default
      username — the Compliance page's security check shows no flags.
- [ ] No camera from an FCC Covered List vendor (the same check flags
      them; [GOVERNMENT_DEPLOYMENT.md](GOVERNMENT_DEPLOYMENT.md) is the
      substitution guide).
- [ ] `NGINX_BIND_HOST` pinned to the management NIC; UI reached over
      TLS only (your CA, or the bundled self-signed cert imported into
      operator browsers).
- [ ] Recording enabled on every camera that the policy says to record;
      the retention setting equal to the policy's retention.
- [ ] Apps: only catalog apps, digest-pinned and signed
      (`INSTALLER_SIGNATURES=require`, the default); egress enforcement
      on (`APPS_EGRESS_ENFORCED=true`, the default); the Network line on
      every app card shows no refused attempts you cannot explain.
- [ ] AI adapters: none declaring `network_egress`; every one reporting
      a model fingerprint; adapter permissions approved by name.
- [ ] Audit log forwarded to the SIEM (`examples/alerts-subscriber` or
      the NATS `opennvr.audit.*` subjects); `policy.boot_posture`
      appears at every boot.
- [ ] Backups: the database (`opennvr_db_data`) nightly, `.env` in the
      secrets store, recordings per the retention policy (or explicitly
      not backed up — write it down).
- [ ] Physical: locked rack, port security on the camera switch,
      console access logged — the residual risks
      [COMPLIANCE.md](COMPLIANCE.md#whats-explicitly-out-of-scope) leaves
      with you.

## Multi-site

Each site is an appliance; there is no central server to own. What a
head office gets is what the sites publish: audit events and alerts
to a central SIEM over the management network, and evidence packs on
a schedule. Federated live view across sites is
`DEPLOYMENT_MODE=hybrid` territory and an explicit, audited decision
per site.

## Related

[ENTERPRISE.md](ENTERPRISE.md) — what an enterprise engagement includes ·
[SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) — the controls
behind every line above · [DETECT_CPU.md](DETECT_CPU.md) — sizing the
detector · [COMPLIANCE.md](COMPLIANCE.md) — framework mapping ·
[APP_NETWORK.md](APP_NETWORK.md) — app egress.
