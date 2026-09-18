# Implementation plan: OpenNVR × Home Assistant

| | |
|---|---|
| **Design** | [home-assistant-integration.md](home-assistant-integration.md) (r2) |
| **Status** | Draft |
| **Date** | 2026-09-18 |
| **Audience** | Engineers picking up the work; reviewers |

## 1. How to read this
- Work is split into **milestones (M0–M6)** of **issues (HA-xxx)**.
- Each issue lists its goal, main files, existing code to reuse, tests, acceptance criteria, dependencies and a size:
  - **S** ≤ 2 days
  - **M** 3–5 days
  - **L** 1–2 weeks
- Repos:
  - **core** = open-nvr (this repo)
  - **lib** = `pyopennvr`
  - **ha** = `hass-opennvr`

## 2. Engineering rules (apply to every issue)
- **Branches and commits:** one issue, one `SRB-*` branch, squashed to **one commit** closing the issue. Tag before rewriting history. No Co-Authored-By trailer.
- **Schema changes:**
  - Define the table or column in `server/models.py` **and** add an Alembic migration in `server/migrations/versions/` (`<12hex>_<desc>.py`).
  - New columns must be **nullable or have a `server_default`**. `init_db()` runs `create_all` and stamps fresh databases to head, so migrations never run there (`server/core/database.py:289-320`).
  - `server/tests/test_migration_graph.py` must stay green (single head).
- **New permissions** are seeded in `scripts/init_db.py`, and upgraded installs get them through the `main.py:216-258` backfill pattern.
- **Server tests** follow the per-file pattern: in-memory SQLite with `StaticPool` plus `app.dependency_overrides` (see `server/tests/test_alerts_inbox.py`). Known host-baseline failures are excluded.
- **Audit:** every state-changing endpoint calls `services/audit_service.write_audit_log`. Non-user actors (`token:<name>`, `mqtt:<name>`) go in `details`, and the correlation id goes in the new column (HA-002).
- **HA contract:** any change to REST fields the integration uses, websocket v2 messages or descriptors bumps `CONTRACT_VERSION` according to the §6.11 rules.
- **Deployment:** after each core PR, rebuild the core image for that branch and run a smoke check. See the deployment-only traps: create_all vs Alembic, the apps network, the NATS token.

## 3. Decisions needed before M1 starts
| # | Decision | Default if nobody objects |
|---|---|---|
| D1 | `pyopennvr` owner and licence | open-nvr org, Apache-2.0 (matches the SDK) |
| D2 | Site mode scope | v1 = arming affects alert delivery only |
| D3 | Can non-admin tokens create manual events? | Only with an explicit `events.create` grant |
| D4 | Retention of hashed evidence exports | Flagged as protected until an admin unflags it |
| D5 | Descriptor governance | Core team owns the `platform` and field list; app descriptors pass `opennvr-app validate` |
| D6 | mDNS on Docker Desktop | Unsupported; manual URL entry |

## 4. Milestones
| Milestone | Outcome | Exit criteria |
|---|---|---|
| **M0 Prerequisites** | The existing gaps that block HA work are fixed | ffmpeg in the core image; correlation id and audit coverage; permissions seeded; e2e suite on main; websocket covered by the firewall |
| **M1 Core foundations** | Everything HA needs exists in `/api/v1` and websocket v2 | Contract v1.0 frozen; contract fixtures published |
| **M2 Integration 0.1 (MVP)** | Install from HACS; cameras live; core entities; Repairs | E2E: setup → entities → occupancy → restart restores state |
| **M3 Integration 1.0** | Services, media browser, notifications, card session, Gold rules | Gold checklist green; blueprint works on and off the LAN |
| **M4 MQTT discovery** | Zero-install path | A clean HA with only MQTT discovers the devices |
| **M5 Assist** | LLM tools | "What happened at the gate this morning?" works |
| **M6 Standards spikes** | Go/no-go on ONVIF server and Matter bridge | A spike report for each |

**Critical path:** HA-101 (tokens) → HA-109 (websocket v2) + HA-112 (descriptors) → HA-113 (contract) → HA-201 (pyopennvr) → HA-202/203/204 → integration 0.1.

