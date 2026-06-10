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

- **V-022 sovereignty validator accepts Docker bridge URLs
  (ISSUE-28).** Operator on a tier0 deploy hit:
  
  ```
  RuntimeError: V-022: AI_SOVEREIGNTY=local_only requires every
  adapter URL to be loopback. The following adapters violate
  that policy:
    - default='http://yolov8-adapter:9002' (host=yolov8-adapter)
  ```
  
  kai-c refused to start. Root cause: the validator was written
  in the host-networking era when all adapter URLs were
  `127.0.0.1:<port>`. Tier 0 ships with bridge networking + Docker
  service-DNS URLs (`http://yolov8-adapter:9002`), and the
  loopback-only check rejected them. But Docker bridge networks
  are confined to a single physical host — packets between
  containers stay inside the kernel networking stack and never
  reach the NIC. Bridge URLs are **equally "on this machine"**
  for sovereignty purposes; the validator was just narrowed too
  aggressively.
  
  Fix in `kai-c/main.py`:
  
  * `_host_is_loopback` → renamed to `_host_is_on_this_machine`
    with the old name preserved as a back-compat alias.
  * The check now accepts: loopback IPs + hostnames AND any
    address that resolves into `OPENNVR_DOCKER_SUBNET` (default
    `172.28.0.0/16`, operator-overridable). Continues to reject:
    non-bridge RFC1918 (peer hosts on the LAN), public IPs,
    `0.0.0.0` wildcard.
  * Error message updated to name the bridge subnet explicitly
    so operators who DO hit a legitimate violation (e.g. an
    `ADAPTER_URL` pointing at a peer VM) know what URLs are
    accepted.
  
  Acceptance matrix:
  
  | URL | Old | New | Why |
  |---|---|---|---|
  | `http://localhost:9100` | ✓ | ✓ | loopback |
  | `http://127.0.0.1:9100` | ✓ | ✓ | loopback IPv4 |
  | `http://[::1]:9100` | ✓ | ✓ | loopback IPv6 |
  | `http://yolov8-adapter:9002` | ✗ | ✓ | Docker bridge (NEW — tier0) |
  | `http://172.28.0.7:9002` | ✗ | ✓ | direct bridge IP (NEW) |
  | `http://192.168.1.50:9100` | ✗ | ✗ | peer host on LAN |
  | `http://api.openai.com` | ✗ | ✗ | public |
  
  New regression test
  `tests/host-hardening/test_v022_sovereignty_docker_bridge.sh`
  (4 tests) locks the new contract — the renamed function
  exists, its logic handles the 7-case acceptance matrix above,
  the error message references the bridge subnet, and tier0.yml
  ships an `ADAPTER_URL` value that passes the validator.
  
  V-022's stronger claim is unchanged: AI inference still must
  not leave this physical box. The fix just acknowledges that
  Docker bridge networking is "this box."

- **`init_db.py` refuses known-bad DEFAULT_ADMIN_PASSWORD at
  runtime (ISSUE-27 — defense in depth for ISSUE-26).** The
  ISSUE-26 fix blanked `.env.example`'s hardcoded password line,
  but operators with existing `.env` files (created before the
  fix, copied from a tutorial, or from an AI-generated template)
  still arrive at boot with `DEFAULT_ADMIN_PASSWORD=SecurePass123!`.
  init_db.py would honor it as a "provisioned bootstrap" — same
  V-001 violation, just at a different layer.
  
  Defense-in-depth: init_db.py now has a `KNOWN_BAD_PASSWORDS`
  reject list. If `settings.default_admin_password` matches one
  of these (case-insensitive), init_db.py:
  
  1. Prints a loud framed warning to stderr explaining the
     refusal and pointing the operator at the fix (blank the
     value in `.env`).
  2. Sets `initial_password = ""` and falls through to the
     existing setup-token flow.
  3. The admin is seeded with `password_set=False` and the
     setup-token banner prints normally.
  
  The reject list ships with the historical .env.example default
  plus the common weak-password checklist (admin, password,
  changeme, letmein, opennvr, default, 12345, 123456, qwerty).
  Extending the list is a one-line edit.
  
  Test 4 in
  `tests/host-hardening/test_env_example_no_default_creds.sh`
  AST-parses init_db.py, locates the `KNOWN_BAD_PASSWORDS`
  frozenset, and asserts it contains at minimum `securepass123!`,
  `admin`, `password`, `changeme`. So a future PR can't
  accidentally remove these entries without the test failing.
  
  Operator recovery on existing deploys (independent of the
  source fix):
  
  ```bash
  # Blank the value in YOUR .env, not just the template
  sed -i 's/^DEFAULT_ADMIN_PASSWORD=.*/DEFAULT_ADMIN_PASSWORD=/' .env
  
  # Wipe the DB volume (admin row currently has password_set=True)
  ./start.sh down
  docker volume rm $(docker volume ls -q | grep opennvr_db_data)
  
  # Restart — token banner will print
  ./start.sh up
  ```
  
  After this commit lands, even operators who don't blank their
  `.env` will get the warning + setup-token flow at next boot.

