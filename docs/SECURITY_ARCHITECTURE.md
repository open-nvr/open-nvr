# OpenNVR Security Architecture

> **Reference paper.** Singh, V. P., Bhandari, S. R., Singh, A., Kushwaha, R., & Kaura, S. (2025).
> *Eliminating Systemic IP Camera Vulnerabilities via Offline-First Open Security Architecture.*
> Zenodo. DOI [10.5281/zenodo.17261761](https://doi.org/10.5281/zenodo.17261761).

This document is the implementation companion to the paper above. It records (a) the
threat model OpenNVR is defending against, (b) the architectural decisions that follow
from it, (c) the concrete code surface where each defense is implemented, and (d) the
explicit gaps and roadmap items that remain. Anything in this document that diverges
from the running code is a bug — please open an issue.

## 1. Threat model in one paragraph

IP cameras are routinely exploited at internet scale through default credentials,
plaintext RTSP/RTP, opaque firmware supply chains, vendor-controlled cloud
aggregation, and slow patch cycles (paper §2, §3; CISA, NVD, ENISA references).
Conventional defenses — VLAN segmentation, perimeter firewalls, vendor patch
cycles — mitigate symptoms but leave structural weaknesses intact (paper §2.2, §6).
OpenNVR addresses the structural weaknesses directly by enforcing three
architectural invariants: cameras live on an isolated network with no internet
route; a hardened software-defined middleware is the only edge that operators and
analytics touch; and every long-lived secret, every encryption key, every retention
decision, and every analytics path stays under the operator's control.

## 2. Mapping the paper's six systemic challenge categories to OpenNVR

The paper enumerates six categories of systemic weakness (§3.1–§3.6). For each
category this section lists the relevant OpenNVR code paths, what is enforced
today, and what the roadmap item (V-xxx) is for the remaining gap. The roadmap
identifiers are tracked alongside their controls below and in the issue tracker.

### 2.1 Public exposure and credential abuse (paper §3.1)

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| No default admin password shipped | `env.example` ships no `DEFAULT_ADMIN_PASSWORD`. Account is created with `password_set=False` and a *high-entropy random* placeholder hash (never the literal `__UNSET__`). Activation requires the **one-time first-time-setup token** that is minted at startup and printed to stdout + the audit log; the token is constant-time-compared at the endpoint and consumed on first use. | `server/scripts/init_db.py`; `server/main.py` startup hook; `server/services/first_time_setup_service.py`; `server/routers/auth.py:first_time_setup`. |
| Provisioned admin password (for automated deployment) | If the operator explicitly sets `DEFAULT_ADMIN_PASSWORD`, the account is created with `password_set=True` so the supplied password is immediately usable. The setup-token flow is skipped in this branch. | `server/scripts/init_db.py`. |
| Account brute-force | bcrypt cost 12 with timing-equalised dummy-hash on failed login; account lockout after configurable failures. | `server/core/auth.py`. |
| MFA on by default | New admin and operator accounts are created with `mfa_enabled=True`. | `server/scripts/init_db.py`; `server/main.py` seed path. |
| Strong service-to-service secrets | Startup validator rejects empty, weak, short (`< 32`), or placeholder values for `SECRET_KEY`, `MEDIAMTX_SECRET`, `INTERNAL_API_KEY`, `CREDENTIAL_ENCRYPTION_KEY`. | `server/core/config.py:validate_strong_secrets`, `validate_fernet_key`. |
| Bootstrap workflow | `make secrets` emits cryptographically random values for every secret; `make check-secrets` verifies no placeholder ever shipped. The fragment list is loaded from `core.config._PLACEHOLDER_FRAGMENTS` so the Makefile and the runtime validator cannot drift. | `Makefile`. |
| Rate-limit on auth endpoints | **Gap — V-013.** Planned: `slowapi` 5/min/IP on `/auth/*`, 30/min/IP on `/streams/token/*`. | — |
| Unique-credential evidence for ETSI EN 303 645 | **Gap — V-021.** Planned: `firmware_health` table flags cameras with default-pattern passwords. | — |

### 2.2 Insecure protocols and legacy services (paper §3.2)

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| RTSPS / SRTP support | Camera-URL schema accepts both `rtsp://` and `rtsps://`; MediaMTX config wires `rtspsAddress` and `srtpAddress`. | `server/schemas.py`; `server/services/mediamtx_admin_service.py`. |
| Preferring encrypted transport per camera | **V-003.** Each camera carries a `transport_security` policy (`rtsps_required` / `rtsps_preferred` / `plaintext_allowed`) plus an async TLS-handshake probe outcome on its `CameraConfig` row. Provisioning runs through `enforce_transport_policy` at every MediaMTX choke point (path create, path patch, startup auto-provisioner, config-driven re-provisioner, admin debug endpoint), so all five entry paths into stream provisioning are covered before any MediaMTX HTTP call. Refusals surface as HTTP 409 with remediation hints and emit per-entry-point audit events; unknown policy values fail closed. Entry-point inventory and audit-event names live in the code paths listed in the right-hand column. | `server/services/transport_probe_service.py`; `server/services/mediamtx_admin_service.py:provision_path`; `server/services/mediamtx_startup_service.py:_provision_camera_with_retry`; `server/services/camera_config_service.py:provision`; `server/routers/mediamtx_admin.py:push_rtsp_stream`; `server/services/camera_service.py:create_camera`; `server/routers/cameras.py`; `server/models.py:CameraConfig.transport_security*`; migration `b4e2a9c7f1d0_add_camera_transport_security.py`. |
| Eliminating plaintext outbound to viewers | **V-019.** All three operator-facing transports terminate TLS at MediaMTX itself — RTSPS at `:8322`, HLS-over-HTTPS at `:8888`, WebRTC signalling over HTTPS at `:8889`. WebRTC media stays DTLS-SRTP at the wire. `rtmp: no` and `srt: no` are explicit. Inside the host, a plaintext RTSP listener bound to loopback serves the in-kernel KAI-C inference tap — see the *RTSP encryption posture* subsection below for the trust-boundary rationale and the operator-side knobs (`rtspEncryption`, `INFERENCE_USE_MEDIAMTX_TAP`, `MEDIAMTX_ALLOW_PLAINTEXT_OUTPUTS`). Known residual: JWT-in-URL tokens still appear in browser history and proxy access logs even over TLS; tracked under V-007 for header-based migration. | `mediamtx.docker.yml`, `mediamtx.yml`, `mediamtx.local.yml`, `docker-compose.*.yml`, `scripts/generate-mediamtx-certs.{sh,ps1}`; `server/core/config.py:mediamtx_rtsps_url`, `mediamtx_allow_plaintext_outputs`, `inference_use_mediamtx_tap`; `server/services/kai_c_service.py:_resolve_inference_rtsp_url`; `server/services/mediamtx_jwt_service.py:create_stream_token`; `server/routers/streams.py:tokens`; `server/core/policy.py:current_posture`. |
| Telnet / UPnP detection on cameras | **Gap — V-014/V-021.** Planned: ONVIF probe records active services and flags Telnet/UPnP. | `server/services/onvif_service.py`. |

#### RTSP encryption posture — the trust-boundary distinction

OpenNVR's RTSP encryption choice is deliberately asymmetric: TLS on the
operator-facing surface, plaintext on the in-host inference tap. The
reason is that TLS protects information crossing a trust boundary; two
processes that share the same kernel and the same Docker network are
not crossing one, and paying per-frame encrypt + decrypt cost on that
hop buys nothing.

The three RTSP listeners and what they're for:

| Listener | Address (default) | Who reads it | Encryption |
|---|---|---|---|
| Operator-facing RTSP | `:8322` (RTSPS), port-mapped to `127.0.0.1:8322` on the host | External tools the operator points at the NVR (VLC, ffmpeg, third-party recorders) | TLS — clients are on the LAN or further out |
| Operator-facing HLS / WebRTC | `:8888` / `:8889`, port-mapped to `127.0.0.1` | Browsers running the OpenNVR web UI | TLS — same reason |
| Internal inference tap | `:8554` (RTSP plaintext), NOT port-mapped in bridge mode; pinned to `127.0.0.1:8554` in host mode | KAI-C's frame-capture loop, same machine | Plaintext — never leaves the host's loopback |

What stays the same regardless of encryption: **JWT authentication
applies to all three listeners.** MediaMTX's `authMethod: jwt` is
global; the plaintext listener still refuses anonymous reads. KAI-C
mints a wildcard-read token via `MediaMtxJwtService.create_stream_token`
(cached for 50 minutes, refreshed transparently) and appends it as
`?jwt=<token>` to its RTSP URL. The token rides through ffmpeg's RTSP
client because MediaMTX has `authJWTInHTTPQuery: true`.

What the inference tap actually buys:

1. **No double-pull of the camera.** Without the tap, MediaMTX opens
   one RTSP session to the camera (for recording + browser playback)
   and KAI-C opens a second one (for inference). Many consumer cameras
   cap concurrent RTSP sessions at 1–4; the double-pull can be the
   difference between a working camera and a refused connection.
2. **No per-frame TLS overhead on a loopback hop.** RTSPS to `:8322`
   would encrypt every RTP packet on a pipe that never leaves the host
   — measurable CPU cost (~15-35% of the steady-state inference budget
   on Pi-class hardware) with no threat-model gain.
3. **One source of truth for the camera connection.** MediaMTX's
   recording timeline and KAI-C's inference timeline share the same
   underlying RTP clock, which makes "this alert at 22:14 corresponds
   to this segment" correlation deterministic instead of best-effort.

When to flip back to strict mode (`rtspEncryption: "strict"` plus
`INFERENCE_USE_MEDIAMTX_TAP=false`):

- **MediaMTX and KAI-C on different hosts.** If your deployment splits
  the streaming layer from the inference layer onto separate machines,
  the MediaMTX→KAI-C hop now crosses a real network boundary and
  should be TLS-protected. Inference falls back to pulling each camera
  directly, accepting the double-pull cost.
- **Operator policy requires "no plaintext anywhere, ever."** Some
  regulated environments treat plaintext loopback as a finding even
  when the threat model doesn't justify it. Strict mode + tap-off
  satisfies the audit without arguing.

Either decision lands in the boot audit log: `INFERENCE_USE_MEDIAMTX_TAP`
is recorded at startup the same way the other security-relevant config
is, and a flip from default surfaces in `/system/posture`.

### 2.3 Fragmented interoperability (paper §3.3)

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| ONVIF profile detection | `OnvifService` discovers Profile S / Profile T support per camera; Profile capabilities stored against the camera. | `server/services/onvif_service.py`; `server/routers/onvif.py`. |
| Digest-auth fallback for non-compliant cameras | `OnvifDigestService` provides a tested compatibility path; the credential vault never exposes plaintext passwords to the UI. | `server/services/onvif_digest_service.py`; `server/services/credential_vault_service.py`. |
| Heterogeneous fleet policy | Centralized policy via RBAC + `PermissionChecker`; per-camera settings stored in `CameraConfig`. | `server/core/permissions.py`; `server/models.py`. |

### 2.4 Vendor-controlled cloud and AI pipelines (paper §3.4) — the offline-first thesis

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| Storage stays customer-controlled | Recordings written to `RECORDINGS_BASE_PATH`; the base is resolved at access time and every file operation is symlink-resolved and containment-checked. The recording-upload sink additionally **refuses any DB-stored `file_path` that is absolute and does not name the recordings subtree**, so a DB-poisoned record pointing at `/etc/passwd` cannot be rewritten into a relative-under-base lookup. **Residual risk:** there is a TOCTOU window between the path check and the upload worker's `open()`; closing it requires `O_NOFOLLOW` at the open site and is tracked as an M2 follow-up. | `server/services/storage_service.py:resolve_under_root`, `safe_recording_path`; absolute-path refusal in `server/routers/recordings.py:queue_cloud_upload_for_day`. |
| Credential vault Fernet-at-rest | Camera passwords and cloud-provider tokens are encrypted at rest with the Fernet key the operator generated locally. | `server/services/credential_vault_service.py`. |
| KAI-C never reveals adapter URLs to the UI | The NVR talks to a single internal endpoint (`KAI_C_URL`); adapter routing happens inside KAI-C. | `server/services/kai_c_service.py`; `kai-c/`. |
| Cloud connectors are not opt-in / off by default | **V-009.** `settings.deployment_mode: Literal["offline","hybrid","cloud"]` defaults to `offline`. `core.policy.require_outbound_allowed` returns 403 on every cloud-touching route in this mode (`/cloud-inference/infer`, `/cloud-inference/jobs`, `/cloud-streaming/targets`, `/cloud-streaming/targets/{id}/start`, `/recordings/cloud-upload/day`). Defense-in-depth at the service call-sites (`cloud_inference_service._call_kai_c`, `cloud_recording_service.upload_to_nvr`) catches background-task callers that survive a mid-flight policy change. Boot posture audit-logged via `policy.boot_posture` event. Surfaced via `GET /api/v1/system/posture`. | `server/core/policy.py`, `server/routers/cloud_inference.py`, `server/routers/cloud_streaming.py`, `server/routers/recordings.py`, `server/services/cloud_inference_service.py`, `server/services/cloud_recording_service.py`, `server/routers/system.py`. |
| AI sovereignty: refuse to leak frames to vendor inference | **V-022.** `settings.ai_sovereignty: Literal["local_only","federated","cloud_allowed"]` defaults to `local_only`. `core.policy.require_ai_sovereignty_allowed` stacks with the outbound gate on the cloud-inference router. KAI-C imports its own `AI_SOVEREIGNTY` env var and refuses to start if any registered adapter URL is non-loopback in `local_only` mode; `POST /infer/cloud` (HuggingFace proxy) returns 403 outright. Same posture endpoint and audit entry. | `server/core/policy.py`, `kai-c/main.py:_validate_adapters_match_sovereignty`. |
| Customer-managed encryption keys (KMS / HSM / TPM) | **Gap — V-004.** Today there is a single Fernet key in env. Planned: `KeyProvider` abstraction with `EnvKeyProvider`, `FileKeyProvider`, `VaultProvider`, `KMSProvider`, `TPMProvider`; per-camera DEK for recording-at-rest encryption; `key_id` column on encrypted fields for rotation. | `server/core/keys/` (planned). |

### 2.5 Supply chain and firmware transparency (paper §3.5)

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| Open-source middleware | The middleware tier (server, app, kai-c, ai-adapter) is GNU AGPLv3; community-auditable. | `server/main.py` (AGPL header in every source file). |
| Reproducible build | `uv.lock` is committed; container images use pinned base. | `server/uv.lock`; `Dockerfile` (planned: pinned digest). |
| SBOM signed and exposed | **Gap — V-011.** Planned: CycloneDX SBOM generation in build (`cyclonedx-py`, `cyclonedx-npm`, `syft` for Docker), cosign-signed releases, `/api/v1/system/sbom` endpoint returning the running SBOM for in-field verification. | — |
| Camera firmware CVE cross-reference | **Gap — V-014.** Planned: `firmware_health` table populated by periodic NVD/KEV check; refuse to add cameras whose firmware is in the CISA KEV catalog unless explicit override. | `server/routers/firmware.py`. |

### 2.6 Lifecycle and patch management (paper §3.6)

| Concern | OpenNVR enforcement | Code path |
|---|---|---|
| Middleware patch cadence decoupled from camera firmware | Linux LTS base; OpenNVR's own update path is independent of any camera vendor's release cycle (paper §4.3). | Deployment docs (planned). |
| EoL firmware visibility per camera | **Gap — V-014.** Planned: surface in UI "X cameras run firmware with N known CVEs, M EoL." | — |
| Tamper-evident audit log | **Gap — V-012.** Planned: hash-chained audit rows giving ISO 27001-compatible immutability. | `server/services/audit_service.py`. |

## 3. The three-tier architecture in code

The paper proposes a three-tier model (§4.2): **Isolated Camera Network →
Secure Middleware Gateway → Destination / Analytics**. The table below maps each
tier to the OpenNVR component that realises it.

### Tier 1 — Isolated Camera Network

The camera VLAN is RFC1918, has no default gateway, no internet route, no DNS
to the public internet, and authenticates only to the middleware.

| Paper requirement | OpenNVR component | Status |
|---|---|---|
| Private RFC1918 subnet for cameras | Operator deployment guide; `routers/network.py` stores intended firewall rules. | Documented; enforcement gap. |
| No default gateway on camera subnet | **V-016.** Startup validator: refuse to boot if the camera NIC has a default route. | Planned. |
| DNS blackholing | **V-017.** Bundled `unbound`/`dnsmasq` config template installed by `opennvr-netd`. | Planned. |
| Cameras authenticate only to the middleware | RTSP credentials encrypted at rest in the credential vault; MediaMTX is the only consumer of the camera's RTSP URL. | Implemented. |
| OS-level enforcement of firewall rules | **V-010.** Privileged `opennvr-netd` systemd unit materialises DB rules via `nftables`. Without it, `/health` and the UI banner say "isolation is advisory only." | Planned. |
| Dual-homed gateway (camera NIC + management NIC) | **V-016.** `CAMERA_NETWORK_INTERFACE` and `MGMT_NETWORK_INTERFACE` settings; startup validator refuses to boot if they overlap. | Planned. |

### Tier 2 — Secure Middleware Gateway

The middleware re-streams over TLS/SRTP, enforces RBAC, mediates recording and
AI, and is the only edge any operator or downstream system touches.

| Paper requirement | OpenNVR component | Status |
|---|---|---|
| TLS/SRTP re-streaming on the operator side | MediaMTX configured with `rtspEncryption: "strict"`, `hlsEncryption: yes`, `webrtcEncryption: yes`. V-019 closed; see §2.2. WebRTC media remains DTLS-SRTP at the wire (always). | Implemented. |
| Customer-controlled, software-defined gateway | FastAPI server + MediaMTX, both running under `systemd` on Linux LTS. | Implemented. |
| RBAC | `Role`, `Permission`, `PermissionChecker`. | Implemented. |
| Certificate-based authentication | **V-018.** Internal mTLS between NVR ↔ kai-c ↔ ai-adapter; optional client certs for high-end cameras; in-house CA bootstrapped at install. | Planned. |
| Trust-zone-only by default for MediaMTX ingress | **V-015.** Two-NIC model: the *ingress* MediaMTX URLs (camera LAN / Docker bridge / VPN overlay) speak plaintext RTSP and HTTP, so they must stay inside the OpenNVR trust zone — addresses an attacker on the public internet cannot directly route to. Startup validator accepts loopback (127/8, ::1), RFC1918 (10/8, 172.16/12, 192.168/16), IPv6 ULA (fc00::/7), and IPv4/IPv6 link-local (169.254/16, fe80::/10) for `MEDIAMTX_BASE_URL`, `MEDIAMTX_ADMIN_API`, `MEDIAMTX_HLS_URL`, `MEDIAMTX_RTSP_URL`, `MEDIAMTX_RTSPS_URL`, `MEDIAMTX_PLAYBACK_URL`. Rejects public IPs, public FQDNs, scheme-less URLs, and the `0.0.0.0` wildcard bind. Hostname resolution is bounded by a 2-second timeout so broken DNS at boot cannot hang startup. **There is no escape-hatch flag** — the previous `ALLOW_REMOTE_MEDIAMTX` field was retired in ISSUE-4 because letting plaintext MediaMTX cross the trust boundary is a CVE-by-default. **Scope note (egress):** the `MEDIAMTX_EXTERNAL_*` URLs are intentionally *not* in this check because they are the egress/uplink-side endpoints behind your TLS-terminating reverse proxy; their security comes from that proxy (TLS/WebRTC), not from the trust-zone check. | `server/core/config.py:_enforce_mediamtx_internal`, `_host_is_internal`. |
| Hardened HTTP edge | CORS allowlist (no `*`). **V-006/V-007/V-008** still pending: CSP/HSTS/X-Frame headers; cookie-based JWT; CSRF. | Partial. |
| TLS-on-LAN for the UI / API | **ISSUE-6.** opennvr-core's host port is bound to `127.0.0.1:8000` so the bare HTTP API is never reachable from the LAN. The Tier 0 compose ships an `nginx:1.27-alpine` reverse proxy that terminates a self-signed cert on `0.0.0.0:443` and proxies to opennvr-core over the Docker internal bridge — which V-015 already classifies as inside the trust zone (RFC1918), so cleartext on that hop is acceptable. The cert is minted by `nginx-certs-init` on first boot with SAN coverage for `localhost`, `127.0.0.1`, `::1`, `opennvr`, `opennvr.local`, and the host's detected LAN IP (auto-propagated by `start.sh` since v9). The keypair lives under `./nginx-certs/` and is gitignored. Operators with their own TLS terminator (Caddy, Cloudflare Tunnel, bare-metal nginx) can remove the bundled service and route traffic at opennvr-core directly. Browsers warn once about the self-signed CA — operators trust it manually or import the cert into their OS trust store; either way JWT now travels over TLS on the LAN segment, which is the security goal. | `docker-compose.tier0.yml`, `nginx/opennvr.conf`. |
| TLS-fronted media plane (WebRTC / HLS / recording playback) | **ISSUE-6 v8.** nginx adds `/webrtc/`, `/hls/`, `/playback/` proxy locations that forward to MediaMTX over the Docker bridge. opennvr-core's token endpoint emits same-origin HTTPS URLs (`https://<host>/webrtc/cam-X/whep` etc.), so browsers no longer hit mixed-content blockers and the bare `127.0.0.1` URLs are never exposed. WebRTC media is DTLS-SRTP over UDP/8189, published on the same NIC nginx is bound to (`NGINX_BIND_HOST`) — uplink only in dual-NIC mode. MediaMTX advertises the right ICE candidate via `MTX_WEBRTCADDITIONALHOSTS`, also auto-derived by `start.sh`. The `/webrtc/` and `/hls/` proxy upstreams are HTTPS (MediaMTX `hlsEncryption=yes`, `webrtcEncryption=yes` per V-019) with `proxy_ssl_verify off` because the cert is self-signed and the bridge is inside V-015's trust zone — same posture as nginx → opennvr-core HTTP. Bug fix: `recordings.py:get_playback_url` previously emitted the bridge-internal URL to the browser; now uses the external fallback chain matching `streams.py`. | `nginx/opennvr.conf`, `docker-compose.tier0.yml`, `server/routers/recordings.py`. |
| Cert auto-configuration end-to-end | **ISSUE-6 v9.** `start.sh`'s `configure_nginx_bind_host` exports `OPENNVR_HOST_IP` (when not already set) based on the same detection used for `NGINX_BIND_HOST`. Both `nginx-certs-init` and `mediamtx-certs-init` read it and add `IP:<detected>` to their SAN list. Browser warning shrinks from two (CN/IP mismatch + self-signed CA) to one (self-signed CA only — one-click bypass). MediaMTX cert SAN extension also helps direct-RTSPS via VLC at `rtsps://<host-ip>:8322`. `./start.sh refresh-certs` regenerates both certs on demand (LAN IP changed, certs expired, operator updated `OPENNVR_HOST_IP`). The shell-env value never overrides an operator-set `OPENNVR_HOST_IP` in `.env`. | `start.sh:configure_nginx_bind_host`, `docker-compose.tier0.yml`. |
| Tamper-evident audit logging | `audit_service.py`. **V-012** still pending for hash-chain. | Partial. |
| MediaMTX webhook authentication | `X-MTX-Secret` HMAC on every hook; `MEDIAMTX_SECRET` is validated at startup as a strong, non-placeholder value. | Implemented. |

### Tier 3 — Destination / Analytics

Recordings, live streams, and AI inference all flow over encrypted channels and
stay under the operator's control.

| Paper requirement | OpenNVR component | Status |
|---|---|---|
| Customer-owned recording storage | Configurable `RECORDINGS_BASE_PATH`; all access containment-checked. | Implemented (V-005). |
| Encryption at rest for recordings | **V-004.** Planned: per-camera DEK wrapping; integrated with `KeyProvider`. | Planned. |
| AI inference under customer control | KAI-C orchestrator + AI Adapter on the same trust boundary; HuggingFace adapter exists as opt-in only. | Implemented. |
| Federated/local-only AI policy | **V-022.** Planned: `ai_sovereignty` setting; in `local_only`, the cloud inference router returns 403 and KAI-C blocks remote routing. | Planned. |
| Remote operator access without exposing cameras | **V-020.** Planned: bundled Wireguard helper; the UI states "remote access via VPN only." | Planned. |

## 4. The five design principles in code (paper §4.1)

| Principle | Where OpenNVR realises it |
|---|---|
| **Complete network isolation.** Cameras on a private subnet, no internet. | Network router stores firewall rule intent; `opennvr-netd` enforces (V-010); dual-homed validator (V-016); DNS blackhole template (V-017). |
| **Secure middleware enforcement.** All traffic goes through a hardened gateway. | FastAPI + MediaMTX; trust-zone validator on ingress URLs (V-015 ✓, no escape hatch); nginx TLS terminator on the LAN edge (ISSUE-6 ✓); CORS allowlist (✓); CSP/HSTS/CSRF (V-006–V-008 planned). |
| **Customer sovereignty.** Keys, retention, updates under the operator. | Local DB; Fernet-at-rest credentials (✓); `KeyProvider` with TPM tier (V-004 planned); retention service (✓); update path is `apt`/`uv` on Linux LTS. |
| **Open standards and transparency.** ONVIF, IETF; auditable. | ONVIF Profile S/T discovery; RFC 7826 RTSPS / RFC 3711 SRTP support; AGPLv3; SBOM (V-011 planned). |
| **Community-driven development.** Open source. | Public repo, AGPLv3, contributor-friendly module boundaries. |

## 5. Compliance posture (paper §5)

Each framework named in the paper is paired with the OpenNVR controls that
satisfy or partially satisfy it. The longer-form mapping lives in
`docs/compliance/` (planned, V-021).

* **CISA Secure-by-Design.** Random initial admin password, strong-secret
  startup validator, trust-zone-only MediaMTX ingress with no escape
  hatch (V-015), no plaintext defaults in `env.example` — every default
  the operator inherits is the safe one.
* **NIST CSF 2.0 (Identify–Protect–Detect–Respond–Recover).** Identify:
  ONVIF discovery + firmware_health (V-014 planned). Protect: RBAC, MFA,
  Fernet vault, secure middleware. Detect: Suricata IDS integration
  (`routers/suricata_*.py`), audit log. Respond: audit + alerting
  (V-012 hash-chain). Recover: retention service + cloud archive (opt-in
  only under V-009).
* **ISO/IEC 27001:2022.** RBAC + audit log map to A.5/A.8 controls;
  Fernet-at-rest maps to A.10; loopback-only middleware maps to A.13. The
  hash-chained audit (V-012) gives the ISMS the tamper-evidence the standard
  expects.
* **ETSI EN 303 645.** §5.1 no universal default passwords — V-001 ✓.
  §5.4 secure storage of sensitive parameters — Fernet vault ✓. §5.5
  communicate securely — MediaMTX RTSPS/HLSS/WebRTC-DTLS-SRTP all
  default-on per V-019 ✓.  §5.6 minimise exposed attack surfaces —
  trust-zone-only MediaMTX ingress ✓ (V-015 absolute, no flag bypass;
  V-010/V-016/V-017 for camera-LAN side).
* **NIST AI RMF 1.0.** Map → V-022 ai_sovereignty setting; the local_only
  default means no AI processing of footage ever leaves the operator's
  trust boundary unless they opt in.
* **ENISA Threat Landscape 2024.** Patch cadence: middleware on Linux LTS
  vs. vendor camera firmware. Patch latency target: critical security
  fix from main → release within 7 days (operational SLA, tracked in CI).
* **GDPR / DPDP.** Customer-controlled keys + customer-controlled storage
  + audit log give the data-subject-rights workflows the foundation they
  need; retention service supports per-camera retention policy.

## 6. Residual risks (paper §8)

The paper explicitly calls out three residual risks the architecture does
*not* claim to solve. OpenNVR inherits all three and documents them so the
operator can apply compensating controls:

| Residual risk | OpenNVR's compensating control |
|---|---|
| Camera firmware still vendor-controlled | `firmware_health` (V-014) surfaces CVEs / EoL status; an operator can refuse to add KEV-listed firmware. |
| Insider with subnet access | RBAC, MFA, audit log; Suricata IDS on the management subnet; physical-security recommendations in deployment docs. |
| Hardware supply-chain implants in camera SoCs | Out of scope architecturally — but the offline-first design *contains* the blast radius because any covert channel cannot egress to the public internet from the camera subnet (V-010/V-016/V-017). |

## 7. Roadmap (paper §9 future work + this review)

The vulnerability IDs (`V-001` … `V-022`) below cross-reference the systematic
review in the [Zenodo paper](https://doi.org/10.5281/zenodo.17261761).

### Shipped

* **Account hardening.** V-001 (no default password + token-gated
  first-time-setup), V-002 (placeholder-secret rejection across runtime
  validator and Makefile linter, ≥6-char fragment matching to avoid
  random-token false positives), V-005 (recording-path traversal guard +
  absolute-path refusal at the upload sink), V-015 (MediaMTX ingress
  trust-zone enforcement: loopback + RFC1918 + IPv6 ULA + link-local
  accepted, public addresses + `0.0.0.0` + scheme-less URLs refused,
  DNS-resolution timeout, no escape-hatch flag), `make secrets`.
  **Breaking change:** the
  minimum length for symmetric secrets is 32 characters; existing
  operators must re-run `make secrets` or extend their secrets to
  ≥32 chars before upgrading from a pre-release.
* **Network posture.** V-009 (`deployment_mode`) and V-022
  (`ai_sovereignty`) shipped as paired settings + paired router/service
  gates + KAI-C startup validator + `/system/posture` endpoint +
  boot audit entry. See entries in §2.4 for code paths.
* **Operator transport TLS.** V-019: MediaMTX hardened YAML templates
  default to `rtspEncryption: "strict"` (TLS-required RTSPS),
  `hlsEncryption: yes`, `webrtcEncryption: yes`, `rtmp: no`, `srt: no`.
  The backend emits a TLS-explicit `urls.rtsps` field and the
  docker-compose port map exposes the RTSPS listener at
  `127.0.0.1:8322`. `scripts/generate-mediamtx-certs.{sh,ps1}` provisions
  a self-signed cert pair when no PKI is available.
  `mediamtx_allow_plaintext_outputs` records the operator's deviation
  when the permissive dev template (`mediamtx.local.yml`) is in use.
  Known residual: JWT-in-URL (tracked under V-007 follow-up).
* **Per-camera transport policy.** V-003: per-camera
  `transport_security` policy (`rtsps_required` / `rtsps_preferred` /
  `plaintext_allowed`) on `CameraConfig`, populated by an async
  TLS-handshake probe at camera-create time. `POST
  /api/v1/cameras/{id}/probe-transport` re-runs the probe; operator
  overrides are preserved unless `?reset_policy=true`. **Runtime
  enforcement** lives at `provision_path` (the MediaMTX choke point)
  and covers all four entry paths: manual `/provision-mediamtx`, the
  startup auto-provisioner, the config-driven re-provisioner, and the
  admin debug push endpoint. Refusals return HTTP 409; the startup
  loop marks the camera `status=policy_blocked` and aborts retries.

### Planned

* **Network isolation enforcement.** V-010 (`opennvr-netd`), V-016
  (dual-homed validator), V-017 (DNS blackhole template), V-020
  (Wireguard remote-access helper).
* **Customer sovereignty for keys and audit.** V-004 (KeyProvider
  + TPM tier + recording-at-rest), V-012 (hash-chained audit), V-021
  (compliance posture API + docs).
* **Middleware hardening defense in depth.** V-006 (security
  headers), V-007 (cookie JWT), V-008 (CSRF), V-013 (rate limit), V-018
  (internal mTLS).
* **Supply chain transparency.** V-011 (SBOM + cosign), V-014
  (firmware_health + CVE cross-reference).

Each item is independently shippable and produces user-visible audit
evidence aligned with the compliance frame in §5.

## 8. Conventions for new code

Anyone adding code to OpenNVR should treat the following as load-bearing:

1. **No new internet egress paths without a deployment-mode check.** Any
   code that talks to a non-loopback host on the public internet must be
   gated by `settings.deployment_mode != "offline"` and audited on call.
2. **No filesystem operation against a request-supplied or DB-supplied
   path without `safe_recording_path()`.** This is the V-005 contract.
3. **No new secrets without entries in the startup validator.** If you add
   an env var that holds key material, add it to `validate_strong_secrets`
   in `server/core/config.py`.
4. **No new MediaMTX ingress URLs without entries in the V-015 trust-zone check** (and no new operator flag that bypasses it — V-015 is absolute by design; for cross-trust-boundary deployments, add it to `MEDIAMTX_EXTERNAL_*` instead).
5. **No plaintext credential storage.** Use `CredentialVaultService`.
6. **Every state-changing route must record an audit-log entry on both
   success and denial.** Mirror the ISO 27001 access-control auditability
   requirement.

## 9. References

Full bibliographic detail is in the paper. The CVE and advisory list that
OpenNVR's `firmware_health` job (V-014) consumes:

* CVE-2021-36260 (Hikvision RCE)
* CVE-2022-30563 (Dahua ONVIF replay)
* CVE-2023-0773 (Uniview auth bypass)
* CVE-2024-7029 (AVTECH; CISA ICSA-24-214-07)
* CVE-2025-1316 (Edimax; CISA ICSA-25-063-08)
* CVE-2019-11219 / CVE-2019-11220 (iLnkP2P)
* CVE-2021-28372 (ThroughTek Kalay SDK; CISA ICSA-21-229-01)