## 5. Issues

### M0: Prerequisites (core)
| ID | Issue | Main files / reuse | Tests / acceptance | Deps | Size |
|---|---|---|---|---|---|
| HA-001 | Install ffmpeg in the core runtime image | root `Dockerfile` runtime stage | `/recordings/frame` returns a JPEG on a built image | — | S |
| HA-002 | Correlation ids: accept a validated inbound `X-Correlation-Id`, else use the request id; add `AuditLog.correlation_id` (migration); pass it through `write_audit_log`; add the header to CORS allow and expose lists | `middleware/request_logging.py`, `services/audit_service.py`, `models.py`, `main.py:740-764` | A request with the header leads to an audit row carrying it; invalid values are replaced | — | M |
| HA-003 | Audit gaps: PTZ move/stop/preset, alert ack, export ticket, device-firewall approve/block, skill pick put/delete | `cameras.py:2235-2312`, `alerts_inbox.py:305`, `recordings.py:881`, `routers/device_firewall.py`, `routers/skills.py` | One test per endpoint asserting an audit row | HA-002 | M |
| HA-004 | Permissions: seed `camera_device.write`; add `ptz.control`, `events.create`, `apps.actions`, `recordings.pause`, `api_tokens.manage`; role defaults; upgrade backfill | `scripts/init_db.py`, `main.py:216-258`, `core/permissions.py` | Fresh and upgraded databases both have the permissions | — | S |
| HA-005 | Land the e2e suite (`test/e2e-suite`) on main | `tests/e2e/**`, `docker-compose.e2e.yml` | `run.py -m smoke` green | — | M |
| HA-006 | Device-firewall check in the events websocket handler | `routers/events.py:202`, `services/device_firewall_service.py` | An unapproved device's websocket is refused when enforcement is on | — | S |