- **`.env.example` no longer ships a hardcoded admin password
  (ISSUE-26 — V-001 violation).** The `.env.example` template
  shipped with `DEFAULT_ADMIN_PASSWORD=SecurePass123!` as a literal
  value. Every operator who ran `cp .env.example .env` (or whose
  install wizard inherited template defaults) silently ended up
  with that exact known credential as their admin password.
  `init_db.py`'s "provisioned bootstrap" code path then seeded the
  admin user with `password_set=True` and that password, bypassing
  the one-time setup-token flow entirely.
  
  Concrete impact path:
  
  1. Operator does `cp .env.example .env` (or the install wizard
     does it equivalent under the hood).
  2. `.env` now contains `DEFAULT_ADMIN_PASSWORD=SecurePass123!`.
  3. opennvr-core boots, `init_db.py` reads
     `settings.default_admin_password`, sees it's truthy, seeds
     `admin` with `password_set=True` and that exact password.
  4. `maybe_arm()` correctly skips minting a token (admin is
     already activated).
  5. `start.sh up`'s banner-grep finds no token in the logs and
     prints *"First-time setup is already complete"* — the
     operator has no idea what credential they got.
  6. The operator hits the UI and either gets the setup screen
     (cached frontend from before init_db ran) or the login
     screen, and is stuck because they were never told the
     password is the literal `.env.example` default.
  
  This is the V-001 / ETSI EN 303 645 "unique credential" anti-
  pattern OpenNVR's threat model positions itself against.
  Shipping it in the source template is the worst possible form
  of it.
  
  Fix:
  
  * `.env.example` now ships `DEFAULT_ADMIN_PASSWORD=` (empty).
    The accompanying comment explains the two paths clearly:
    *empty → secure-by-default setup-token flow* (the recommended
    path); *set to a value → provisioned bootstrap from a secrets
    manager* (the unattended-deploy path).
  * New regression test
    `tests/host-hardening/test_env_example_no_default_creds.sh`
    (3 tests) locks the class of bug:
    
    1. `.env.example`'s `DEFAULT_ADMIN_PASSWORD` line must be
       blank — anything after the `=` triggers a failure.
    2. No `docker-compose*.yml` may pin
       `DEFAULT_ADMIN_PASSWORD` to a literal value (catches the
       bug re-introduced at the compose layer).
    3. `scripts/install.sh` may not write a literal
       `DEFAULT_ADMIN_PASSWORD` value into the `.env` it
       generates (catches the bug re-introduced at the
       installer layer).
  
  Operators on existing deploys: if you find yourself stuck on
  the setup screen with `password_set=True` for admin in your DB,
  your password is whatever `.env`'s `DEFAULT_ADMIN_PASSWORD`
  was set to during install — most likely `SecurePass123!` from
  the old template. Log in with that, then immediately change
  it in the UI (Settings → Account → Change Password).
  
  Net: 116 host-hardening tests across 12 suites, all green
  (after `git add` of the two new test files).

- **kai-c no longer crashes on import (ISSUE-24).** Operator
  reported the kai-c middleware crash-looping inside the
  opennvr-core container with:
  
  ```
  File "/app/kai-c/kai_c/registry.py", line 46, in <module>
      import httpx
  ModuleNotFoundError: No module named 'httpx'
  ```
  
  Root cause: `kai-c/pyproject.toml` declared httpx in
  `[dependency-groups].dev` with a stale comment claiming
  *"Runtime KAI-C uses `requests` (sync) + `websockets` (async) —
  no httpx in the live path"*. That comment was aspirational,
  not accurate. Production code in `kai_c/registry.py` imports
  httpx at module top-level (line 46) AND constructs
  `httpx.AsyncClient(trust_env=False)` at runtime (line 198)
  for adapter capabilities probing + health checks (lines 143,
  150). The Dockerfile correctly runs `uv sync --no-dev` which
  skipped the misplaced dep, leading to the production crash.
  
  Fix:
  
  * Move `httpx>=0.27,<1.0` from `[dependency-groups].dev` to
    `[project].dependencies` in `kai-c/pyproject.toml`. Replace
    the misleading comment with a correct one tied to ISSUE-24.
  * New regression test
    `tests/host-hardening/test_kai_c_runtime_deps.sh` that walks
    every `.py` file under `kai-c/kai_c/` (production code, not
    tests), parses the AST, and asserts every top-level `import X`
    and `from X import Y` has a corresponding entry in
    `[project].dependencies`. Stdlib modules and known
    bundled-via transitive deps (e.g. starlette via fastapi) are
    whitelisted. The line-scanner parser handles the edge case
    where a comment inside the dependencies list contains a
    literal `]` character (e.g. our own `[dependency-groups]`
    reference) — a naive non-greedy regex would stop there and
    silently drop later entries.
  * Plus a positive contract test that pins httpx specifically
    so a future "remove the httpx import line" PR can't satisfy
    test 1 vacuously while leaving the runtime paths broken.
  
  Operator recovery (immediate, while waiting for the GHA
  rebuild of `:latest`):
  
  ```bash
  # Install httpx into the running opennvr-core's kai-c venv
  docker compose exec opennvr-core /app/kai-c-venv/bin/pip install httpx
  docker compose restart opennvr-core
  ```
  
  After this commit lands + the GHA publish-images workflow
  rebuilds `ghcr.io/open-nvr/core:latest`, `docker compose pull
  opennvr-core` picks up the proper fix.
  
  Total: 113 host-hardening tests across 11 suites, all green.

- **nginx no longer crash-loops on "host not found in upstream
  mediamtx" (ISSUE-21).** Operator switching from
  `docker-compose.linux.yml` (host mode) to
  `docker-compose.tier0.yml` (bridge mode) reported nginx
  endlessly restarting with:
  
  ```
  [emerg] host not found in upstream "mediamtx" in
  /etc/nginx/conf.d/default.conf:89
  ```
  
  Root cause: nginx's `opennvr.conf` uses
  `proxy_pass https://mediamtx:8889/` (and `:8888`, `:9996`) —
  literal hostnames that nginx resolves at CONFIG LOAD time, not
  at request time. The tier0 compose listed nginx's
  `depends_on:` as `nginx-certs-init` + `opennvr-core` but not
  mediamtx itself. opennvr-core's healthcheck happens to wait
  on mediamtx (via its own `depends_on`), so on a clean boot
  the chain wins the race by accident. On recovery boots —
  post-compose-file switch with stale Docker networks confusing
  bridge DNS — nginx loses the race and every restart attempt
  hits the same config-parse error.
  
  Fix: add `mediamtx: service_healthy` to nginx's `depends_on`
  in `docker-compose.tier0.yml`. New regression test #18 in
  `tests/host-hardening/test_media_proxy.sh` parses the compose
  and asserts the dependency + condition shape, so a future
  refactor can't accidentally remove it.
  
  Operator recovery (one-time, for boxes already wedged in the
  crash-loop state):
  
  ```bash
  ./start.sh down
  docker compose -f docker-compose.tier0.yml down --remove-orphans
  docker network prune -f
  docker container prune -f
  ./start.sh up
  ```
  
  Plus the new `depends_on` makes future starts deterministic so
  the recovery dance won't be needed again.

