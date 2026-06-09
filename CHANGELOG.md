# Changelog

All notable changes to OpenNVR are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — targeting v0.1.0

First public release. The architecture has been redesigned around three
principles: secure by default, sovereign by design, and pluggable by contract.

### Added

#### Architecture

- **Pluggable AI adapter ecosystem.** Any model behind a REST or WebSocket
  endpoint becomes a first-class capability via the AI Adapter Contract v1.
  Seven reference adapters ship at v0.1: YOLOv8 object detection (ONNX,
  WebSocket streaming), InsightFace face detection + recognition with REST
  face-DB enrollment, Whisper ASR via faster-whisper, Piper TTS with inline
  audio response, fast-plate-ocr license-plate recognition, BLIP scene
  captioning, and ByteTrack multi-object tracking (the first post-processor
  adapter — composes with any detection-shaped upstream by chaining through
  KAI-C). Plus the Ollama integration with OpenAI-style tool calling. Wire
  spec lives in [`docs/AI_ADAPTER_CONTRACT.md`](docs/AI_ADAPTER_CONTRACT.md);
  authoring guide and template scaffold in the sister
  [`ai-adapter`](https://github.com/open-nvr/ai-adapter) repo.
- **`opennvr-adapter-sdk`.** Apache-2.0 SDK that adapter authors install to
  write a new detector in ~30 lines of code. Lives in the
  `open-nvr/ai-adapter` repository; PyPI publish wires off the first
  `sdk-v*` tag — until then, install from source.
- **NATS event bus.** Inference results and alerts publish to
  `opennvr.inference.*` and `opennvr.alerts.*` subjects. Build downstream
  applications with the copy-as-template subscriber pattern.
- **KAI-C middleware.** Sits between the OpenNVR server and adapter containers,
  enforcing sovereignty, recording the audit chain, and proxying HTTP and
  WebSocket inference calls.
- **End-to-end correlation ID.** Every inference carries an
  `X-Correlation-Id` joined from alert → middleware → adapter line, so an
  operator investigating "why did this alert fire at 22:14?" never has to
  guess.
- **Model fingerprint drift detection.** sha256 of every loaded model is polled
  every 60 seconds. Drift surfaces as an `adapter.fingerprint_mismatch` audit
  event — never silence.
- **Append-only audit log.** Adapter registration, refusal, drift, inference,
  and sovereignty violations are all recorded with reason codes that grep
  cleanly.

#### Examples

- `intrusion-detection` — detect people in restricted zones during restricted
  hours, with HTTP and WebSocket transports.
- `loitering-detection` — NATS subscriber with a dwell-time state machine.
- `inference-listener` — minimal NATS subscriber template for community
  contributors.
- `alerts-subscriber` — fan-out alerts to webhooks, logs, or your own tooling.
- `license-plate-recognition` — drives YOLOv8 + the fast-plate-ocr adapter
  via KAI-C in a two-stage chain, with allowlist / denylist watchlist
  routing and Pillow-based plate cropping.
- `smart-doorbell` — drives the InsightFace adapter via KAI-C, classifies
  visitors into family / known / unknown with severity routing, and embeds
  a base64 JPEG snapshot of unknown faces directly in the alert envelope
  so downstream relays (a ~15-line Telegram / ntfy / Discord bridge — see
  `alerts-subscriber/` for the template) can post the photo with the
  notification. Pure-REST enrollment flow — `python smart_doorbell.py
  enroll --image alice.jpg ...` works from any machine that can reach the
  adapter, no shared volume, no desktop GUI.
- `package-delivery` — drives YOLOv8 against a porch ROI, threads detections
  with a per-camera IoU tracker, and runs a state machine that distinguishes
  arrival, optional linger, and disappearance. The disappearance event
  routes "owner pickup" vs "porch pirate" by whether a person was sighted
  in the ROI during a configurable lookback window — info vs high severity.
  The whole point is the state machine: copy this folder and replace the
  predicate to ship "car arrived and stayed", "dog left the yard", "shed
  door open longer than X" without rewriting the alert plumbing.
- `home-assistant-relay` — a NATS subscriber that bridges
  `opennvr.alerts.>` into Home Assistant entities via MQTT discovery
  (recommended — HA auto-creates the entities on the first fire with
  the right device_class, friendly name, and device card) or HA's
  REST `/api/states` endpoint. Built-in mapping rules cover every
  shipped OpenNVR producer-side example (smart-doorbell,
  package-delivery, intrusion-detection, loitering-detection,
  license-plate-recognition); operators override per source and per
  camera via the `mappings:` config block. Binary sensors hold ON
  for a configurable window then auto-flip OFF so HA automations
  read the alert as an event, not a sticky alarm. Closes the loop:
  OpenNVR fires alerts → HA dashboards and automations consume them
  with zero extra wiring beyond the standard HA UI.
- `camera-agent` — a voice agent that grounds its answers in live
  OpenNVR camera feeds via tool calling. Pipecat pipeline with Silero
  VAD on a WebSocket transport; custom Pipecat services wrap the
  Whisper / Ollama / Piper adapters for the streaming voice path, and
  four registered tools (BLIP scene caption + YOLOv8 detection +
  InsightFace recognition + NATS event history) hit KAI-C for the
  auditable inference. All CPU-runnable; default model `llama3.2:3b`
  is roughly 3 GB RAM and 5-15 tok/s on a modern CPU. The first
  OpenNVR example where cameras have agency, not just data — operators
  ask "what's on the front porch?", the agent runs the right tool
  against the right camera and answers in voice. Ships in v0.1 with
  the three v0.1 integration gaps closed: the BLIP SDK adapter is
  now live alongside InsightFace + YOLOv8; the Piper SDK adapter
  supports inline ``audio_b64`` responses so the camera-agent's
  Pipecat TTS service gets audio bytes back over plain HTTP; and a
  camera-agent-local raw-PCM WebSocket serializer lets the
  self-contained ``/demo`` page work with vanilla JS + AudioContext
  without bundling Pipecat's JS client. 52 unit tests cover the
  config loader, frame cache, event ring, tool dispatch, and the
  raw-PCM serializer.

Each example ships with a `config.example.yml`, a `README.md`, and a focused
test suite designed to be read in five minutes.

#### Performance

- **Inference fast-path: KAI-C taps MediaMTX's loopback RTSP instead of
  double-pulling the camera.** The inference frame-capture loop now reads
  from `rtsp://mediamtx:8554/cam-N` (plaintext, internal-only) instead of
  opening a second concurrent RTSP session directly to each camera.
  Eliminates the double-pull most consumer cameras can't tolerate and
  removes the per-frame TLS overhead the previous path would have paid on
  a same-kernel hop. Pi-class hardware sees roughly 15–35% headroom back
  in the steady-state inference budget. JWT auth still applies — KAI-C
  mints a wildcard-read token through `MediaMtxJwtService` and appends it
  to the URL as `?jwt=<token>`. Operators in distributed deployments can
  flip back to the per-camera RTSP pull via
  `INFERENCE_USE_MEDIAMTX_TAP=false` plus `rtspEncryption: "strict"`
  in `mediamtx.docker.yml`. Trust-boundary rationale documented in
  [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md)
  §"RTSP encryption posture".

#### Installation

- **Tier 0 install path** (`docker-compose.tier0.yml`). Pulls pre-built
  container images from `ghcr.io/open-nvr/*` — no source build, no
  toolchain, no manual model downloads. NVR core + YOLOv8 detection in
  ~5 minutes wall-clock on a typical home broadband link. Camera-agent
  voice overlay is one additional compose flag (`-f
  docker-compose.camera-agent.yml --profile camera-agent`). YOLOv8
  weights auto-fetched from Hugging Face on first boot.
- **Pre-built container images** on GHCR: `ghcr.io/open-nvr/core` plus
  the seven per-adapter images (yolov8 / piper / whisper /
  fast-plate-ocr / insightface / blip / bytetrack). Published by the
  `publish-images.yml` workflow on every release tag.
- **Interactive installer.** `./start.sh` (Linux / macOS) and `.\start.ps1`
  (Windows) still work as the source-build path — useful when running an
  unreleased commit or modifying the core itself.
- **`make secrets` / `make secrets-env` / `make check-secrets`** for
  bare-metal developer workflows.
- **Reusable React frontend** with a first-time-setup flow that gates admin
  activation on a one-time token.

#### Documentation

- [`docs/AI_ADAPTER_CONTRACT.md`](docs/AI_ADAPTER_CONTRACT.md) — wire spec for
  adapter authors.
- [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md) — threat
  model, control mapping, and the academic paper that informs the architecture
  ([Zenodo DOI 10.5281/zenodo.17261761](https://doi.org/10.5281/zenodo.17261761)).
- [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) — paper §3 → §4 → code mapping
  plus framework alignment table (CISA Secure-by-Design, NIST CSF 2.0,
  NIST AI RMF, ISO/IEC 27001, ETSI EN 303 645, GDPR, India's DPDP Act).
  Procurement-grade evidence trail.
- [`docs/GOVERNMENT_DEPLOYMENT.md`](docs/GOVERNMENT_DEPLOYMENT.md) — printable
  procurement one-pager. FCC Covered List substitution argument plus the
  "operational sovereignty — your AI, your tactics, your hardware" framing
  for defence / critical-infrastructure / regulated deployments.
- [`docs/USE_CASES.md`](docs/USE_CASES.md) — per-industry fit guide for
  12 segments (critical infra, defence, government, healthcare, education,
  industrial, logistics, retail LP, cannabis compliance, construction,
  aviation / maritime / ports, smart city) with honest scope caveats.
- [`docs/COMPARISONS.md`](docs/COMPARISONS.md) — honest head-to-head with
  Frigate, ZoneMinder, Shinobi, Viseron, and Verkada. Acknowledges what
  each does well before stating where OpenNVR fits differently.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what's shipped, v0.2 plans
  (YOLOv11, CLIP semantic search, pose, audio events, AD/SAML SSO,
  multi-host, TelemetrySource), v0.3+ direction (go2rtc evaluation,
  federated AI, hardware trust anchors).
- [`docs/SUPPORT.md`](docs/SUPPORT.md) — community support channels and
  commercial-support tiers (deployment, custom adapters, compliance
  evidence packs, SLA, sponsored development) via
  [contact@cryptovoip.in](mailto:contact@cryptovoip.in).
- [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) — bare-metal developer setup.
- [`docs/DOCKER_SETUP.md`](docs/DOCKER_SETUP.md) — Docker-only path.
- Per-example `README.md` files documenting each reference application.

### Security

OpenNVR was rebuilt to close every systemic IP-camera weakness documented in
recent academic work on networked surveillance. The defaults are deliberately
strict; every relaxation is an explicit operator decision recorded in the
audit log.

- **No shipped default password.** The admin account activates via a one-time
  setup token printed to stdout on first boot. The token is consumed on first
  successful use; a fresh one is minted on every restart that finds a pending
  user.
- **Strong-secret validators.** The server refuses to boot if `SECRET_KEY`,
  `MEDIAMTX_SECRET`, `INTERNAL_API_KEY`, or `CREDENTIAL_ENCRYPTION_KEY` is
  shorter than 32 characters or matches a placeholder pattern.
- **Camera credentials encrypted at rest** under a Fernet key the validator
  refuses to accept if it's a placeholder.
- **Offline mode by default.** Cloud-touching routes (cloud recording, cloud
  AI inference, federated streams) return 403 unless `DEPLOYMENT_MODE` is
  explicitly set to `hybrid` or `cloud`. The deviation is audit-logged at
  boot and surfaced at `/api/v1/system/posture`.
- **AI sovereignty enforcement.** KAI-C refuses to register adapters that
  declare `network_egress` permissions under the default
  `AI_SOVEREIGNTY=local_only` policy.
- **MediaMTX bound to loopback by default.** Cloud-style `0.0.0.0` binds are
  rejected by the boot validator.
- **Path-traversal hardening** on recording storage. `..` and absolute paths
  are refused server-side; symlinks resolve within the configured root.
- **RTSPS, HLS-TLS, and WebRTC-TLS on by default.** MediaMTX refuses to start
  without `server.crt` / `server.key`; the bundled `mediamtx-certs-init`
  service generates a 10-year self-signed pair on first boot if none exists.
  Plaintext outputs require `MEDIAMTX_ALLOW_PLAINTEXT_OUTPUTS=true`, which is
  recorded in the boot audit log.
- **Per-camera transport-security policy.** Each camera carries a
  `transport_security` field — `rtsps_required` / `rtsps_preferred` /
  `plaintext_allowed`. RTSPS reachability is probed on add; operators can
  override per-camera. Stream provisioning refuses plaintext for
  `rtsps_required` cameras across every code path that touches stream config.
- **Account lockout** after repeated failed logins, with a clear feedback
  message and a 180-second cool-down.

### Fixed

- **TLS certs are now auto-configured for the operator's actual
  LAN IP (ISSUE-6 v9).** Previously, `nginx-certs-init` and
  `mediamtx-certs-init` baked the SAN list with only loopback
  addresses unless the operator manually set `OPENNVR_HOST_IP` in
  `.env`. Result: a phone visiting `https://192.168.1.100/` got
  TWO browser warnings — self-signed CA AND CN/IP mismatch — and
  WebRTC ICE could fail because the host advertised had no
  matching cert. Now `start.sh`'s `configure_nginx_bind_host`
  exports `OPENNVR_HOST_IP` automatically based on the same
  detection used for `NGINX_BIND_HOST` (uplink IP in dual-NIC,
  detected LAN IP in single-LAN) — but never overrides an
  operator-set value, so explicit `.env` choices still win. Both
  cert-init containers consume the env var via compose
  interpolation and add it to their SAN lists. mediamtx-certs-init
  also gained OPENNVR_HOST_IP support for parity (direct RTSPS via
  VLC/ffmpeg at `rtsps://<host-ip>:8322` now sees a matching
  cert). On first boot the cert SAN includes the LAN IP
  automatically; the browser warning shrinks to just the
  self-signed-CA one-click bypass.
  
  Also added a new `./start.sh refresh-certs` subcommand for the
  "my LAN IP changed" case (DHCP renewal, moved the box, switched
  networks): stops nginx + mediamtx, deletes `./nginx-certs/` and
  `./mediamtx-certs/`, re-runs `configure_nginx_bind_host` to
  re-detect the current IP, brings the stack back up so the init
  containers regenerate certs with the new SAN. Confirmation
  prompt on TTY; non-interactive (CI / scripted) skips the
  prompt. 11 new tests under
  `tests/host-hardening/test_cert_san.sh` cover every contract —
  both init scripts read OPENNVR_HOST_IP, both add it to the SAN
  at runtime (verified by stubbing openssl and inspecting the
  args), start.sh exports OPENNVR_HOST_IP in every NIC path with
  the operator-value-wins guard, and the refresh-certs subcommand
  is wired into the usage help.

- **Live streams and recording playback now work from any LAN
  device, end-to-end through nginx (ISSUE-6 v8).** The UI loaded
  fine on LAN after v0.1.0's nginx work, but the `<video>` element
  would refuse to play because the token endpoint emitted
  `http://127.0.0.1:8889/...` for WebRTC and `http://127.0.0.1:8888/...`
  for HLS — both unreachable from a phone or laptop, and blocked
  by browser mixed-content policy anyway (HTTPS UI → HTTP fetch).
  Six coordinated changes:
  
  (1) `nginx/opennvr.conf` adds three new proxy locations — `/webrtc/`
  → mediamtx:8889 for WHEP signalling, `/hls/` → mediamtx:8888 for
  live HLS, `/playback/` → mediamtx:9996 for recording playback.
  Each carries `X-Forwarded-Proto https` so MediaMTX knows the
  original request was TLS.
  
  (2) `docker-compose.tier0.yml` changes the three
  `MEDIAMTX_EXTERNAL_*` env vars on opennvr-core to
  `${MEDIAMTX_PUBLIC_URL:-https://localhost}/webrtc`,
  `/hls`, and `/playback` respectively. The token endpoint
  (`/api/v1/streams/{id}/info`) now emits same-origin HTTPS URLs
  the browser can actually fetch.
  
  (3) `start.sh` exports `MEDIAMTX_PUBLIC_URL` and
  `MEDIAMTX_WEBRTC_HOSTS` based on the operator's declared NIC
  topology — uplink IP for dual-NIC, detected LAN IP for
  single-LAN. These propagate to opennvr-core and mediamtx via
  compose interpolation.
  
  (4) `docker-compose.tier0.yml` publishes mediamtx's WebRTC ICE
  port `8189` (UDP and TCP) on `${NGINX_BIND_HOST:-0.0.0.0}`, so
  the DTLS-SRTP media path is reachable on the same NIC the UI is
  bound to. Dual-NIC operators still get camera-LAN isolation:
  the camera VLAN cannot reach the WebRTC media port either.
  
  (5) MediaMTX's `webrtcAdditionalHosts` is wired via the
  `MTX_WEBRTCADDITIONALHOSTS` env var so the SDP answer advertises
  the right ICE candidate for LAN browsers to discover the UDP
  media path.
  
  (6) **Real bug fix:** `server/routers/recordings.py:get_playback_url`
  was returning `settings.mediamtx_playback_url` (the Docker-bridge
  internal URL `http://mediamtx:9996/...`) directly to the
  browser, which broke recording playback on LAN clients exactly
  the same way the live-stream URLs were broken. Now uses the
  same `mediamtx_external_playback_url → mediamtx_playback_url →
  hardcoded default` fallback chain that `streams.py` uses.
  
  Security model unchanged: the nginx → mediamtx hop is over the
  Docker bridge (V-015 trust zone, RFC1918), same profile as
  nginx → opennvr-core. WebRTC media is DTLS-SRTP-encrypted
  end-to-end and only usable with a JWT-authenticated SDP session.
  The 8189 UDP/TCP port published on the LAN-facing NIC is the
  smallest exposure increment — anyone who could already POST
  through nginx's WHEP signalling can already reach this port; no
  new attack surface in dual-NIC mode either, because the publish
  is on `NGINX_BIND_HOST` (uplink only). 13 new tests under
  `tests/host-hardening/test_media_proxy.sh` cover every contract
  (proxy locations exist, point at the right backend ports, carry
  TLS forwarded-proto, env vars wire correctly through to compose,
  UDP/TCP both published, recordings.py uses external chain, no
  regression on opennvr-core's loopback-only binding).

- **Docker bridge subnets are now pinned for a deterministic trust
  zone (ISSUE-6 v7).** `docker-compose.tier0.yml`'s
  `opennvr_internal` bridge and `docker-compose.yml`'s
  `sentinel_internal` + `public_uplink` bridges previously relied
  on Docker's automatic subnet assignment (typically `172.18.0.0/16`
  or `172.19.0.0/16`, picked at runtime). Now explicitly declared
  via ipam config: `sentinel_internal` / `opennvr_internal` →
  `172.28.0.0/16`, `public_uplink` → `172.29.0.0/16`. Both are
  well inside V-015's trust zone (RFC1918 `172.16/12`) and sit
  outside the address ranges consumer routers commonly use
  (`192.168.0.0/16`, `10.0.0.0/8`), removing a class of "Docker
  picked a subnet that collides with my home LAN" deployment
  headaches. Operators with an existing LAN on `172.28/16` can
  override via the `OPENNVR_DOCKER_SUBNET` /
  `OPENNVR_PUBLIC_SUBNET` env vars (documented in `.env.example`).
  No behaviour change for V-015 — the validator already accepted
  the entire `172.16/12` range. Covered by 8 new tests in
  `tests/host-hardening/test_docker_subnets.sh` that verify the
  pin, RFC1918 membership, env-var override interpolation, and
  non-overlap with each other and with consumer LAN defaults.

- **Network setup walkthrough rewritten in plain English with a
  "not sure — pick the safe default" option (ISSUE-6 v6).** The
  NIC-topology menu is now compact (~10 lines instead of 20+) and
  speaks to non-technical operators: option `1) Simple` for "one
  network for cameras, phone, and computer" (most home/small-
  office setups), option `2) Advanced` for "cameras on a separate
  network" (needs two cables or a managed switch), and option
  `3) Not sure` which silently picks Simple. The numeric choices
  are aliases for the previous `s/d/l` letters, both still work.
  The single-LAN security warning got reframed from a scary
  "trust-mode breach scenario" into an informational `ℹ Simple
  network setup` block whose top-line advice — "change every
  camera's default password before you connect it. That's how
  most home cameras get hacked." — is the single most actionable
  mitigation a non-tech user can do, beating "set up dual-NIC"
  on impact-per-effort. Banner header softened from "Security
  posture — limitations to be aware of" to "Heads up". Dual-NIC
  and legacy-flag warnings remain technical for the advanced
  audience that hits them. **Security model unchanged** — the
  detection logic, the bind decisions, the hardening offer, and
  the test contracts (30 tests, all green) are identical; only
  the operator-facing wording changed.

- **Boot-time security-posture banner flags degraded
  configurations on every run (ISSUE-6 v5).** `./start.sh
  up/build` now prints a one-time, structured security banner
  after the access URLs whenever the deployment is in a state
  that an operator should know about. Currently detects: (a)
  single-LAN trust mode — cameras and operators share one
  network, compromised camera can reach the UI; (b) dual-NIC
  declared but the kernel-level forward-drop hardening hasn't
  been applied — nginx is bound correctly but a compromised
  camera could still potentially pivot through the host; (c)
  legacy `ALLOW_REMOTE_MEDIAMTX` env var still present in
  `.env` or shell — silently ignored by V-015 but the operator
  may still think they have an escape hatch. Each warning
  includes a one-line "Mitigation:" pointing at the exact
  command or doc that fixes it. Silent when the posture is
  clean — no noise on fully-hardened deployments. Hardening
  state is detected via the `./host-hardening/snapshot-active`
  symlink that the apply/revert scripts maintain. Covered by
  8 unit tests in `tests/host-hardening/test_security_posture.sh`.

- **Multi-NIC topology now gets an interactive walkthrough and
  optional paper-compliant host hardening (ISSUE-6 v3).** When
  `./start.sh up` runs on a multi-NIC host with no topology
  declared AND has a TTY, the operator gets a menu: single-LAN
  (writes `NGINX_BIND_HOST=0.0.0.0` to `.env`), dual-NIC
  (prompts for which NIC is camera-LAN and which is uplink, writes
  both `CAMERA_NETWORK_INTERFACE` and `MGMT_NETWORK_INTERFACE` to
  `.env`), or "later" (skips, asks again next boot). Same-NIC-for-
  both-sides is rejected without persisting anything. Selecting
  dual-NIC additionally offers `./scripts/apply-camera-vlan-
  hardening.sh` — a one-shot script that adds an `inet opennvr-vlan`
  nftables table with forward-chain drop rules between the two
  declared NICs, so a compromised camera cannot pivot through the
  OpenNVR host to reach LAN devices on the uplink side (and vice
  versa). The hardening script is the *only* part of the install
  path that requests sudo, surfaces every nftables command before
  running, snapshots existing state to
  `./host-hardening/snapshot-<timestamp>/`, and is reversible via
  `./scripts/revert-camera-vlan-hardening.sh` which removes only
  the dedicated table (existing firewall rules from UFW, firewalld,
  fail2ban, or bare iptables stay untouched). Non-interactive runs
  (CI, scripted) keep the previous `0.0.0.0`+warning fallback. The
  hardening intentionally does NOT modify routing, DNS, or
  input/output filtering — those are tracked separately under
  V-010/V-016/V-017 and need their own operator-consent workflow.

- **NIC topology is now auto-detected and nginx binds to the
  management NIC in dual-homed deployments (ISSUE-6 v2).**
  `start.sh` enumerates the host's routable IPv4 interfaces before
  `docker compose up -d` and configures `NGINX_BIND_HOST` based on
  what it finds. Single-NIC hosts get `0.0.0.0` (the only NIC there
  is). Dual-NIC hosts with `CAMERA_NETWORK_INTERFACE` +
  `MGMT_NETWORK_INTERFACE` declared in `.env` get nginx bound to the
  management NIC's IPv4 only — a compromised camera on the camera
  VLAN physically cannot probe the management UI. Multi-NIC hosts
  without declared topology fall back to `0.0.0.0` but print a loud
  warning so the operator knows they're not getting paper-compliant
  isolation. VLAN-tagged sub-interfaces (`eth0.10`, `eth0.20`) are
  treated identically to physical NICs by the detection logic, so
  single-physical-NIC deployments with a managed VLAN-aware switch
  get the same isolation as true dual-NIC. `start.sh` refuses to
  boot if a declared `MGMT_NETWORK_INTERFACE` has no IPv4 address
  (typo guard).

- **OpenNVR UI is now reachable from the LAN over HTTPS (ISSUE-6).**
  Previously opennvr-core's host port was bound to `127.0.0.1:8000`,
  so the UI was reachable from the host machine but refused
  connections from any phone, tablet, or laptop on the same LAN. The
  binding was a deliberate Secure-by-Default choice (cleartext JWT
  must never traverse the LAN), but it left operators with no
  built-in way to actually *use* the product from the device they
  carry. The Tier 0 docker-compose now ships an nginx TLS reverse
  proxy that terminates a self-signed cert on `0.0.0.0:443` (and
  redirects `0.0.0.0:80`) and proxies to opennvr-core over the
  Docker internal bridge. The bridge hop is already inside V-015's
  trust zone (RFC1918), so the security model is preserved — JWT now
  travels over TLS on the LAN segment, and never crosses an
  untrusted boundary in cleartext. Cert SAN list covers
  `localhost`, `opennvr`, `opennvr.local`, `127.0.0.1`, `::1` by
  default; set `OPENNVR_HOST_IP=192.168.x.y` in `.env` to add your
  server's LAN IP and silence the CN/IP-mismatch warning (browsers
  still warn once about the self-signed CA — one-click "Accept the
  risk and continue"). `NGINX_BIND_HOST` lets operators restrict
  the public edge to a specific NIC. opennvr-core's host port
  remains pinned to `127.0.0.1:8000` for host-side debugging.
  start.sh now surfaces the LAN URL after boot, with auto-detection
  of the server's first non-loopback IPv4. **Known follow-up
  (task #11):** live-view (HLS/WebRTC) is currently not proxied
  through nginx, so browsers viewing from LAN will refuse the
  mixed-content stream URLs. That's tracked separately because it
  requires a coordinated change to `MEDIAMTX_EXTERNAL_*` defaults.

- **First-time setup token now surfaces reliably on slow first boot
  (ISSUE-5).** Previously the `start.sh` / `start.ps1` helper polled
  `docker compose logs opennvr-core` for the setup-token banner for
  30 seconds after `compose up -d` returned. With the ISSUE-3 fix in
  place, `yolov8-weights-init` now exports the ONNX model before
  opennvr-core starts (≈3 min on x86, ≈10–15 min on a Pi 5), so the
  30-second window almost always missed the banner on slow hardware
  and printed a misleading "either the admin is already activated or
  the server is still starting" fallback. The helper now waits for
  opennvr-core's Docker healthcheck to pass first — with periodic
  progress messages so the operator isn't staring at a silent
  terminal — then extracts the banner from the logs. The fallback
  message is now disambiguated: once the container is healthy and
  there's no banner, that unambiguously means the admin is already
  activated, and the message points the operator at the login URL.
  Timeout extended to 20 minutes (overridable via
  `OPENNVR_SETUP_TOKEN_MAX_WAIT_S` for testing). All three branches
  smoke-tested against a stubbed `docker`.

### Changed

- **V-015 MediaMTX bind enforcement is now trust-zone-aware (ISSUE-4).** The
  previous loopback-only check refused to boot in Tier 0 docker-compose
  because `MEDIAMTX_BASE_URL=http://mediamtx:8889` resolves to the Docker
  bridge's RFC1918 address. The validator now accepts every address that
  is unreachable from the public internet — loopback (127/8, ::1), RFC1918
  (10/8, 172.16/12, 192.168/16, covering Docker bridges and camera LANs),
  IPv6 ULA (fc00::/7), and IPv4/IPv6 link-local — and refuses public IPs,
  public FQDNs, scheme-less URLs, and the `0.0.0.0` wildcard bind. The
  two-NIC framing in `docs/SECURITY_ARCHITECTURE.md` §2.2 makes the scope
  explicit: V-015 polices the *ingress* MediaMTX URLs (camera LAN side,
  plaintext RTSP/HTTP); the *egress* side (browser-facing HLS/WebRTC over
  the uplink NIC, behind a TLS reverse proxy) uses `MEDIAMTX_EXTERNAL_*`,
  which is deliberately scoped out of this validator. **Breaking change
  for any operator who set `ALLOW_REMOTE_MEDIAMTX=true`:** the flag has
  been removed. The previous escape hatch silently let unencrypted
  MediaMTX traffic cross the trust boundary, which voids the paper's
  Secure-by-Design guarantee. Operators with a real cross-trust-boundary
  requirement must terminate TLS in a reverse proxy and configure
  `MEDIAMTX_EXTERNAL_*` for the public URLs instead. Stale
  `ALLOW_REMOTE_MEDIAMTX` entries in operator `.env` files are now
  silently ignored (Pydantic `extra="ignore"`).
- Migrated FastAPI lifecycle from the deprecated `@app.on_event("startup")` /
  `("shutdown")` decorators to the lifespan async context manager pattern.
  Same behaviour; no more deprecation warnings in test runs.
- **Tier 0 YOLOv8 weights provisioning** now derives the ONNX from the
  upstream `.pt` checkpoint at first boot rather than fetching a pre-built
  ONNX. Ultralytics retired both prior URLs (HuggingFace gated behind an
  allowlist; the `v8.3.0` release `yolov8n.onnx` asset returned 404), so the
  `yolov8-weights-init` container now downloads `yolov8n.pt` from
  `github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt`,
  pip-installs `ultralytics==8.3.40`, and exports to ONNX with
  `opset=12 imgsz=640 simplify=False` (the adapter-conformance-tested
  shape). First-boot wall time adds ~3 min on x86 and ~10–15 min on a
  Raspberry Pi 5; cached on the `opennvr_yolov8_weights` volume so
  subsequent boots are instant. Operators with a fine-tuned model or a
  private mirror can set `YOLOV8_WEIGHTS_URL` in `.env` to skip the export
  entirely and curl their own pre-built ONNX. A v0.1.1 follow-up will
  publish `ghcr.io/open-nvr/yolov8-weights` so the default case becomes
  instant too.

### License

OpenNVR is licensed under [GNU Affero General Public License v3.0](LICENSE).
The AGPL is intentional: it ensures the sovereignty story stays intact — any
service built on OpenNVR, even one offered over a network, must share its
modifications openly. Commercial licensing is available — see the contact
address in the README.

---

[Unreleased]: https://github.com/open-nvr/open-nvr/compare/...HEAD