### M1: Core foundations
| ID | Issue | Main files / reuse | Tests / acceptance | Deps | Size |
|---|---|---|---|---|---|
| HA-101 | **API tokens:** `ApiToken` model and migration (SHA-256 hash, `onvr_` prefix, scopes, camera allow-list, optional `allowed_cidrs`, expiry, revoke, throttled `last_used`); `services/api_tokens.py`; `routers/api_tokens.py` CRUD (shown once); `core/auth.py` resolves `Bearer onvr_…` to its owner, with effective permissions = scopes ∩ owner's permissions and cameras = allow-list ∩ visible; tokens pass the device firewall | Pattern: `services/app_keys.py`, `apps.py:_service_or_user_principal` | Scope intersection; camera limits on `/cameras` and `/alerts-inbox`; revoked or expired → 401; firewall pass; audit actor `token:<name>` | HA-002, HA-004 | L |
| HA-102 | Token management UI (Settings tab): create, show once, revoke, last used | `app/src/views/Settings.tsx` registry, `views/settings/`, `lib/api.ts`, react-query | UI flow works; en and fr locale keys | HA-101 | M |
| HA-103 | Websocket ticket for tokens (camera-scoped) | `routers/events.py:137` | Token ticket sees only its cameras | HA-101 | S |
| HA-104 | `GET /system/info`: `site_id` (a `SecuritySetting` key), name, version, `contract_version`, features, passthrough allow-list; opt-in update check | `routers/system.py`, `apps.py:_registry_info` pattern, `main.py:103 __version__` | Stable `site_id` across restarts; update check off by default | — | S |
| HA-105 | Health: per-volume storage, days retained, memory, GPU in `/system/resources`; `GET /cameras/{id}/stats` from MediaMTX path stats and tier0 metrics | `routers/system.py`, MediaMTX client, `/ai-models/tier0-metrics` source | Fields present; missing GPU → null | — | M |
| HA-106 | Controls, part 1: `Camera.detection_enabled` (nullable, default true) gating detect-pipeline and KAI-C dispatch; camera on/off via `is_active` with an audit reason (replaces privacy mode) | `models.py`, `cameras.py` PUT, detect-pipeline camera roster, KAI-C dispatch | Detection off → no tier0 publishes for that camera | HA-003 | M |
| HA-107 | Controls, part 2: PTZ presets by camera id (list, goto, set) using stored credentials; manual events `POST /events` and `PUT /events/{id}/end`; `POST /events/{id}/protect` → `recordings/flag` | Reuse `onvif_service.py:589` GetPresets, `PTZService`, `timeline_service.record_track_visit` | Preset goto audited; manual event appears in `/events` | HA-003, HA-004 | M |
| HA-108 | Recording pause behind the site flag `recording_pause_enabled` (`SecuritySetting`, default off): `POST /cameras/{id}/recording {enabled, resume_after_s?}`; permission `recordings.pause`; audit; auto-resume job; the flag appears in `/system/info` features | Re-enable `cameras.py:2133` handler logic behind the flag; update the code comment to state the new rule | Flag off → 403 with a clear message; on → pause, auto-resume and audit work | HA-004 | M |
| HA-109 | **Core zones:** `CameraZone` model and migration; CRUD at `/cameras/{id}/zones`; camera-settings zone editor reusing `GeometryEditor.tsx`; `record_track_visit` stores `zone_ids` | `models.py`, new router, `app/src/views/apps/GeometryEditor.tsx`, `services/timeline_service.py` | Zone CRUD; the event row has `zone_ids` | — | L |
| HA-110 | **live-state service:** hooked to the `tier0_track_consumer` feed; counts per (camera, zone, label) total and active (= not stationary); staleness timeout; synthesised track start/end; motion debounce; last-object crop at track confirmation (rate-limited); `GET /live-state` | `services/tier0_track_consumer.py`, zones from HA-109, the snapshot capture path | Unit tests with synthetic tier0 frames: counts, staleness → 0, start/end, crops rate-limited | HA-109 | L |
| HA-111 | **Websocket v2:** `v=2` negotiation; `seq` plus a 5-min ring buffer and `since` resume; `state_snapshot` first; new message types (`object_count`, `motion`, `recording_state`, `camera_stats`, `event_started/ended`, `alert`, `media_ready`, `site_mode`, `entity_state`, `descriptors_changed`); per-subscription filters; v1 unchanged | `services/event_bus_service.py`, `routers/events.py` | A v1 client is unaffected; resume within the window replays; outside it, a new snapshot; load test with 20 cameras × 3 clients | HA-103, HA-110 | L |
| HA-112 | **Signed media:** `services/media_signing.py` (HMAC, `key_id`, rotation in `SecuritySetting`); `POST /media/sign`; `GET /media/s/{token}` (single object; kinds: alert image, event evidence/snapshot, event clip via `/playback/get`, export); audit per fetch, rate-limited | `alerts_inbox.py:225` images, `timeline_events.py` evidence routes, `recordings.py:937` export proxy | Expiry, tamper, wrong kind or id → 404; rotation revokes; audit rows | HA-002 | M |
| HA-113 | `media_ready` emission: alerts after evidence is stored; events at visit end; clips at end + grace. **First verify** that MediaMTX 1.15.4 serves the in-progress segment | `services/alerts_inbox.py`, `timeline_service.py`, HA-111 | An alert → `media_ready` → the signed clip plays | HA-111, HA-112 | M |
| HA-114 | **Entity descriptors:** core registry (occupancy, counts, motion, health, controls, recording switch when the flag is on); **server-side state resolution** → `entity_state`; typed commands (`core_control` enum, `app_action`); `GET /entities` with ETag; SDK `AppManifest.entities` (dataclass, `to_dict`, facade `_manifest_kwargs`, `validate.check_manifest`); token access to `/apps/{id}/actions` with `apps.actions` | `sdk/.../manifest.py:235-396`, `facade.py:989`, `validate.py:173`, `apps.py:1343`; StateView metrics as the value source | Sample app with `entities:` → descriptors listed and states pushed; forbidden command types rejected | HA-101, HA-111 | L |
| HA-115 | **Compatibility contract:** `CONTRACT_VERSION`, deprecation list in `/system/info`, published JSON-schema fixtures (REST subset, websocket v2, descriptors); CI check that fails on a breaking fixture diff without a major bump | new `server/contract/`, `.github/workflows/ci.yml` | CI blocks a field removal | HA-104, HA-111, HA-114 | M |
| HA-116 | Search API (structured): `/search` over `TimelineEvent` and `AppAlert` with camera, label, zone, plate and time filters, limited to the token's cameras | new router | Filters and scoping tested | HA-109 | S |
| HA-117 | LAN exposure: `RTSPS_BIND_HOST`, `MEDIAMTX_WEBRTC_HOSTS` and `CORS_ORIGINS` docs; optional `mdns-announcer` sidecar (compose profile `mdns`, host network, advertises `_opennvr._tcp`) | `docker-compose.yml`, new `scripts/mdns-announcer/` | On Linux, HA discovers the service; the docs cover Windows/macOS | HA-104 | M |
| HA-118 | Site mode v1: `GET/PUT /site-mode`, stored in `SecuritySetting`; gates alarm actions and notification delivery; websocket `site_mode` | `services/alarm_actions.py`, HA-111 | armed_away vs disarmed changes delivery; audited | HA-111 | M |