- **CI pytest test_m1b_mediamtx_hardening.py fixed for the
  ISSUE-17 include shim (ISSUE-20).** The pytest
  `test_compose_has_mediamtx_certs_init_service` walked
  `docker-compose.yml` directly and asserted
  `mediamtx-certs-init` was in the `services:` block. After
  ISSUE-17 made `docker-compose.yml` a thin `include:` shim
  pointing at `docker-compose.tier0.yml`, the pytest saw an
  empty services dict and CI failed:
  
  ```
  AssertionError: docker-compose.yml is missing the
  mediamtx-certs-init service
  assert 'mediamtx-certs-init' in {}
  ```
  
  Same class of bug as the shell tests fixed in ISSUE-17 — a
  pytest that hard-referenced the canonical filename without
  following the include indirection. Fix is symmetric:
  
  * `test_compose_has_mediamtx_certs_init_service` now walks
    `docker-compose.tier0.yml` (the implementation file where
    the services actually live). Test intent is preserved —
    the canonical compose lifecycle must include cert-init
    before mediamtx — it just follows the post-ISSUE-17 file
    layout.
  * **New pytest** `test_canonical_docker_compose_yml_is_include_shim`
    locks the include shape: `docker-compose.yml` must contain
    an `include` directive that references
    `docker-compose.tier0.yml` AND must NOT have its own
    `services:` block. A stray services block would shadow
    the include and silently give bare-invocation operators a
    different stack from the `-f tier0.yml` operators.
  
  This mirrors the contract test in
  `tests/host-hardening/test_build_resilience.sh` so both the
  pytest layer and the shell-test layer enforce the same
  property structurally.

- **Host-hardening tests run on macOS (ISSUE-19).** First Mac
  contributor ran the test suite, hit two compatibility bugs the
  Linux-only CI never caught:
  
  * **`test_media_proxy.sh` used `declare -A`** for an associative
    array — bash 4+ syntax that macOS's bundled `/bin/bash` 3.2
    doesn't understand. Apple stopped updating bash after the
    GPLv3 switch in 2007, so every Mac without Homebrew bash
    failed with `webrtc: unbound variable`. Rewritten as a
    `port_for()` case statement that's bash 3.2 compatible.
  
  * **Five tests crashed with `ModuleNotFoundError: yaml`** when
    PyYAML wasn't installed on the system python3 — Apple's
    bundled Python doesn't ship it, and most Mac contributors
    don't think to install it because nothing tells them.
  
  Fix: new shared library `tests/host-hardening/_lib.sh` with
  two helpers, sourced by every test that needs them:
  
  * `require_python_yaml` — bails early with the exact install
    command (covers `pip3`, Homebrew Python, system Python,
    Debian/Ubuntu `apt`). Operators see a clear actionable
    message instead of an opaque Python traceback.
  * `require_bash_4` — same shape, for tests that genuinely
    need bash 4+ features. Currently unused — every test should
    be 3.2 compatible — but available if a future test has no
    way around it.
  
  Wired into all five yaml-using tests:
  test_build_resilience.sh, test_cert_san.sh,
  test_compose_file_selection.sh, test_docker_subnets.sh,
  test_media_proxy.sh. The other four suites need neither
  helper. Net: 110 tests across 10 suites, all green on Linux
  and macOS (with PyYAML installed).
  
  Also extended `test_script_permissions.sh`'s expected-suites
  list to include the new `_lib.sh` shared helper plus the
  three new test files this session added — so the ISSUE-9
  tracking-regression check catches any of them being
  un-staged before commit.

