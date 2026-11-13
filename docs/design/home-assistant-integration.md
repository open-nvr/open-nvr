# Design doc: OpenNVR × Home Assistant integration

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 2026-09-18 |
| **Revision** | r3 (2026-09-18): execution self-review (token principal, recording loophole, permission grants, request context, dev layout). r2: every code claim checked against the repo and corrected |
| **Author** | Suraj Raj Bhandari |
| **Audience** | OpenNVR core, frontend and app-platform engineers; reviewers of the HA integration |
| **Supersedes** | `examples/home-assistant-relay` as the recommended HA path |
| **Implementation plan** | [home-assistant-integration-implementation-plan.md](home-assistant-integration-implementation-plan.md) |

## 1. Summary
OpenNVR will ship a first-class Home Assistant (HA) integration. It will match everything the established NVR integrations for Home Assistant offer, and go further on what prosumers and small businesses need:
- alert media that stays secure and works off the local network;
- an audit trail of every action;
- alerts graded by severity;
- evidence export;
- health warnings;
- interoperability with professional systems.

The work has six parts:
1. **Core foundations** in open-nvr:
   - API tokens and signed media URLs;
   - a live-state service and core zones;
   - the missing controls, websocket v2, and optional LAN discovery;
   - **server-described entities** and a **server↔integration compatibility contract**. These two keep the integration's long-term maintenance cost low.
   - Before any of this, some existing gaps must be closed: ffmpeg in the core image, correlation ids, audit coverage, and permission seeding (§6.0).
2. **A native HA integration** (`opennvr`, no MQTT required), plus a client library, `pyopennvr`.
3. **Zero-install MQTT device discovery** published by core, generated from the same entity descriptors.
4. **Assist / MCP AI tools** so people can ask about footage in plain language.
5. **Standards bridges:** an ONVIF Profile S/M server and a Matter 1.5 camera bridge. Each starts as a spike.
6. **Beyond parity:** a cross-camera timeline card, two-way audio, and alarm-centre reporting.

## 2. Background
### Today
OpenNVR reaches HA only through `examples/home-assistant-relay`. That is a one-way NATS→MQTT bridge that turns alerts into binary sensors. It has no cameras, controls, media, health data or availability. `docs/COMPARISONS.md` acknowledges that established NVR integrations are well ahead of it.

### The reference: established NVR integrations
The most widely used NVR integration (tens of thousands of opt-in installs) offers:
- entities for cameras, occupancy and counts, motion, switches, numbers, a profile select, images, health sensors, and updates;
- services for PTZ, export, create/end event, and favourite;
- a media browser, a notification media proxy, and a websocket API used by the Advanced Camera Card.

For that NVR, HA is the main route to users.