### M2: `pyopennvr` and integration 0.1 (lib and ha repos)
| ID | Issue | Contents | Tests / acceptance | Deps | Size |
|---|---|---|---|---|---|
| HA-201 | `pyopennvr` 0.1 | aiohttp client; token auth; typed models; websocket v2 client with ticket refresh, `since` resume and snapshot handling; descriptor models; tolerant reader; contract fixtures from HA-115 | Fixture replay tests; resume and gap tests | HA-115 | L |
| HA-202 | Integration skeleton | `manifest.json` (no MQTT), config flow (manual URL, `verify_ssl`, token, cameras), zeroconf step, reauth, reconfigure, options, coordinator (websocket primary, 30 s REST fallback), diagnostics (redacted), CI (hassfest, HACS action, pytest-homeassistant-custom-component) | Config-flow tests (all branches); coordinator snapshot and delta | HA-201 | M |
| HA-203 | Camera entity | `async_handle_async_webrtc_offer` → WHEP POST with the MediaMTX token from `/streams/{id}/info`; `Location` rewrite; PATCH trickle; DELETE on close; still image; `ON_OFF` → `is_active`; `stream_source` RTSPS when exposed | Browser live view on the LAN; session closed on stop (no leaked MediaMTX sessions) | HA-202 | L |
| HA-204 | Generic descriptor platforms | sensor, binary_sensor, switch, select, button, number, event, image; unique ids `<site>:<device>:<key>`; unknown platforms and fields skipped (listed in diagnostics); command dispatch | Descriptor fixtures → entities; unknown platform skipped | HA-202 | L |
| HA-205 | Hand-written `update` and `alarm_control_panel` entities | — | State and service tests | HA-202 | S |
| HA-206 | Repairs | `token_expiring`, `token_revoked` (→ reauth), `firewall_blocked`, `server_too_old` / `integration_too_old`, `webrtc_hosts_unset`, `ssl_unverified`, `clock_skew`, `rtsp_not_exposed` | One test per Repair | HA-202 | M |
| HA-207 | E2E `ha-dev` profile | HA container with the integration mounted; scripted checks: setup, entities, fakecam occupancy, HA restart restores state within 5 s, token revoke → Repair | Green in `tests/e2e` | HA-005, HA-203, HA-204 | M |
| — | **Release 0.1** on HACS (custom repository) | — | — | all of the above | — |

### M3: Integration 1.0
| ID | Issue | Contents | Deps | Size |
|---|---|---|---|---|
| HA-301 | Services | `ptz`, `create_event`/`end_event`, `export_recording` (signed URL + SHA-256), `protect_recording`, `ack_alerts`, `search_events` (response), `summarize_period` | HA-107, HA-112, HA-116 | M |
| HA-302 | Media browser | Alerts / Events / Recordings / Exports trees; authenticated HLS and MP4 proxy view; thumbnails | HA-112 | L |
| HA-303 | Notification relay and blueprint | `/api/opennvr/{site}/m/{token}` relay (off-LAN via the HA external URL); blueprint with severity, zone, site-mode and quiet-hours filters; `media_ready` trigger; Ack / Live / Protect actions; critical alerts for high and critical | HA-113, HA-301 | M |
| HA-304 | Card session | `opennvr/card_session` websocket command (≤10-min read-only token); fixed passthrough view restricted by the server allow-list; core CORS for configured origins; ACC `opennvr` engine PR upstream | HA-101, HA-104 | L |
| HA-305 | Gold readiness | `quality_scale.yaml`, translations, docs, brands PR, HACS default-list submission | all M3 | M |
| — | **Release 1.0** | — | — | — |