- **README quickstart tightened for first-time developers
  (ISSUE-18).** The previous quickstart had a dense 4-bullet
  paragraph explaining what `./start.sh up` does, the
  "Skip the wizard" section still referenced the obsolete
  `docker compose -f docker-compose.tier0.yml up -d` pattern,
  and the camera-agent section had verbose dual-`-f` commands
  that were hard to scan. The new structure (still ~one screen
  of content):
  
  * Three commands, headline-form.
  * **"What you'll see when it boots"** code block — visual
    mock-up of the printed NIC topology, URL, and token banner
    so operators know what to expect before they run it.
  * **Three numbered next steps** — open URL, accept cert, paste
    token. One line each.
  * **Common follow-ups table** — lost token, IP changed, stop,
    logs, status, upgrade. Six rows, one command per row.
  * **Advanced setup** subsection — bare `docker compose up -d`
    now mentioned (ISSUE-17 made it work) with the trade-offs
    listed.
  * **Talk to your cameras** rewritten with a "what you can ask"
    table showing the four reference questions and which adapter
    each one calls — concrete + scannable. Commands shortened to
    use `docker-compose.yml` instead of `docker-compose.tier0.yml`
    now that the include shim makes them equivalent.
  
  No content removed from the project positioning sections ("Why
  this exists", "What makes it different") — those carry the
  project's voice and aren't part of the friction the user was
  flagging.

- **`docker-compose.yml` is now the canonical entry point —
  bare `docker compose up -d` works (ISSUE-17).** Five compose
  files is confusing for new developers and for operators who
  expect the standard Docker convention (`docker compose up -d`
  in the repo root just works). Cleanup pass:
  
  * **`docker-compose.yml` is now a thin `include:` shim**
    pointing at `docker-compose.tier0.yml`. Operators get the
    canonical hardened stack with bare `docker compose up -d` —
    no `-f` flag needed. Editing tier0.yml automatically updates
    both forms, so there is no copy of service definitions to
    fall out of sync. Requires Docker Compose v2.20+ (Aug 2023+).
  * **`docker-compose.tier0.yml` remains as the implementation
    file** — all reviews and PR diffs land here where reviewers
    expect them. Existing scripts with
    `docker compose -f docker-compose.tier0.yml up -d` keep
    working unchanged because the file is still present.
  * **`docker-compose.linux.yml` got a prominent deprecation
    banner** at the top explaining its limited scope (host
    networking for ONVIF multicast camera discovery only — strict
    subset of tier0 otherwise) and the opt-in path
    (`OPENNVR_COMPOSE_FILE=docker-compose.linux.yml ./start.sh up`).
    Slated for removal in v0.2 once a host-mode profile/overlay
    lands on tier0.
  * **`docker-compose.tier0.offline.yml` is being prepared for
    removal** — was a no-op stub since ISSUE-7 v3. The land
    sequence below includes `git rm` for it.
  
  The old `docker-compose.yml` had a different network
  architecture (`sentinel_internal` + `public_uplink` networks
  with separate `OPENNVR_PUBLIC_SUBNET` override) that predated
  the tier0 simplification to a single `opennvr_internal`
  network. After consolidation, operators running bare
  `docker compose up -d` get the same architecture as tier0 —
  no surprise drift.
  
  Test surface adjusted to match:
  
  * `test_build_resilience.sh` has a new contract test
    (now 25 tests total) that asserts `docker-compose.yml` is
    a thin include shim with no `services:` block — a stray
    services block would shadow the include and create silent
    drift from tier0.yml.
  * `test_docker_subnets.sh` was rewritten (8 → 6 tests) to
    reflect the single-network architecture: the old
    `sentinel_internal`/`public_uplink` two-network tests are
    gone because that architecture is gone.
  * The compose-file scan lists in `test_build_resilience.sh`
    no longer include `docker-compose.yml` (since it's an
    include shim — same content gets tested via tier0.yml).
  
  Net: 110 tests across 10 suites, all green.

- **Compose file reference added to `docs/DOCKER_SETUP.md`
  (ISSUE-16).** The repo ships five compose files with subtly
  different purposes (`tier0.yml`, `linux.yml`, `docker-compose.yml`,
  `camera-agent.yml`, `tier0.offline.yml`) and there was no
  central reference explaining which to use when. Operators
  who looked for guidance found `DOCKER_SETUP.md`'s "Network
  Strategy" section telling them to manually edit
  `docker-compose.yml` to toggle bridge/host mode — advice that
  predates the `tier0.yml` + `./start.sh` productized path by
  several iterations.
  
  Added an authoritative "Compose file reference" section at
  the top of `DOCKER_SETUP.md` covering: file-by-file purpose
  table, persona-to-file mapping, full service-comparison matrix
  (the one that surfaced the "linux.yml is a strict subset"
  finding during the ISSUE-12/13 review), historical context
  on why there are five files, the planned post-v0.1 consolidation
  path, and how to override the auto-pick via
  `OPENNVR_COMPOSE_FILE`. README's quickstart now cross-links
  to it.
  
  The older "Network Strategy" section below is now visibly
  stale; flagging in the cross-link but leaving the prose
  in place until a fuller DOCKER_SETUP.md refresh in v0.1.1.

- **`./start.sh up` on Linux now picks `docker-compose.tier0.yml`
  by default (ISSUE-12 + ISSUE-13).** The historical default
  was `docker-compose.linux.yml`, a strict functional subset of
  tier0.yml: no `nginx` / `nginx-certs-init` (so no TLS edge),
  no `yolov8-weights-init` / `yolov8-adapter` (so no detection
  out of the box), no `nats` (so the audit/events bus was
  silent). Surface of broken promises:
  
  * **start.sh's `print_access_urls` always printed
    `Web UI: https://<lan-ip>/`** — but linux.yml had no listener
    on :443. Operators following the printed URL hit "connection
    refused". This was the user-visible bug that surfaced the
    issue.
  * **The README quickstart promised "YOLOv8 detection running
    on your camera feed"** — linux.yml shipped detection only
    behind the opt-in `--profile ai`, which the README never
    mentions.
  * **Downstream services subscribing to `opennvr.inference.*`
    / `opennvr.alerts.*` NATS subjects** got nothing because the
    nats broker wasn't present.
  
  Root cause: linux.yml predates the v0.1 hardening + Tier 0
  productization work. It was originally a "host networking
  variant for ONVIF multicast camera discovery", but tier0.yml
  has since become the canonical full-featured path. Defaulting
  to linux.yml meant operators on Linux silently got a degraded
  experience versus the documented one.
  
  Fix in `start.sh`:
  
  ```bash
  case "$OS" in
    Linux*)
  -    COMPOSE_FILE="docker-compose.linux.yml"
  -    OS_LABEL="Linux (host network mode)"
  +    COMPOSE_FILE="docker-compose.tier0.yml"
  +    OS_LABEL="Linux (Tier 0 — bridge networking + TLS edge)"
  ```
  
  Plus a new `OPENNVR_COMPOSE_FILE` env-var override so operators
  who specifically need host networking (single-LAN topology with
  ONVIF multicast discovery — niche) can opt back into linux.yml
  with `OPENNVR_COMPOSE_FILE=docker-compose.linux.yml ./start.sh up`.
  
  Trade-off: tier0.yml uses bridge networking (Docker subnet
  pinned to 172.28.0.0/16 — ISSUE-6 v7). ONVIF WS-Discovery
  (multicast 239.255.255.250:3702) doesn't cross Docker bridges
  without extra config — operators relying on multicast must
  add cameras by IP manually or use the OPENNVR_COMPOSE_FILE
  escape hatch. In a dual-NIC camera-LAN topology (the
  recommended hardened layout per `docs/SECURITY_ARCHITECTURE.md`),
  multicast discovery isn't the path anyway — the camera-LAN NIC
  is firewalled off from the operator UI by intent.
  
  New regression test
  `tests/host-hardening/test_compose_file_selection.sh` (6
  tests) locks the new default and catches the class of bug
  where start.sh's printed scheme drifts from what the
  selected compose actually serves:
  
  (1) start.sh's Linux case-arm points at tier0.yml;
  (2) the `OPENNVR_COMPOSE_FILE` override hook exists;
  (3) the default Linux compose ships nginx + nginx-certs-init;
  (4) it ships yolov8-weights-init + yolov8-adapter and they
      aren't profile-gated (operators don't discover --profile
      flags from the quickstart);
  (5) it ships nats;
  (6) start.sh's `https://` URL matches a real :443 listener
      in the compose — drift one and the test fails.