### Known weaknesses of that integration: our opportunity
- MQTT is mandatory.
- The notification proxy is unauthenticated, and its links never expire by default.
- Occupancy is wrong after an HA restart.
- No storage sensors, no per-zone images, no PTZ preset select.
- It is HACS-only **by choice** (a public maintainers' discussion, Aug 2026):
  - An HA core member offered to bring the integration into core. The owner was open to it, but noted that some code (such as the unauthenticated proxy in `views.py`) likely wouldn't be accepted.
  - A collaborator then argued against it because of the **"forever cost"**. Every NVR or card release that needs an integration change would wait in HA's review queue: more than 2 weeks at best, sometimes more than 4 months. Meanwhile users report "I updated the NVR and HA broke".
  - The move stalled. The agreed outcome was to extract the API layer into a separate API-client library.
  - For users this means an extra HACS install and no official quality-scale grade. **The forever-cost argument applies to us too; see §7.1.**
- It doesn't use HA Repairs. That is a gap in its implementation, not a HACS limitation: custom integrations can raise Repairs too.
- No MFA, no audit log, and multi-site is out of scope.
- No ONVIF server.

### Other benchmarks
- **UniFi Protect** (HA core, Platinum): event entities and stable proxy URLs.
- **Reolink** (HA core, Platinum): push events and deep controls.
- **Nest:** triggers fire only once the clip is ready.

### Where OpenNVR already starts ahead (verified in code)
- MFA.
- An audit log (`services/audit_service.write_audit_log`), though coverage has gaps (§6.12).
- Per-app keys.
- A severity-graded alerts inbox.
- Native WHEP through MediaMTX.
- A multi-app AI platform with declarative app manifests.

**Not yet present:** request correlation ids in the audit log (only a generated `X-Request-ID` exists). This is new work (§6.12).

## 3. Goals and non-goals
### Goals
- **G1.** Match every capability of the established NVR integrations, or record a deliberate difference (see the parity matrix in §12).
- **G2.** Secure by default:
  - scoped, revocable credentials;
  - every action audited;
  - no media without HA authentication or a valid signed, expiring, single-object token.
- **G3.** Correct state at all times: after an HA or server restart, entities are correct within seconds.
- **G4.** Media is ready before a trigger fires, so notification links never point at missing media. They also work when the phone is off the local network.
- **G5.** Standards first: HA event entities, native WebRTC over WHEP, MQTT device discovery, ONVIF Profile S/M, Matter 1.5.
- **G6.** Several sites in one HA, each isolated.
- **G7.** Meet HA's Gold quality-scale rules (later Platinum), whether we ship through HACS or core. Moving into HA core is optional, not a goal (§7.1).
- **G8.** Low "forever cost":
  - most new OpenNVR capabilities (new AI apps, alert types, sensors, controls) reach HA **with no integration release**;
  - no server upgrade ever breaks a supported integration version (§6.10, §6.11).

### Non-goals (for this design)
- Replacing the OpenNVR web UI inside HA.
- HomeKit Secure Video, which needs Apple's MFi programme.
- Claiming ONVIF or Matter certification. That needs paid membership and is a business decision.
- Cloud relays or remote access. Users keep their existing HA Cloud or VPN. Notification media rides on HA's own external URL (§7.7).

## 4. Users and key scenarios
**Primary persona:** the owner or IT person at a shop, office or showroom who uses HA to run the building (lights, locks, sirens, speakers). **Secondary:** advanced home users.

| # | Scenario | What they get |
|---|---|---|
| S1 | Setup | They enter the OpenNVR URL (or HA discovers it on Linux hosts with the optional mDNS sidecar), paste a token, choose cameras, and are done within 2 minutes |
| S2 | After-hours intrusion | A person in the "Stockroom" zone while armed_away turns on the siren and lights, and sends a critical phone alert with photo, clip and an "Acknowledge" button. It works on mobile data. |
| S3 | Doorbell or visitor | Announced on Alexa or Google speakers; live view in the notification |
| S4 | Evidence | Export 10 minutes from the gate camera with a SHA-256 hash, and protect it from retention deletion |
| S5 | Health | Camera offline, recording stalled, or disk above 90% → a Repairs entry or notification |
| S6 | Plain-language questions | "Did a white van come to the gate yesterday?" answered by Assist |
| S7 | Accountability | Every switch, PTZ move or export done from HA appears in OpenNVR's audit log as `token:<name>`, with the HA context id as the correlation id |
| S8 | Multiple sites | Two shops and a warehouse in one HA, each with its own device tree |
| S9 | Professional VMS | A security company's VMS adds OpenNVR cameras and AI events over ONVIF (after the Phase 5 spike) |

## 5. Architecture overview
```
                ┌──────────────────────── Home Assistant ────────────────────────┐
                │  custom_components/opennvr  (pyopennvr client)                  │
                │   config flow · coordinator · entities · media_source · views   │
                │   services · llm.py (Assist) · diagnostics · repairs · ws_api   │
                │   relay views: /api/opennvr/{site}/m/{token} · passthrough      │
                └───────▲──────────────▲───────────────────▲──────────────────────┘
          REST + token  │   WS v2      │ WHEP (SDP)        │ MQTT (optional, Phase 3)
                        │   (push)     │                   │
┌───────────────────────┴──────────────┴───────┐   ┌───────┴────────┐
│ nginx :443                                    │   │ MQTT broker    │
│  /api/v1/* → opennvr-core                     │   │ (user's)       │
│  /webrtc/* → MediaMTX (WHEP)  /hls/*          │   └───────▲────────┘
└──────────────▲────────────────────────────────┘           │
               │                                            │
┌──────────────┴──────────── opennvr-core ──────────────────┴──────────────┐
│ auth (JWT | ApiToken) · device firewall · audit (+correlation id)        │
│ NEW: api_tokens · media_signing · live_state · zones · site_mode         │
│ NEW: ha_mqtt_discovery · search API · system/info · camera stats         │
│ NEW: entity descriptors (core + app manifests) · contract_version        │
│ event_bus_service (WS v2)  ◄── tier0_track_consumer ◄── NATS tier0       │
└──────────────▲───────────────────────────────▲───────────────────────────┘
               │ same /api/v1 contract         │
      ┌────────┴────────┐              ┌───────┴────────┐   ┌──────────────┐
      │ onvif-server    │              │ matter-bridge  │   │ mdns-        │
      │ Profile S + M   │              │ Matter 1.5 cam │   │ announcer    │
      └─────────────────┘              └────────────────┘   │ (host net,   │
          (Phase 5 spikes)                                   │  optional)   │
                                                             └──────────────┘
```

**Principle: one contract, many clients.** Everything goes into the generic `/api/v1` and the event websocket. HA, MQTT discovery, ONVIF and Matter all consume that contract. Core has no HA-only endpoints.

**Runtime constraint.** Core runs **one uvicorn worker** (`supervisord.conf:7`). Several things rely on this and keep their state in memory: websocket tickets, export tickets, the event bus, and the new websocket v2 ring buffer. Moving to multiple workers would need a shared store for all of them.

## 6. Core changes (open-nvr)
### 6.0 Prerequisites (existing gaps found in review)
- **ffmpeg in the core image.** The runtime stage of the root `Dockerfile` has no ffmpeg, so `/recordings/frame` returns 502 on every install. Frame extraction and last-object crops need it. (Exports don't: they proxy MediaMTX `/playback/get`.)
- **Correlation ids and audit coverage:** see §6.12.
- **Permission seeding:** `camera_device.write` is checked (`camera_settings.py:56`) but never seeded. New permissions are added in §6.1.
- **The device firewall doesn't cover websockets.** It is a `BaseHTTPMiddleware`, so `/api/v1/events/ws` bypasses it. The websocket handler must enforce it.
- **The e2e suite source is on the unmerged `test/e2e-suite` branch.** It must land on main before the HA end-to-end tests (§15).

### 6.1 API tokens (service accounts)
- **Model `ApiToken`:**
  - `id`, `name`, `token_hash`: SHA-256 with constant-time compare, the same scheme as app keys in `services/app_keys.py`;
  - prefix `onvr_<8>`, `owner_user_id`, `scopes[]`;
  - `camera_ids[] | null` (null means the owner's visible cameras) and `allowed_cidrs[] | null`;
  - `expires_at`, `created_at`, `last_used_at` (written at most once a minute), `last_ip`, `revoked_at`.
- **Scopes reuse the seeded permission names** (`scripts/init_db.py`): `cameras.view`, `cameras.manage`, `live.view`, `recordings.view`, `recordings.manage`, `alerts.view`, `alerts.manage`, `settings.view`.
- **New permissions, with behaviour-preserving grants.** No existing user gains or loses an ability as a side effect. They are seeded on fresh installs and backfilled on upgrades (the `main.py:216-258` pattern):
  - `ptz.control`: today any user who can see a camera may move it. It is granted to every role that holds `live.view`.
  - `camera_device.write`: already checked, but never seeded. It is seeded with no new grants.
  - `events.create`, `apps.actions`, `recordings.pause`, `api_tokens.manage`: new abilities, admin-only (via `full_access`).
- **Auth:** `core/auth.py` resolves `Authorization: Bearer onvr_…` to a **`TokenPrincipal`**.
  - The principal is a read-only proxy around the owner's User.
    - It always reports `is_superuser=False`.
    - It carries the token, its scopes and its camera allow-list.
    - The ORM User row is **never mutated**. Setting `is_superuser=False` on the mapped row would be persisted by the next commit and permanently demote the admin.
  - Effective permissions = the token's scopes ∩ the owner's permissions.
  - Cameras = the token's allow-list ∩ the owner's visible cameras.
  - Superuser-only routes refuse tokens.
  - This follows the prefix-dispatch pattern of `apps.py:_service_or_user_principal`.
- **Central camera gate** (in the resolver). Camera scoping is spread over several helpers today, so the resolver rejects any camera outside the allow-list in:
  - path or query `camera_id` / `cam_id`;
  - `camera_ids` lists;
  - `cam-N` / `camN` handles.

  Endpoints that carry a camera in the request body check the allow-list explicitly. The scoping helpers intersect with it as well.
- **Device firewall** (`middleware/device_firewall.py`): a valid token passes, within `allowed_cidrs` if set. Tokens are bound when created, not approved per browser.
- **Websocket:** `POST /events/ws-ticket` accepts tokens, and the ticket inherits the token's camera scope. App keys stay excluded.
- **Audit:** `details.actor = "token:<name>"`, plus the correlation id (§6.12).
- **Endpoints:** `GET/POST /api/v1/api-tokens` and `DELETE /api-tokens/{id}`. The secret is shown once.
- **UI:** a new API tokens tab in the Settings registry (`app/src/views/Settings.tsx`).

### 6.2 Site info, health and camera stats
- **`GET /api/v1/system/info`** returns `{site_id, name, version, contract_version, features[], deprecations[], passthrough_allowlist[], latest_version?}`.
  - `site_id` is a UUID persisted in the generic `SecuritySetting` key/JSON store.
  - The payload is modelled on `apps.py:_registry_info()`.
  - `latest_version` comes from an opt-in GitHub releases check (`UPDATE_CHECK=off` by default, for offline sites).
- **`GET /system/resources`** is extended with per-volume used/free storage, per-camera `days_retained`, memory, and GPU if present (otherwise null).
- **`GET /cameras/{id}/stats`** returns `{input_fps, bitrate_kbps, detect_fps, skipped_fps, inference_ms, recording_state, last_frame_at}`. Sources:
  - input and bitrate: a background sampler takes `bytesReceived` deltas from MediaMTX path info every 10 s;
  - detect fps, skipped fps and inference time: new reducers in `services/tier0_metrics` over the per-camera `tier0_detector_latency_seconds` and `tier0_detector_skipped_total` metrics;
  - recording state: `cameras._derive_recording_state`.

### 6.3 Controls
| Control | Endpoint | Notes |
|---|---|---|
| Camera on/off (also used as privacy mode) | `PUT /cameras/{id} {is_active, reason?}` (existing route) | `is_active=false` already tears down the stream path, which stops live view **and recording**. The audit entry records the reason. There is no separate privacy endpoint. **For API tokens it requires `cameras.manage` and the `recording_pause_enabled` site flag.** Otherwise HA could get around the always-on recording rule the flag protects. Human users in the SPA keep today's behaviour. |
| Detection on/off | `PUT /cameras/{id} {detection_enabled}` | New nullable column `Camera.detection_enabled` (default true). It gates detect-pipeline and KAI-C dispatch for that camera. |
| Recording pause/resume | `POST /cameras/{id}/recording {enabled, resume_after_s?}` | **Behind the site flag `recording_pause_enabled`, off by default.** The handler at `cameras.py:2133` is disabled by an explicit product rule ("recording … must not be switchable off"). With the flag off the rule stands and the endpoint returns 403. With it on, pausing needs `recordings.pause`, is audited, and can auto-resume. `/system/info` reports the flag in `features`, and the HA switch exists only when it is on. |
| PTZ move/stop | `POST /cameras/{id}/ptz/move`, `/ptz/stop` (existing) | Add `ptz.control` and audit logging (§6.12). |
| PTZ presets | `GET /cameras/{id}/ptz/presets`, `POST …/presets/{token}/goto`, `POST …/presets` | Looked up by camera id using stored credentials. Reuses `onvif_service` `GetPresets` (today only reachable via the IP-keyed `onvif.py:539`). |
| Manual event | `POST /api/v1/events {camera_id, label, sub_label?, duration_s?, include_recording}`, `PUT /events/{id}/end` | Today only the site key can insert events (`internal_camera_agent.py:126`). Needs `events.create`. |
| Protect footage | `POST /events/{id}/protect {protected}` | Maps to `PUT /recordings/flag` over the event's time range. |
| App action | `POST /apps/{id}/actions/{name}` (existing) | Today user-JWT only, with the comment "do not widen" (`apps.py:1343`). It is **deliberately widened** to API tokens holding `apps.actions`, with audit logging. |
| Site mode | `GET/PUT /api/v1/site-mode {mode: disarmed\|armed_home\|armed_away}` | **v1 = arming only:** the mode changes alert delivery (alarm actions and notifications) and drives the HA alarm panel. Profiles of app enables are deferred (§17). |

### 6.4 Core zones
- **Model `CameraZone`:** `id`, `camera_id`, `name`, `slug`, `polygon` (normalised 0..1), `enabled`, and a labels filter.
- CRUD at `/cameras/{id}/zones`. The zone editor in camera settings **reuses `app/src/views/apps/GeometryEditor.tsx`**, which already draws normalised polygons on a snapshot.
- Apps may reference zones by id. App-local zones keep working. The SDK's `scale_vertices` already accepts both normalised and pixel coordinates.
- `timeline_service.record_track_visit` stores `zone_ids` on the event row, computed from the best box against the zones. It is a new nullable JSON column, used by the search API and the media browser.

### 6.5 Live-state service (`server/services/live_state.py`)
- **Feed:** built on `services/tier0_track_consumer.py`, which already consumes `opennvr.inference.tier0.*.completed` and normalises boxes to 0..1. Tracks carry `id`, `label`, `score`, `box` and `stationary`.
- **Counts:** per (camera, zone, label), with a **total** count (visible tracks) and an **active** count (tracks with `stationary == false`). Parked objects keep appearing in tier0 results, so occupancy stays correct while they are there.
- **Staleness:** tier0 publishes nothing when a camera has no tracks (`publish_empty=False`). If no message arrives for `LIVE_STATE_STALE_S` (default 5 s), that camera's counts drop to 0.
- **Track lifecycle:** a new track id means `event_started`, and a vanished id means `event_ended`. Tracks are keyed by `(camera_id, track_id, started_at)`.
  - `TimelineEvent` rows are written **only when a visit ends** (detect-pipeline `events_poster.py`, for confirmed visits of at least 1 s). The persisted `event_id` is attached when that row lands.
- **Motion:** on/off with debounce, derived from active tracks and gate messages.
- **Last object per label and zone:** when a track is confirmed, take one frame from the KAI-C capture pool (the `/cameras/{id}/snapshot` path) and crop the box. Rate-limited per camera and label. Used by the image entities.
- **Also tracks** the last plate and the last face.
- **Output:** `GET /api/v1/live-state` returns a full snapshot, and deltas go to the event bus.

### 6.6 Websocket contract v2 (`services/event_bus_service.py`)
- **Connection:** `wss://…/api/v1/events/ws?ticket=…&v=2&since=<seq>`. v1 clients are unchanged: they receive today's types (`inference_result`, `camera_status`, `camera_event`, `app_alert`, `tracks`, …).
- **The device firewall is enforced in the handler** (§6.0).

| Message | Payload (abridged) |
|---|---|
| `state_snapshot` | Always sent first. Contains live-state, camera states, site mode, and every `entity_state` |
| `object_count` | camera_id, zone_id?, label, count, active |
| `motion` | camera_id, on |
| `camera_status` | camera_id, online |
| `recording_state` | camera_id, state |
| `camera_stats` | Throttled to one per 10 s per camera |
| `event_started` / `event_ended` | track key, camera_id, label, zone_ids, score, plate?, face?; `event_id` on end once persisted |
| `alert` | alert_id, severity, source, camera_id, title, correlation_id |
| `media_ready` | ref (alert or event); kinds now available (image, clip) |
| `entity_state` | descriptor key, state, attributes (§6.10) |
| `descriptors_changed` | etag |
| `site_mode` | mode |

- Every message carries a monotonic `seq`. The server keeps a 5-minute in-memory ring buffer so a client can resume with `since`. If the gap is too old, the client gets a fresh `state_snapshot`.
- Subscriptions are filtered per token camera scope and message type.

### 6.7 Signed media URLs (`server/services/media_signing.py`)
- **Request:** `POST /api/v1/media/sign {kind, id, ttl_s}` returns `{token, path: "/api/v1/media/s/<token>", expires_at}`. `kind` is one of `alert_image | event_evidence | event_snapshot | event_clip | export`.
- **Token:** HMAC-SHA256 over (kind, id, exp, key_id), base64url-encoded.
  - Keys are stored in `SecuritySetting`. Rotating the key revokes every outstanding link.
  - The TTL is at most 7 days; the default is 24 h.
- **Serving:** a token unlocks exactly one object, with no listing and no guessable ids. Each fetch is audited, rate-limited per link.
- **Sources:**
  - alert images: `alerts_inbox` evidence store;
  - event evidence: `timeline_events` evidence routes;
  - clips and exports: MediaMTX `/playback/get` through the existing export proxy (`recordings.py:937`), padded ±5 s. No ffmpeg needed.
- **Reaching the phone:** core's URL is on the LAN. Notifications use the HA relay view instead (§7.7), so the same token works over HA's external URL.

### 6.8 LAN discovery and streaming
- **mDNS:** core runs on a Docker bridge network, so multicast cannot reach the LAN. Discovery is an **optional `mdns-announcer` sidecar** (compose profile `mdns`, `network_mode: host`) that advertises `_opennvr._tcp.local.` with TXT records `site_id`, `version` and `api=/api/v1`.
  - This works on Linux hosts. Docker Desktop (Windows/macOS) has no host networking, so manual URL entry is the default there.
  - The sidecar is covered by unit tests only, and stays **unverified end to end** until it is run on a Linux host. Manual URL entry is the supported default everywhere.
- **RTSPS on the LAN** is opt-in via `RTSPS_BIND_HOST`. Today it binds to `127.0.0.1` only (`docker-compose.yml:147`).
- **WebRTC remote viewing:** MediaMTX has no ICE servers configured, and `MEDIAMTX_WEBRTC_HOSTS` is empty by default. The setup docs cover both, and the Repair `webrtc_hosts_unset` flags it.
- **Docs:** set `MEDIAMTX_PUBLIC_URL` so stream URLs don't point at localhost. Add the HA origin to `CORS_ORIGINS` for card sessions (§7.8).

### 6.9 Search API (for Assist)
- `GET /api/v1/search?q=&camera_id=&label=&zone=&plate=&from=&to=&limit=`.
- Structured filters work over `TimelineEvent` (including the new `zone_ids`) and `AppAlert`, scoped to the caller's cameras.
- When a KAI-C embedding or VLM adapter is installed, `q` does semantic search. This respects `AI_SOVEREIGNTY=local_only`.

### 6.10 Server-described entities (entity descriptors)
**Why:** to keep the "forever cost" (§2) low. If the HA integration hard-codes every sensor and switch, every new OpenNVR feature needs an integration release. Instead, the server **describes** what HA should show, **resolves the values**, and the integration renders them generically. MQTT discovery and ESPHome prove this pattern works.

**Endpoint:** `GET /api/v1/entities` returns the descriptors visible to the caller's token, with an `ETag`. The websocket message `descriptors_changed` tells clients to re-fetch.

**Descriptor fields:**
- `key` (stable, e.g. `app.loitering.cam3.dwell_alert`) and `device {kind: site|camera|zone, id}`.
- `platform`: `sensor | binary_sensor | switch | select | button | number | event | image`.
- Presentation: `name`, `translation_key?`, `device_class?`, `unit?`, `state_class?`, `entity_category?`, `enabled_default`, `icon?`.
- `options?` (select options, or number min/max/step) and `event_types?`.
- `command?`: **typed, never a free URL**:
  - `{type: "core_control", control: camera_on | detection | recording_pause | ptz_preset | ptz_move | ack_alerts | manual_event | site_mode, args}`, or
  - `{type: "app_action", action}`, which is limited to the declaring app's own declared actions.
  - Core rejects any other command, and checks the token's scopes on every execution. A descriptor grants nothing by itself.
- `required_scope?`, `origin: core | app:<app_id>`, `descriptor_version`.

**State is resolved on the server.** Core pushes `entity_state {key, state, attributes}` messages, which are also included in `state_snapshot`. The integration never interprets data paths. App values come from the app's existing declarative `StateView` metrics.

**Sources:**
- Core publishes descriptors for its own features: occupancy, counts, motion, health, and controls. The recording switch is published only when its flag is on.
- **AI apps declare descriptors in their manifest** through a new `entities:` section in `AppManifest`. That means updating:
  - the dataclass in `sdk/opennvr-app-sdk/opennvr_app_sdk/manifest.py`;
  - `to_dict`;
  - the facade `_manifest_kwargs`;
  - `validate.check_manifest`.
- The server already stores `manifest_json` unchanged, so a new app appears in HA as soon as it is installed, with no integration release.

**What stays hand-written in the integration** is only what can't be generic: the camera entity (WebRTC over WHEP), the media browser, the alarm panel, the update entity, the Assist tools, and the config flow.

**Unknown platforms or fields are skipped**, not treated as errors, and diagnostics lists them.

### 6.11 Compatibility contract (server ↔ integration)
**Goal:** "I updated OpenNVR and Home Assistant broke" must not happen.
- **Versioning:** `/system/info` reports `contract_version`, the semver of the HA-facing API: the REST fields used, websocket v2 messages, and descriptors. The integration declares the range it supports. The existing app-registry `API_VERSION` stays separate.
- **Additive-only changes within a major version:** new fields, messages and descriptor platforms. Removing or renaming anything needs a major bump and a **deprecation window of two server releases**. Deprecated items are listed in `/system/info`.
- **Tolerant readers:** `pyopennvr` and the integration ignore unknown fields and message types.
- **Repairs:** `server_too_old` and `integration_too_old` tell the user what to upgrade, instead of entities just going unavailable.
- **Contract fixtures:** published JSON schemas in `server/contract/`. CI fails on a breaking fixture diff without a major bump.
- **Test matrix:**
  - The integration's test suite runs against server `main` and the last two server releases.
  - Server PRs that touch the contract run the latest released integration.

### 6.12 Audit and correlation coverage
- **Request context:** one contextvar holds a **mutable `RequestContext` object** (`correlation_id`, `actor`).
  - The request middleware creates it before calling the app. Later code, such as the token resolver, mutates the object and never re-sets the var.
  - This matters because FastAPI runs sync dependencies in a threadpool on a *copied* context: a `ContextVar.set()` made there would never reach the endpoint or the audit writer.
- **Correlation ids:**
  - Request logging accepts a validated inbound `X-Correlation-Id` (≤64 chars, safe charset). Otherwise it uses the generated `X-Request-ID`. Today `middleware/request_logging.py:73` always generates one.
  - A new nullable `AuditLog.correlation_id` column is filled via `write_audit_log`.
  - The header is added to the CORS allow and expose lists.
- **Audit gaps to close:** these endpoints change state but write no `AuditLog` today:
  - PTZ move/stop/preset;
  - alert acknowledge;
  - export ticket;
  - device-firewall approve/block/delete;
  - skill picks (PUT/DELETE).
- **Non-user actors** (`token:<name>`, `mqtt:<name>`) are recorded in `details.actor`, following the existing `registered_by` convention in `apps.py`.

## 7. HA integration (`hass-opennvr` repo)
### 7.1 Packaging and distribution
- **Repo** `open-nvr/hass-opennvr`: `custom_components/opennvr/`, `hacs.json`, `blueprints/`, `tests/`, and CI (hassfest, the HACS action, pytest).
- **Development layout (until 1.0):** both packages are developed in this monorepo, under `integrations/home-assistant/hass-opennvr/` and `integrations/home-assistant/pyopennvr/`. Each folder mirrors its future repo root exactly, so the split is a plain `git subtree split --prefix=<folder>`.
- **Install gate:** HA installs integration requirements from PyPI, so **end users can't install the integration until `pyopennvr` is published there**. Until then, 0.1 and 0.5 are development builds; the dev HA container pre-installs `pyopennvr`.
- **`pyopennvr` on PyPI:** an async aiohttp client covering REST, websocket v2 with resume, token auth, descriptor models, and the contract fixtures. It stays a separate library wherever the integration ships, because it is clean, testable, reusable, and a prerequisite for HA core. The same split came out of the public discussion cited in §2.
- **Distribution strategy: HACS by default; core only if it pays for itself.** The "forever cost" argument (§2) holds for OpenNVR too, and as a young, fast-moving product we would feel it more. Being in HA core is **an option, not a goal**:
  1. **Ship on HACS.** We control release timing and can ship the same day as a server release.
  2. **Make integration releases rare.** Descriptors (§6.10) and the contract (§6.11) mean most new capabilities need no integration release.
  3. **Stay core-eligible anyway:**
     - a separate PyPI client;
     - no unauthenticated routes (the relay only accepts valid signed tokens);
     - full tests, config flow, diagnostics and Repairs;
     - `quality_scale.yaml` tracked against the Gold rules.
  4. **Use Repairs from day one.** They don't depend on being in core.
  5. **Decide about core later, against explicit criteria. All must hold:**
     - (a) no breaking contract change for 2 consecutive server releases;
     - (b) the integration's own release rate over the last 6 months is ≤ 1 per month;
     - (c) clear demand from business customers or partners;
     - (d) a maintainer committed to the HA review process.

     Until then, HACS is the home.
- **`manifest.json`:**
  - `iot_class: local_push`, `config_flow: true`;
  - `dependencies: [http, media_source]`;
  - `zeroconf: ["_opennvr._tcp.local."]`;
  - `requirements: ["pyopennvr==X"]`.

  There is no MQTT dependency.

### 7.2 Config flow
1. Enter the URL manually (the default), or confirm a zeroconf discovery on hosts running the mDNS sidecar.
2. `verify_ssl`. For a self-signed certificate it defaults to off, with a warning and a Repair suggesting a proper certificate.
3. Enter the token. It is validated with `GET /system/info` plus scope checks; missing scopes are listed.
4. Choose cameras. The default is every camera the token can see.

- `unique_id = site_id`.
- Reauth for an expired or revoked token; reconfigure to change the URL.
- **Options:** cameras, notification link TTL, stream (main/sub), and which entity groups to enable.

### 7.3 Runtime
- The coordinator holds one websocket per site and applies `state_snapshot`, deltas, and `entity_state`.
- A REST poll every 30 s refreshes stats, and acts as the fallback if the websocket is down for more than 60 s.
- Entities become `unavailable` when the site is unreachable. Camera entities become `unavailable` on `camera_status: offline`.

### 7.4 Devices and entities
- **Devices:**
  - `OpenNVR <site name>` (the server);
  - one per camera, with `via_device` = the server;
  - one per zone, with `via_device` = its camera.

| Platform | Entity | Scope | Default | Source |
|---|---|---|---|---|
| camera | Live camera: WebRTC over WHEP, still image, motion-detection toggle → detection flag. On/off (`is_active`) is advertised **only when `recording_pause_enabled` is on** (§6.3) | camera | on | `/streams/{id}/info`, WHEP, `/snapshot` |
| event | `detection` (event_types = labels) | camera, zone | on | `event_started` |
| event | `alert` (event_types = source apps; attributes: severity, title, alert_id, correlation_id; signed media on request) | camera | on | `alert` + `media_ready` |
| event | `doorbell` (device_class doorbell) | camera running the smart-doorbell app | on | app descriptor |
| event | `plate`, `face` | camera | on | `event_started` / `event_ended` |
| binary_sensor | `<label> occupancy`, `all occupancy` | camera, zone | on | `object_count` |
| binary_sensor | motion | camera | on | `motion` |
| binary_sensor | online (connectivity) | camera | on | `camera_status` |
| binary_sensor | recording problem | camera | on | `recording_state` = stalled |
| binary_sensor | unacknowledged alerts | site | on | alerts inbox |
| sensor | `<label> count`, `<label> active count` | camera, zone | count on, active off | `object_count` |
| sensor | last plate, last face | camera | on | live-state |
| sensor | input fps, detection fps, skipped fps, inference ms, bitrate | camera | off (diagnostic) | `camera_stats` |
| sensor | storage used/free %, days retained, CPU %, memory %, GPU % | site | storage on, others off | `/system/resources` |
| sensor | unacknowledged alert count, highest open severity | site | on | alerts inbox |
| image | last `<label>` | camera, **zone** | on | live-state crops |
| image | last alert evidence | camera | on | `media_ready` |
| switch | detection | camera | on | §6.3 |
| switch | recording | camera | **only if the site flag is on**; then off by default | §6.3 |
| switch | `<app>` on this camera | camera × assigned app | on | skill picks (`consumer="app:<id>"`) |
| select | PTZ preset | PTZ camera | on | §6.3 |
| select | stream quality (main/sub) | camera | off | options |
| button | PTZ up/down/left/right/zoom in/out/stop | PTZ camera | on | §6.3 |
| button | acknowledge all alerts, trigger manual event | site, camera | on | §6.3 |
| button / sensor | app-declared (e.g. abandoned-object "resolve", "unattended now") | camera | per descriptor | app `entities:` |
| alarm_control_panel | site mode | site | on | §6.3 |
| update | server version | site | on | `/system/info` |

**How the table is built:**
- `camera`, `alarm_control_panel` and `update` are hand-written in the integration.
- **Every other row is an entity descriptor (§6.10)**, rendered by generic platform code. Per-app rows come from app manifests.
- Adding a row later is a server or app change, not an integration release.

**Unique ids** have the form `<site_id>:<camera_id>[:<zone_id>]:<key>` and stay stable across renames.

### 7.5 Services
All services are audited in core with the correlation id, and use `supports_response` where useful.

| Service | Fields | Returns |
|---|---|---|
| `opennvr.ptz` | action (move/zoom/stop/preset), argument | none |
| `opennvr.create_event` | camera, label, sub_label, duration, include_recording | event_id |
| `opennvr.end_event` | event_id | none |
| `opennvr.export_recording` | camera, start, end, with_hash | relay URL, sha256 |
| `opennvr.protect_recording` | event_id or camera+range, protected | none |
| `opennvr.ack_alerts` | alert_ids, or source/severity | count |
| `opennvr.search_events` | query, camera, label, zone, plate, from, to, limit | events[] with relay thumbnail URLs |
| `opennvr.summarize_period` | camera?, from, to | text (needs an AI adapter) |

### 7.6 Media browser
```
OpenNVR <site>
├── Alerts      → severity → app → date → alert (image / clip)
├── Events      → camera → label → zone → date → event (snapshot / clip)
├── Recordings  → camera → date → hour → HLS (/recordings/playback/hls)
└── Exports     → export (mp4)
```
- Thumbnails come from event evidence.
- HLS and MP4 go through an **HA-authenticated** proxy view (`/api/opennvr/{site}/vod/...`).
- Pages hold 50 items, and each level shows counts.

### 7.7 Notifications
- **Blueprint** `blueprints/automation/opennvr/alert_notification.yaml`, triggered by the `alert` or `detection` event entities at the `media_ready` stage.
- **Filters:** severity ≥ X, cameras, zones, labels, site mode, quiet hours, cooldown.
- **Media relay (off-LAN):** attachments use `/api/opennvr/{site}/m/{token}` on **HA's own URL**. That is reachable through HA's external URL or HA Cloud whenever the phone can reach HA.
  - The view forwards to core's `/api/v1/media/s/{token}`. Core validates the HMAC, expiry and object binding.
  - The view needs no HA login, because the phone's notification fetcher can't send one. It only accepts valid signed tokens, which unlock one object for a limited time.
  - The comparable proxy in existing integrations is open to anyone who knows an event id, with no expiry by default.
- **Actions:** Acknowledge (calls `ack_alerts`), Open live view, Protect footage.
- High and critical severity use the Companion app's critical alerts, and iOS gets live view in the notification.

### 7.8 Card session (for dashboard cards)
**Card data does not route through integration code.** Existing integrations need an integration change whenever their card needs a new proxy, which is one of the forever costs named in §2.
- **Session:** one stable HA websocket command, `opennvr/card_session`, returns a **short-lived (≤ 10 min), read-only, camera-scoped token** and the site's API base URL.
- **Direct access:** the card then calls the OpenNVR API, websocket v2 and WHEP **directly** with the ordinary `/api/v1` contract. Core adds CORS for the configured HA origins (§6.8).
- **Fallback:** when the browser can't reach OpenNVR directly (e.g. remote access through HA Cloud), it uses one fixed, HA-authenticated passthrough view, `/api/opennvr/{site}/passthrough/{path}`. It is restricted to the read-only path prefixes that the server publishes in `/system/info`. New card features need no integration change.
- **Advanced Camera Card:** an `opennvr` engine will be contributed upstream, built on this session model.

### 7.9 Diagnostics, Repairs and quality
- **Diagnostics:** config, versions, websocket state, skipped descriptors, and the last N messages. Tokens and URLs are redacted.
- **Repairs:**
  - `token_expiring` (in under 7 days)
  - `token_revoked` (→ reauth)
  - `firewall_blocked`
  - `server_too_old` / `integration_too_old`
  - `webrtc_hosts_unset`
  - `rtsp_not_exposed` (info)
  - `clock_skew`
  - `ssl_unverified`
- **Quality:** `quality_scale.yaml` tracks the Bronze → Gold → Platinum rules. Plus `translations/en.json` and a brands PR for the logo.
- **WHEP specifics:** MediaMTX returns a `Location` relative to its own root, which the integration rewrites to `/webrtc/...` the same way `app/src/lib/streamUrl.ts` `resolveWhepSessionUrl` does. The session is closed with DELETE; trickle ICE uses PATCH.

## 8. MQTT device discovery (core, Phase 3)
- **Real MQTT integration type:** the MQTT type becomes functional. `services/integration_service.py` currently skips it, and the UI is `app/src/views/Integrations.tsx`.
- **Generated from descriptors:** `server/services/ha_mqtt_discovery.py` generates **device-based discovery** from the entity descriptors (§6.10), so there is one source of truth.
  - It publishes to `homeassistant/device/opennvr_<site>_<camera>/config` with `dev`, `o` and `cmps`, plus one device for the site.
  - `entity_state` messages map to plain state topics.
- **Availability:** a Last Will on `opennvr/<site>/status`. Discovery is re-published whenever HA sends `homeassistant/status = online`.
- **Commands:** `opennvr/<site>/<device>/<key>/set` becomes a typed command, run as a principal bound to an `ApiToken`, with the same scopes and audit.
- **Events:** event-entity topics carry a **CloudEvents 1.0** envelope (structured mode). State topics stay plain so HA can read them directly.
- **Positioning:** the zero-install alternative, with fewer features (no media browser, services or Assist). Running it alongside the native integration isn't recommended, and the docs and a Repair warn about it.
- `examples/home-assistant-relay` is deprecated after this ships.

## 9. Assist / MCP (Phase 4)
- **`llm.py`** registers an `llm.API` named `OpenNVR` with these tools:
  - `search_events`
  - `describe_camera` (a live snapshot, plus a VLM description when one is available)
  - `list_alerts`
  - `summarize_period`
  - `ptz_goto_preset`
- Every tool is limited to the token's cameras and audited.
- Camera and image entities work with HA **AI Task** attachments with no extra work.
- **Stretch:** a core MCP endpoint (`/api/v1/mcp`) that exposes the same tools to non-HA agents.

## 10. Standards bridges (Phase 5, each starting with a spike)
### 10.1 ONVIF server (`onvif-server/` service)
- **Profile S:** WS-Discovery; the Device and Media services; `GetProfiles`; `GetStreamUri` (MediaMTX RTSP/RTSPS with a scoped token); `GetSnapshotUri`; PTZ passed through to core.
- **Profile M:** analytics events over PullPoint, plus the **MQTT JSON event broker** binding (`AddEventBroker`, topics `<prefix>/onvif-ej/...`). OpenNVR detections and alerts map to ONVIF event topics.
- **Interop tests:** HA's `onvif` integration, ONVIF Device Manager, and one commercial VMS.
- **Spike (2 weeks):** decide between building on Python SOAP and a Go base.

### 10.2 Matter 1.5 camera bridge (`matter-bridge/` service)
- **Target:** each camera becomes a Matter **Camera** endpoint with Camera AV Stream Management, a **WebRTC Transport Provider** fed from MediaMTX WHEP, and Push AV Stream Transport on alerts. Doorbell events come from the smart-doorbell app.
- **The spike's first question:** does Matter 1.5/1.5.1 allow **bridged** camera endpoints, or must each camera be its own node? This is unverified.
- **Ecosystem risk:** in production, only SmartThings supports Matter cameras; HA has experimental support in its Matter server.
- **Spike (2 weeks):** matter.js (OHF) or the CHIP camera-app example, ending in a go/no-go gate.

## 11. Beyond parity (Phase 6)
- **Cross-camera timeline card:** HA has no native timeline. Built on the card session (§7.8).
- **Two-way audio:** MediaMTX has no ONVIF audio back-channel. This is research: either a go2rtc sidecar, or a MediaMTX feature, fed by a WHIP back-channel from the browser.
- **SIA DC-09 alarm reporting** to monitoring centres, which matters to small businesses.

## 12. Parity matrix vs the established NVR integration
| Established capability | OpenNVR equivalent | Better? |
|---|---|---|
| MQTT required | Native websocket; MQTT optional (Phase 3) | ✅ |
| Camera entity: RTSP/WebRTC via go2rtc | Native WebRTC over WHEP; RTSPS opt-in | ✅ no go2rtc hop |
| Live modes MSE / jsmpeg | Covered by WebRTC plus the HA stream fallback | = |
| Combined multi-camera view | Not planned for v1 | ✗ gap, v2 |
| Occupancy and counts per camera/zone/object | Same, plus a state snapshot on reconnect and a staleness timeout | ✅ fixes restart staleness |
| Motion binary sensor | Same | = |
| Audio sensors (sound level dB, labels) | Via audio AI apps (descriptors) when installed; no core dB sensor | ◐ |
| Review status sensor | Unacknowledged alert count plus highest severity | ✅ |
| Face and plate sensors | Same, plus `plate`/`face` event entities | ✅ |
| Per known face/plate "last camera" sensors | Deferred; answerable through search | ◐ |
| Classification sensors | App-declared descriptors | ◐ → ✅ as apps adopt `entities:` |
| FPS, inference, CPU and GPU sensors | Same, plus storage, days retained and memory | ✅ |
| Image entity: last object per camera | Per camera **and per zone** | ✅ |
| Switches: detect, record, snapshots, motion, audio, review, GenAI, autotrack | Detection, camera on/off, per-app enables, and recording **only if the site admin enables `recording_pause_enabled`** (audited; recording is always on by default, for evidence integrity) | ◐ deliberate |
| Numbers: motion threshold, contour area | Out of scope for v1 (no core equivalent) | ✗ gap, v2 |
| Profile select | Site-mode alarm panel (arming) | ✅ |
| Update entity | Same | = |
| Services: ptz, export, favourite, create/end event, review summarise | All of these, plus ack, search and hashed export | ✅ |
| Export timelapse (25×) | Realtime export only | ✗ gap, v2 |
| RTSP URL template option | Not needed: stream URLs come from `/streams/{id}/info` | = |
| PTZ | Preset select and buttons | ✅ |
| Media browser: clips, snapshots, recordings | Plus Alerts and Exports | ✅ |
| Notification proxy (open, never expires by default) | Signed, expiring, single-object, audited tokens, relayed through HA so they work off-LAN | ✅ |
| Websocket API for the card (new proxy per card feature) | One stable session command; the card talks to the OpenNVR API directly; plus an ACC engine contribution | ✅ |
| LLM query tool | Five Assist tools plus AI Task | ✅ |
| Multiple instances | `site_id` per config entry | = |
| Auth: username/password | Scoped, revocable tokens; reauth; Repairs | ✅ |
| Distribution: HACS by choice, no quality grade, no Repairs | HACS by default, Gold rules met, Repairs from day one; core only against explicit criteria (§7.1) | ✅ |
| Integration change needed for most new backend or card features (the "forever cost") | Server-described entities, a compatibility contract and direct card sessions (§6.10, §6.11, §7.8) | ✅ |
| ONVIF server / Matter | Phase 5 spikes | ✅ unique (if go) |

## 13. Security and privacy
- **Least privilege:** each token has scopes, a camera allow-list, and optional CIDRs. Its effective permissions never exceed its owner's.
- **Media:** no media route exists without HA authentication or a valid signed token that unlocks one object and expires. Nothing is enumerable.
- **Commands:** descriptor and MQTT commands are typed. They never carry free URLs, and every execution is scope-checked.
- **Audit:** every command from HA, MQTT, ONVIF or Matter is audited with its actor and correlation id (§6.12). Signed-media fetches are audited too.
- **Keys:** only token hashes are stored, and secrets are shown once. The media-signing key rotates.
- **Firewall:** the device firewall now also covers the events websocket (§6.0).
- **Transport:** HTTPS through nginx. `verify_ssl` is honoured everywhere, including the proxy views (a known failure in existing integrations).
- **Recording pause** is off site-wide by default. When enabled, pausing is permissioned and audited, and can auto-resume.
- **Local-only:** the update check is opt-in, and AI tools respect `AI_SOVEREIGNTY=local_only`.

## 14. Rollout and compatibility
1. **Phase 0 (prerequisites, §6.0)** lands first.
2. **Phase 1 (core)** ships behind no flag, except recording pause. New endpoints are additive; websocket v1 clients keep working, and v2 is opt-in via `v=2`. Entity descriptors (§6.10) and the contract (§6.11) are part of Phase 1, because the integration is built on them from its first release.
3. **Phase 2:**
   - integration 0.1 (MVP): config flow, camera, descriptor-driven entities, occupancy, switches, Repairs;
   - 0.5: media browser, services, blueprint and relay, card session;
   - 1.0: Gold rules met, on HACS.
   - The first end-user release also needs `pyopennvr` published on PyPI and the two repos split out (§7.1).
4. **Phase 3** (MQTT discovery) and **Phase 4** (Assist) run in parallel after integration 0.5.
5. **Phase 5:** spikes, then go/no-go decisions.
6. **Phase 6:** after 1.0.

- **Minimum versions:** HA 2025.6 or later (the new WebRTC API only), and the OpenNVR version that ships Phase 1.
- **Delivery conventions:** one issue per PR and one commit per branch. Schema changes follow the create_all + Alembic rules in the implementation plan.

## 15. Testing
- **Core:** pytest following the per-file SQLite pattern (`server/tests/test_alerts_inbox.py`). Coverage:
  - token scopes, camera allow-list, CIDRs and the firewall (HTTP and websocket);
  - the websocket ticket;
  - signed media: expiry, tampering, single object, and rotation;
  - live-state: counts, staleness, and track start/end;
  - websocket v2 snapshot and resume;
  - audit rows with correlation ids;
  - recording pause with the flag off and on;
  - zones CRUD, and descriptor command rejection.

  Known host-baseline failures are excluded.
- **Integration:** `pytest-homeassistant-custom-component` (0.13.365, pinned to HA 2026.9.2) with a fake `pyopennvr` server. hassfest and the HACS validation action run in CI.
  - HA 2026.9 needs Python ≥ 3.14.2 and doesn't support Windows, so these tests run in a Linux `python:3.14` container.
- **Dev HA instance:** runs **outside `INTERNAL_SERVICE_CIDRS`** and reaches OpenNVR through the published nginx, as a real LAN install does. Otherwise it would bypass the device firewall and hide firewall bugs.
- **Contract (§6.11):** CI checks the fixture diff. The integration suite runs against server `main` and the last two releases. A sample app with an `entities:` section appears in HA with no integration change. Unknown platforms and fields are skipped cleanly.
- **E2E** (after the `test/e2e-suite` branch lands): `tests/e2e` gains a `ha-dev` compose profile running `homeassistant/home-assistant` with the integration mounted. Scripted checks:
  - setup → entities appear;
  - a fakecam person → occupancy on then off. This needs real footage clips, because synthetic clips give motion only;
  - `POST /alerts-inbox/test` → the alert event entity fires once `media_ready` arrives;
  - HA restart → correct state within 5 s;
  - token revoked → a Repair and reauth;
  - PTZ preset → an audit row with actor `token:<name>` and the correlation id.
- **Manual:**
  - WebRTC live view on the dashboard and in the Companion app;
  - a phone notification **on mobile data** with image and clip via the relay, whose link then expires;
  - the Acknowledge action syncs;
  - an Assist query.
- **Phase 3:** a clean HA with only MQTT discovers the devices; a broker disconnect → unavailable.
- **Phase 5:** HA's `onvif` integration adds an OpenNVR camera and receives Profile M events; the Matter spike streams to SmartThings.

## 16. Risks
| Risk | Mitigation |
|---|---|
| WHEP↔HA WebRTC edge cases (ICE, NAT, H.265); no ICE servers configured by default | Setup docs and the `webrtc_hosts_unset` Repair; fall back to RTSPS/HLS; substream option; test the Companion apps early |
| MediaMTX 1.15.4 may not serve the in-progress 60 s segment through `/playback/get` | Verify first. If it doesn't, `media_ready` for clips waits for the segment to close |
| Tier0 staleness gives false occupancy (nothing is published when there are no tracks) | Staleness timeout, and a test with a static scene |
| Websocket fan-out load with many HA clients and cameras | Throttled stats, deltas only, per-token subscription filters, a load test |
| In-memory state assumes a single uvicorn worker | Documented constraint (§5); a shared store is needed before scaling workers |
| Widening the app-action route to tokens | `apps.actions` scope, audit, security review |
| mDNS unavailable on Docker Desktop | Manual URL entry is the primary path; the sidecar is optional |
| Scope creep from site modes and zones | Each is its own issue with a minimal v1 |
| The Matter ecosystem is immature; bridged cameras unverified | Spike with a go/no-go gate |
| ONVIF server complexity and certification cost | Interop-tested only; certification is a business decision |
| Two HA paths (native and MQTT) confuse users | Docs and a Repair warn when both are active |
| Self-signed certificates vs `verify_ssl` | Clear config-flow warning, a Repair, and a guide to a proper certificate |
| "Forever cost": integration releases pile up and users hit server/integration mismatches | Descriptors (§6.10), the contract and CI matrix (§6.11), direct card sessions (§7.8), HACS by default (§7.1) |
| Generic descriptor entities feel less polished, or face pushback in a later HA core review | `translation_key` for core descriptors, a name fallback for app descriptors, and hand-written entities for the high-value surfaces |
| Mutating the owner's ORM User for a token request would persist and demote an admin | Read-only `TokenPrincipal` proxy; a grep for code that treats `current_user` as an ORM instance |
| A contextvar set inside a sync dependency is lost (copied threadpool context) | One contextvar holding a mutable request-context object (§6.12) |
| Camera off (`is_active=false`) also stops recording, so HA could get around the always-on rule | Tokens need the `recording_pause_enabled` flag to change `is_active`; `ON_OFF` is hidden without it (§6.3) |
| End users can't install the integration before `pyopennvr` is on PyPI | Pre-1.0 builds are development-only; the release is gated on the PyPI publish and the repo split (§7.1, §14) |

## 17. Open questions
1. Which organisation publishes `pyopennvr` on PyPI, and under which licence (Apache-2.0, to match the SDK)?
2. Site modes: after v1 (arming only), should modes also switch app enables per camera, or grow into a schedule-based arming system?
3. Should non-admin tokens be allowed to create manual events (`events.create`)?
4. What retention applies to evidence exports created with `with_hash`?
5. Are the core-submission criteria in §7.1 step 5 the right ones, and who reviews them each quarter?
6. Descriptor governance: who approves new `platform` values and descriptor fields, and do app descriptors need catalog review before they reach HA?
7. mDNS on Docker Desktop (Windows/macOS): is manual URL entry acceptable, or should we ship a host-side helper?