### M4: MQTT device discovery (core)
| ID | Issue | Contents | Deps | Size |
|---|---|---|---|---|
| HA-401 | Real MQTT integration type | Broker config through `Integrations.tsx` (currently a stub in `integration_service.py`); connection and test | HA-101 | M |
| HA-402 | Discovery from descriptors | `services/ha_mqtt_discovery.py`: device-based discovery (`dev`/`o`/`cmps`) generated from descriptors; availability LWT; republish on `homeassistant/status`; `entity_state` → state topics | HA-114, HA-401 | L |
| HA-403 | Commands and events | `…/set` topics → typed commands as a token-bound principal (scopes and audit); CloudEvents on the event topic | HA-402 | M |
| HA-404 | Deprecate `examples/home-assistant-relay` | Docs, catalog note, `ROADMAP.md` and `COMPARISONS.md` updated | HA-402 | S |

### M5: Assist
| ID | Issue | Contents | Deps | Size |
|---|---|---|---|---|
| HA-501 | `llm.py` tools | search_events, describe_camera, list_alerts, summarize_period, ptz_goto_preset, all scoped and audited | HA-301 | M |
| HA-502 | Semantic search path | `/search?q=` routed to a KAI-C embedding or VLM adapter when installed; respects `AI_SOVEREIGNTY` | HA-116 | M |

### M6: Standards spikes (2 weeks each, go/no-go)
- **HA-601 ONVIF server:**
  - Profile S (discovery, media, stream URI, snapshot, PTZ) and Profile M events (PullPoint + MQTT JSON).
  - Choose Python SOAP or a Go base.
  - Interop with HA's `onvif` integration and ONVIF Device Manager.
- **HA-602 Matter 1.5 bridge:**
  - First question: are bridged camera endpoints allowed?
  - Then matter.js vs the CHIP camera app; WebRTC fed from WHEP; test with SmartThings.
- **Phase 6 backlog** (not scheduled): timeline card, two-way audio (go2rtc sidecar research), SIA DC-09.

## 6. Rough effort (1–2 engineers)
| Milestone | Estimate |
|---|---|
| M0 | ~2 weeks |
| M1 | ~8–10 weeks (HA-110, 111 and 114 are the heavy items) |
| M2 | ~5–6 weeks |
| M3 | ~5 weeks |
| M4 | ~3 weeks |
| M5 | ~2 weeks |
| M6 | 4 weeks |

M4 and M5 can overlap M3.

## 7. Verification per milestone
- **M0/M1:** `uv run pytest` in `server/` (new tests per issue); `run.py -m smoke`; the contract CI check.
- **M2/M3:** integration CI (hassfest, HACS, pytest); the e2e `ha-dev` profile. Manual checks:
  - WebRTC live view in the browser and the Companion app;
  - a notification on mobile data (relay) whose link expires;
  - Ack syncs with the OpenNVR inbox;
  - the audit log shows `token:<name>` plus the correlation id.
- **M4:** a clean HA with only MQTT; broker disconnect → unavailable; a command is audited.
- **M5:** an Assist query answered using only the token's cameras.

## 8. Risks tracked during implementation
| Risk | Owner issue | Mitigation |
|---|---|---|
| MediaMTX in-progress segment not playable | HA-113 | Verify first; otherwise make `media_ready` for clips wait for segment close |
| Tier0 staleness and false occupancy | HA-110 | Timeout, plus a test with a static scene |
| Websocket load | HA-111 | Filters, throttling, load test |
| Widening the app-action route | HA-114 | `apps.actions` scope, audit, security review |
| mDNS unavailable on Docker Desktop | HA-117 | Manual URL is primary |
| HA API churn | HA-203 | Pin a minimum HA version; nightly job against HA beta |