- **`./start.sh up` on Linux no longer hits `deb.debian.org`
  during `opennvr-core` build (ISSUE-11).** When start.sh
  detects a Linux host it picks `docker-compose.linux.yml`
  (host networking — needed for ONVIF camera discovery via
  multicast). That file declared `opennvr-core` with a `build:`
  block but no `image:` directive, forcing Compose to build
  the root `Dockerfile` locally. That Dockerfile does
  `RUN apt-get install` at both the python-builder stage
  (build-essential, libpq-dev, libgl1, libglib2.0-0) and the
  runtime stage (supervisor, gosu, libpq5, libgl1, libglib2.0-0,
  libgomp1, libsm6, libxext6, libxrender1) — same
  `deb.debian.org` block several operator ISPs filter. Reported
  from IN on first `./start.sh up` after the ISSUE-10 doc fix
  pointed operators at start.sh as the canonical entry.
  
  This was the same bug class as ISSUE-7, but at a layer the
  ISSUE-7 v6 regression test couldn't see: the test walked
  `dockerfile_inline:` blocks in compose files, not external
  Dockerfiles referenced via `build: dockerfile: <path>`. The
  root `Dockerfile` and `kai-c/Dockerfile` and every
  `examples/*/Dockerfile` were entirely unaudited.
  
  Fix in both `docker-compose.linux.yml` and `docker-compose.yml`
  uses the same pull-or-build pattern as `tier0.yml`'s
  opennvr-core and ISSUE-7 v3's yolov8-weights-init:
  
  ```yaml
  opennvr-core:
    image: ghcr.io/open-nvr/core:${CORE_TAG:-latest}
    pull_policy: missing
    build:
      context: .
      dockerfile: Dockerfile
  ```
  
  Compose tries to pull `ghcr.io/open-nvr/core:latest` first
  (published by `.github/workflows/publish-images.yml`). If the
  pull succeeds the local Dockerfile build is skipped entirely
  and no apt-get runs. If the pull fails (fresh repo before
  first publish, registry blocked) Compose falls back to
  building locally so unfiltered-network operators still get a
  working build. `CORE_TAG` defaults to `latest` and is
  operator-overridable to pin a specific release.
  
  Regression test surface in `test_build_resilience.sh` grew
  from 22 to 24 tests. Two new ones:
  
  (1) Walks every external Dockerfile (not just inline) and
      asserts that any service whose Dockerfile does `RUN
      apt-get install` / `RUN apk add` / `RUN pip install` ALSO
      declares `image:` + `pull_policy: missing` so operators
      on filtered networks have a fallback. Profile-gated
      services (`profiles: [ai]`, `profiles: [camera-agent]`)
      are exempted because they're explicitly opt-in — the
      operator who runs `--profile ai` accepts the build-side
      burden. Tracked as follow-ups for when those images get
      published.
  
  (2) Positive contract on opennvr-core specifically: every
      compose where opennvr-core has a `build:` block must
      also have `image:` set to `ghcr.io/open-nvr/core:*` AND
      `pull_policy: missing`. Locks the fix shape so a future
      "let's just build locally" refactor regresses immediately.
  
  Known follow-ups: ai-adapter and camera-agent services still
  require local build because their pre-built images aren't
  published yet. Both are profile-gated so they don't block the
  default Tier 0 install path, but `--profile ai` and
  `--profile camera-agent` operators on filtered networks will
  still hit the same trap. Tracked as ISSUE-11 follow-ups.

- **README quickstart now uses `./start.sh up` as the canonical
  entry point (ISSUE-10).** The previous quickstart instructed
  operators to run `cp .env.example .env`, then
  `./scripts/generate-secrets.sh --write`, then
  `docker compose -f docker-compose.tier0.yml up -d` — and then
  said "`./start.sh up` prints the URLs you should visit."
  
  That sequence is doubly broken. (1) It does manually what
  `./start.sh up`'s interactive installer (`scripts/install.sh`)
  does automatically — but worse, because the installer also
  prompts for deploy mode, recordings path, admin user, and
  validates the resulting config. (2) After running compose
  directly, the operator never sees the first-time setup token
  banner (no health-wait, no log grep), the LAN access URL
  (start.sh has the NIC IP, compose doesn't), or the security
  posture banner (the every-boot flag-if-degraded check). The
  fresh-deploy operator either grep'd the logs by hand for the
  token (if they read past the "start.sh prints URLs" line) or
  hit the UI on localhost only and missed the LAN-reachability
  story entirely.
  
  Quickstart is now three lines:
  
  ```
  git clone https://github.com/open-nvr/open-nvr.git
  cd open-nvr
  ./start.sh up
  ```
  
  First run launches the interactive installer, second run
  starts containers + prints LAN URL + token + posture. The old
  manual flow is preserved under a new "Skip the wizard" section
  for power users (CI, configuration management, pinning specific
  secret values) — but it still ends in `./start.sh up`, never
  bare compose. The README now explicitly warns against using
  bare compose at the end of the manual flow, with the grep
  fallback documented for operators who absolutely need it.
  
  The camera-agent section ("Talk to your cameras") legitimately
  uses bare compose because start.sh doesn't support the
  camera-agent profile yet — that's an unaddressed gap, not a
  doc bug. Tracked separately.

- **Setup-token banner pipeline locked with a regression harness
  (ISSUE-5 follow-up).** The ISSUE-5 fix made `start.sh` wait for
  opennvr-core's Docker healthcheck before grepping the token
  banner out of container logs. The pipeline has four silent-
  failure traps — each one would leave a fresh-deploy operator
  staring at a misleading "first-time setup is already complete"
  message without any obvious code break:
  
  * the literal string `"first-time setup token"` must match
    between `server/main.py` (banner emitter) and `start.sh`
    (banner grep);
  * the banner must be exactly 7 lines (match + `-A 6`) so
    `tail -7` keeps a clean unit;
  * `tail -7` must be present to handle crash-loops — each
    restart of opennvr-core mints a fresh banner with a new
    in-memory token, and only the LAST one is live;
  * the health-wait loop must be present (not just a wall-clock
    timeout) so slow boots like the Pi 5 first-export pass.
  
  The new harness `tests/host-hardening/test_setup_token_banner.sh`
  exercises the actual grep pipeline against synthetic log
  inputs and AST-walks `first_time_setup_service.py` for the
  idempotency guard. 8 tests:
  
  (1) single-banner happy path extracts the token cleanly,
  (2) crash-loop with three banners returns only the latest token,
  (3) absent-banner returns exactly empty (signals "already activated"),
  (4) banner is exactly 7 lines after the pipeline,
  (5) main.py contains the literal `"first-time setup token"`,
  (6) start.sh polls `.State.Health.Status` and breaks on `healthy`,
  (7) `OPENNVR_SETUP_TOKEN_MAX_WAIT_S` override hook exists (so a
       future docker-in-docker integration test can short-circuit
       the 20-min production timeout),
  (8) `maybe_arm()` has the `if _state is not None: return None`
       idempotency guard (AST match — survives cosmetic reformatting
       but catches deletion).
  
  Tests run by feeding the exact same `grep -A 6 "first-time setup
  token" | tail -7` pipeline that start.sh runs, so any drift in
  either side fails the contract. No docker needed — pure synthetic
  inputs, runs in milliseconds, CI-friendly.

- **AST contract test for the URL fallback chain caught three
  more recordings.py bugs (ISSUE-4 v2).** While writing the
  follow-up AST regression test for ISSUE-6 v8's `get_playback_url`
  fix, the test found three more functions in
  `server/routers/recordings.py` returning the Docker-bridge
  internal MediaMTX URL (`http://mediamtx:9996`) to the browser
  instead of the external fallback chain:
  
  * **`get_playback_config`** (line 886) returned
    `"playback_url": settings.mediamtx_playback_url` — the
    frontend uses this as the base for constructing playback
    URLs. LAN browsers would get an unreachable
    `http://mediamtx:9996/...`.
  
  * **`get_today_segments`** (lines 959, 984) built per-segment
    `playback_url` strings and a `playback_base_url` field, both
    using the internal URL. The DVR-style timeline scrubber in
    live view almost certainly hit this — segment thumbnails
    would point at the unreachable URL.
  
  * **`list_recordings`** (line 563) returned the same
    `playback_base_url` field with the internal URL.
  
  * **`_group_segments_by_date`** (line 1065) built per-day
    aggregate `playback_url` strings using the internal URL —
    surfaced through any endpoint that calls this helper.
  
  All four shapes are the same bug as the `get_playback_url`
  ISSUE-6 v8 fix; they just survived because no one had thought
  to write a regression test that catches the bug class
  structurally. Each is fixed by extracting the same
  `external or internal or hardcoded-default` chain that
  `get_playback_url` already uses, then using that for any
  field returned to the browser. The five legitimate server-side
  uses (LIST calls to mediamtx's admin API over the Docker
  bridge, plus the health probe and the config-guard check) are
  marked with a per-line `# url-internal-ok: <rationale>` pragma
  so the test exempts them while keeping the intent reviewable
  in the diff.
  
  The new contract test lives at
  `tests/host-hardening/test_url_fallback_chain.sh` and runs three
  checks: (1) every `settings.mediamtx_<x>_url` reference in
  streams.py/recordings.py either appears in a `BoolOp(or)`
  fallback chain with its external counterpart or carries the
  pragma; (2) every `# url-internal-ok` pragma carries a
  colon-separated rationale (a bare pragma fails — keeps the
  next reviewer from approving an "ok-because-I-said-so"
  exemption); (3) every fallback chain includes a hardcoded
  http(s)/rtsp(s) default so the service can't 500 during
  startup if both settings are None. Walks both
  `server/routers/streams.py` (which was already correct — used
  as the positive reference shape) and
  `server/routers/recordings.py` (which had the four bugs).
  Implementation uses Python's `ast` module with parent tracking
  so it can identify "this Attribute reference is or isn't inside
  a fallback chain" precisely.
  
  Total host-hardening suite now: 96 tests across 8 suites, all
  green.

- **Host-hardening test suites are now actually in git
  (ISSUE-9).** Discovered while preparing the ISSUE-7 v6 commit:
  `.gitignore` line 34 was an un-anchored `host-hardening/` —
  intended to exclude the operator-local nft snapshot directory
  at repo root, but the un-anchored pattern matches any directory
  named `host-hardening` at any depth, including
  `./tests/host-hardening/`. Consequence: every test file under
  `tests/host-hardening/test_*.sh` (built up across ISSUE-6 → ISSUE-8
  to ~95 tests across 7 suites) was excluded from commits. The
  "X tests green" claims in those CHANGELOG entries pointed at
  files that lived only in local working trees — they never
  reviewed, never ran in CI, and a new contributor cloning the
  repo would not get them.
  
  How it slipped through `test_script_permissions.sh` (which was
  supposed to be the regression net for this class of bug): tests
  2 and 3 took globbed file lists as input
  (`git ls-files --stage 'tests/host-hardening/*.sh'`). When the
  glob matched zero tracked files (because they were all
  gitignored), the awk filter that catches mode mismatches produced
  empty output — and an empty output passed the "no violations"
  check vacuously. The test couldn't detect its own absence.
  
  Fix:
  
  (1) **`.gitignore` line 34 changed from `host-hardening/` to
      `/host-hardening/`** — leading-slash anchors the pattern to
      the repo root, so it matches only the operator-artifact dir
      `./host-hardening/` (created by
      `scripts/apply-camera-vlan-hardening.sh`), not `./tests/`'s
      subdirectory.
  
  (2) **New test 3 in `test_script_permissions.sh`** explicitly
      enumerates the seven host-hardening test suite files and
      asserts each one is (a) on disk, (b) not gitignored, (c)
      tracked in `git ls-files`. The enumeration is explicit (not
      globbed), so an empty list now fails loudly rather than
      passing vacuously. The error message tells the next operator
      exactly which file is missing and what command will fix it.
  
  (3) **`git add tests/host-hardening/`** is required as part of
      this commit to actually land the suites in source control
      for the first time. After that the regression test prevents
      this exact class of bug from recurring.
  
  Test count after the fix: `test_script_permissions.sh` grew from
  3 to 4 tests (the new tracking check). Net host-hardening suite
  total: ~95 tests across 7 suites, all green, all in git.

- **Cert-init + config-init containers no longer depend on
  `dl-cdn.alpinelinux.org` at runtime (ISSUE-7 v6).** Closes the
  "Known follow-up" called out under ISSUE-7 below. Operators on
  the same filtered networks (IN reports, same class as CN/IR)
  who got past the build step still hit:
  
      Container opennvr_nginx_certs_init  Error
      WARNING: fetching https://dl-cdn.alpinelinux.org/.../main: Permission denied
      ERROR: unable to select packages:
        openssl (no such package):
          required by: world[openssl]
  
  Root cause was the same as ISSUE-7's build-time bug, just at a
  different lifecycle stage: `mediamtx-certs-init` and
  `nginx-certs-init` ran `apk add --no-cache openssl` in their
  `command:` block at container *start*, and that apk fetch
  reaches the Alpine package mirror. `camera-agent-config-init`
  had the same shape (`apk add --no-cache gettext` for `envsubst`).
  Three containers, one failure mode, three deploys broken on
  filtered networks.
  
  Fix is to never run `apk add` in a `command:` block either —
  pre-bake the tool at the registry layer instead, the same
  principle that ISSUE-7 applied to image build:
  
  * Cert-init: base swapped from `alpine:3.20` to
    `alpine/openssl:3.3.2` (a 4–6 MB Alpine image that ships the
    `openssl` CLI). Pulled from Docker Hub at the same registry
    operators already reach for `alpine:*`. Multi-arch
    (amd64/arm64/armv7), tag-pinned, last published 2025-02-08.
    Image's default `ENTRYPOINT ["openssl"]` is cleared with
    `entrypoint: []` so our existing `command: [sh, -c, ...]`
    runs as the container's argv. Applied to all three cert-init
    occurrences (`docker-compose.tier0.yml`, `docker-compose.linux.yml`,
    `docker-compose.yml`) — they converge on the identical shape.
  
  * Config-init: kept `alpine:3.20` base (`envsubst` is the only
    thing it needed) and replaced `envsubst`-from-gettext with
    a busybox-`sed` substitution that does the same job. Sed is
    in busybox by default, so no `apk add` is needed. The
    substitution preserves envsubst's "only the listed variables"
    semantics — other `$` sigils in YAML multi-line blocks (e.g.
    `system_prompt`) are left untouched. Values are escape-shielded
    against sed's replacement-string specials (`\`, `&`, `/`) so
    secrets containing those characters round-trip cleanly;
    verified end-to-end with `INTERNAL_API_KEY` set to a value
    containing all three.
  
  Regression test surface in `tests/host-hardening/test_build_resilience.sh`
  grew from 17 to 22 tests. The new ones lock the class of bug
  structurally: (1) no `apk add` / `apt-get install` / `pip install`
  on any non-comment line of any `command:` block in any compose
  file; (2) every `mediamtx-certs-init` uses `alpine/openssl:*`;
  (3) `nginx-certs-init` uses `alpine/openssl:*`; (4) all cert-init
  services declare `entrypoint: []` (without it, our command would
  be passed as args to `openssl` and the container would silently
  do nothing); (5) `camera-agent-config-init` uses sed not envsubst.
  All 92 host-hardening tests green across the 7 suites.
  
  Net: the documented single command
  `docker compose -f docker-compose.tier0.yml up -d` now works
  end-to-end on ISP-filtered networks. There is no remaining
  external-package-repo dependency anywhere in the Tier 0
  lifecycle: not at image pull, not at image build, not at
  container start.

- **Single-command Tier 0 deploy works for every network
  (ISSUE-7 v3).** Folded the ISSUE-7 v2 offline overlay into the
  default `docker-compose.tier0.yml` so the documented command
  
      docker compose -f docker-compose.tier0.yml up -d
  
  works on every operator's network — ISP-filtered or not — with
  no extra `-f` flag. Pattern: `yolov8-weights-init` now declares
  both `image: ${YOLOV8_WEIGHTS_IMAGE:-ghcr.io/open-nvr/yolov8-weights:v8.3.0}`
  AND `build:` pointing at `./examples/yolov8-weights/`. Compose
  tries to pull the image first; if it can't (registry blocked,
  fresh repo before first release, no internet at all for the
  registry), it falls back to building locally — first build ~10
  min (dominated by `ultralytics/ultralytics:8.3.40` base pull,
  ~3 GB), subsequent runs use the cached image. The container's
  `command:` is now a single `cp /yolov8n.onnx /weights/`; the
  old apt-get + pip install + ultralytics export path is gone
  entirely. The weights image's own Dockerfile dropped its
  `apt-get install curl` step too — replaced with Python
  `urllib.request.urlretrieve` (urllib is stdlib in the
  ultralytics base, no install needed). `docker-compose.tier0.offline.yml`
  is kept as a no-op stub so operators who pasted the old
  dual-flag command don't break, with a header pointing at
  `git rm` for cleanup. 2 new tests in
  `tests/host-hardening/test_build_resilience.sh` lock the
  contract: tier0's yolov8-weights-init has both `image:` and
  `build:` with a `cp` command (no apt/pip/yolo), and the
  Dockerfile doesn't `RUN apt-get install curl`. Total
  build-resilience tests: 10/10 green.

- **Tier 0 offline overlay for ISP-filtered networks
  (ISSUE-7 v2).** The yolov8-weights-init container at *runtime*
  does `apt-get install curl` (deb.debian.org), `pip install
  ultralytics` (pypi.org), and downloads `yolov8n.pt` from
  github.com — three external dependencies that any of which can
  be filtered by an operator's ISP/firewall. Reported from IN
  networks; same pattern hits CN/IR. Fix: ship a
  `docker-compose.tier0.offline.yml` overlay that redefines
  `yolov8-weights-init` to pull `ghcr.io/open-nvr/yolov8-weights:v8.3.0`
  (a ~20 MB alpine-based image with `yolov8n.onnx` pre-baked) and
  `cp` it into the weights volume. Zero apt, zero pip, zero
  external network beyond Docker registries. Operators add one
  `-f` flag:
  
      docker compose -f docker-compose.tier0.yml \
                     -f docker-compose.tier0.offline.yml \
                     up -d
  
  The image is built and published by
  `.github/workflows/build-yolov8-weights.yml` on every release
  tag — multi-arch (amd64+arm64), uses GHA cache so subsequent
  builds are ~30 sec. Operators on networks where ghcr.io itself
  is blocked can build the image locally with
  `examples/yolov8-weights/Dockerfile` (multi-stage: builds via
  `ultralytics/ultralytics:8.3.40`, ships only the .onnx in the
  final stage) and either `docker save | scp | docker load` it
  or push to a private registry, then point the
  `YOLOV8_WEIGHTS_IMAGE` env var at it. Custom fine-tuned models
  can be baked in by overriding the `YOLOV8_PT_URL` build arg.
  Adapter conformance posture unchanged — the ONNX produced is
  byte-identical to what the default first-boot export path
  produces, since both pin the same Ultralytics tag and export
  flags (opset=12, imgsz=640, simplify=False). 4 new tests in
  `tests/host-hardening/test_build_resilience.sh` lock the
  contract: the offline overlay has no apt/pip, its
  yolov8-weights-init is a simple image+cp (not apt+pip+yolo
  export), the image reference is `YOLOV8_WEIGHTS_IMAGE`-
  overridable, and the weights-image Dockerfile's final stage is
  `COPY`-only. Documented end-to-end in
  `examples/yolov8-weights/README.md`.

- **Tier 0 build no longer depends on external package
  repositories (ISSUE-7).** Operators behind ISP / corporate
  firewalls that filter `dl-cdn.alpinelinux.org` (reported from
  IN, also seen on IR/CN networks) previously had
  `docker compose -f docker-compose.tier0.yml up -d` fail at the
  mediamtx image build step:
  `apk add --no-cache curl` → `WARNING: fetching ... Permission denied`.
  Mirror-swapping (Aliyun, Tsinghua, Yandex, etc.) helped some
  operators but not all — depending on which hostnames the
  filter targets. The robust fix is to **never `apk add` during
  build**: the mediamtx Dockerfile_inline now pulls a static
  curl binary out of the official `curlimages/curl:8.10.1` image
  on Docker Hub via multi-stage `COPY`, instead of calling apk.
  Same registry as `alpine:3.20` and `bluenviron/mediamtx`, so
  if the operator can pull *any* image they can build mediamtx.
  Applied to `docker-compose.tier0.yml`,
  `docker-compose.linux.yml`, and `docker-compose.yml` — all
  three converge on the same pattern. New regression test
  `tests/host-hardening/test_build_resilience.sh` walks every
  compose file and fails CI if any `dockerfile_inline:` block
  contains `RUN apk add`, `RUN apt-get install`, or
  `RUN pip install`, plus asserts every `FROM` references either
  Docker Hub or `ghcr.io/open-nvr/*` — preventing future drift on
  the "no external repo at build time" principle.
  
  **Follow-up resolved in ISSUE-7 v6 above:** `mediamtx-certs-init`,
  `nginx-certs-init`, and `camera-agent-config-init` all called
  package-manager installs at *runtime* (inside container
  `command:` blocks, not at build time) which hit the same
  `dl-cdn.alpinelinux.org` filter at first start. v6 swaps cert-init
  to `alpine/openssl:3.3.2` (openssl pre-baked) and replaces
  envsubst with busybox sed in the config-init container.

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
