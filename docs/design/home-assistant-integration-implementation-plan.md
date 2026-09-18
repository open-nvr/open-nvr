# Implementation plan: OpenNVR × Home Assistant

| | |
|---|---|
| **Design** | [home-assistant-integration.md](home-assistant-integration.md) (r3) |
| **Status** | In progress (see [Progress](#9-progress)) |
| **Revision** | r3 (2026-09-18): execution revisions from code, HA 2026.9 API and environment research, plus a self-review. r2: first issue breakdown |
| **Audience** | Whoever implements or reviews this work |

## 1. How to read this
- Work is split into **milestones (M0–M6)** of **issues (HA-xxx)**, in execution order.
- **Sizes:** S ≤ 1 working session, M = 2–3 sessions, L = 4+ sessions.
- **Code homes:**
  - **core** = this repo.
  - **lib** = `integrations/home-assistant/pyopennvr/`.
  - **ha** = `integrations/home-assistant/hass-opennvr/`.
  - Both `integrations/` folders mirror their future repo roots and are split out at 1.0 (HA-306).
- Line numbers were taken on 2026-09-18 (main `9fc355e`). Re-check them before editing.

## 2. Engineering rules (every issue)
- **Branches and commits:**
  - One issue, one `SRB-ha-<id>-<slug>` branch, stacked on the previous issue's branch, squashed to **one commit** (`feat(ha): <title> (HA-xxx)`). No Co-Authored-By trailer.
  - Branches stay local and are pushed **per milestone**, after:
    - a `git rebase --update-refs origin/main` of the whole stack (backup tags first);
    - a full retest;
    - a code and security review pass;
    - the owner's go-ahead.
- **Schema changes:**
  - Every change goes in `server/models.py` **and** an Alembic migration in `server/migrations/versions/` (`<12hex>_<desc>.py`). Head at the start was `b7e4a1c9d302`; HA-002 added `c4d8e2f1a9b3`.
  - New columns are **nullable or have a `server_default`**, because `init_db()` runs `create_all` and stamps fresh databases, so migrations never run there (`server/core/database.py:289-320`).
  - Every migration implements `downgrade()`. `server/tests/test_migration_graph.py` stays green.
- **Permissions:**
  - New permissions are seeded in `scripts/init_db.py` and backfilled on upgrade with the `main.py:216-258` pattern.
  - **Grants must preserve behaviour.** No existing user may gain or lose an ability as a side effect.
- **Server tests:** per-file in-memory SQLite with `StaticPool` and `app.dependency_overrides` (e.g. `server/tests/test_alerts_inbox.py`). Results are compared with the recorded host baseline (§8).
- **Integration and lib tests:** run in a Linux `python:3.14` container (`scripts/ha-dev/test-integration.ps1`). HA 2026.9 needs Python ≥ 3.14.2 and doesn't support Windows.
- **Audit:** every state-changing endpoint calls `services/audit_service.write_audit_log`. The actor and correlation id come from the request context (HA-002).
- **HA contract:** changes to the REST fields HA uses, websocket v2 messages or descriptors follow the §6.11 rules of the design, and bump `CONTRACT_VERSION`.
- **Live verification:**
  1. Build **one reusable tag**, `ghcr.io/open-nvr/core:ha-dev`.
  2. Recreate only core, with the stack's own `-f`/`--profile` set: `$env:CORE_TAG='ha-dev'; docker compose … up -d --no-deps opennvr-core`.
     - **Never** edit `.env`.
     - **Never** use `--remove-orphans`: it deletes the fakecams and apps-profile containers.
  3. Restore `core:main` at the end of each session.
  4. Read core's logs from the supervisord files inside the container, and audit rows through the audit-logs API.
- **Security traps to avoid** (found in review):
  - **Never mutate the ORM `User` on behalf of a token.** Tokens resolve to a read-only `TokenPrincipal` proxy (HA-101).
  - **Never `ContextVar.set()` inside a sync dependency.** It runs in a copied threadpool context; mutate the shared `RequestContext` object instead (HA-002).
  - **`is_active=false` stops recording.** Tokens need the `recording_pause_enabled` flag to change it (HA-106/108).

## 3. Execution revisions (r3): research findings that shaped this plan
| # | Finding | Consequence |
|---|---|---|
| R1 | HA 2026.9 needs Python ≥ 3.14.2. `pytest-homeassistant-custom-component==0.13.365` pins HA 2026.9.2. There are no Windows HA tests. | Integration and lib tests run in a Linux py3.14 container. |
| R2 | The most widely used existing NVR integration predates current HA practice (`hass.data`, no reauth or repairs, ICE ignored). | Nothing is copied. The integration is written against HA 2026.9's developer docs, and existing integrations are only a reference for how the APIs behave and which problems to avoid. |
| R3 | HA signs relative, query-free media URLs itself (`async_sign_path`, 24 h), and the signature covers one exact path. | The media browser uses `requires_auth` proxy views with query-free paths. HLS segments are validated by prefix against the manifest signature. |
| R4 | Native HA WebRTC: `async_handle_async_webrtc_offer`, `async_on_webrtc_candidate`, `close_webrtc_session`. | WHEP: POST the offer, rewrite `Location` to `/webrtc/…` (as `app/src/lib/streamUrl.ts` does), PATCH candidates, DELETE on close. |
| R5 | Camera scoping lives in 6+ unshared helpers. | A central gate in the token resolver, plus fixes to each helper and a test per helper. |
| R6 | Bespoke auth in `recordings._authenticate_request` and `apps._service_or_user_principal`. | Both gain the `onvr_` branch. |
| R7 | `get_current_superuser` guards 117 routes; there are 51 inline `is_superuser` checks. | Tokens never pass superuser routes; the `TokenPrincipal` reports `is_superuser=False`. |
| R8 | Named permissions are enforced on only about 6 routes. | `RequirePermission` is added to every route HA uses. |
| R9 | `write_audit_log` commits internally; there is no request context. | New `core/request_context.py`: one contextvar holding a mutable object. |
| R10 | Detect-pipeline already honours roster `analyze`, and KAI-C dispatch runs in that worker. | The detection flag is a column plus a roster field. |
| R11 | The tier0 consumer returns early when the overlay is off and drops non-drawable tracks. | live-state hooks in before those filters. |
| R12 | PTZ move/stop use `onvif_digest_service`; presets exist only in IP-keyed onvif-zeep code. | New digest preset functions that reuse the `PTZService` config cache. |
| R13 | Browser websockets can't send `X-Device-Token`. | The firewall decision is bound into the websocket ticket at mint time. |
| R14 | There is no shared `SecuritySetting` helper. | New `services/site_settings.py`. |
| R15 | `create_all` plus stamping on fresh databases. | Rules in §2. |
| R16 | The e2e suite is on the local-only branch `test/e2e-suite` (210 behind main); `tests/e2e/.artifacts` holds credentials. | HA-005 rebases the branch; `.artifacts` is gitignored (done in HA-000). |
| R17 | C: has 22 GB free (Docker's disk lives there); D: has 48 GB. | One reusable image tag; HA dev config on D:. |
| R18 | `hass-web-proxy-lib` exists. | We write our own small aiohttp streaming proxy (fewer dependencies, core-eligible). |
| R19 | Since HA 2026.3 an integration can ship a local `brand/` folder. | `hacs.json` sets `homeassistant: 2026.3.0` and ships `brand/icon.png`. |

## 4. Decisions
| # | Decision | Status |
|---|---|---|
| D0 | Code home and delivery | **Locked:** monorepo until 1.0; stacked local branches, pushed per milestone |
| D1 | `pyopennvr` owner and licence | Default: open-nvr org, Apache-2.0 (matches the SDK). Confirm before HA-306. |
| D2 | Site mode scope | Default: v1 = arming affects alert delivery only |
| D3 | Can non-admin tokens create manual events? | Default: only with an explicit `events.create` grant |
| D4 | Retention of hashed evidence exports | Default: protected until an admin unflags them |
| D5 | Descriptor governance | Default: the core team owns `platform` and the fields; app descriptors must pass `opennvr-app validate` |
| D6 | mDNS on Docker Desktop | Default: unsupported; manual URL entry |
| D7 | Recording pause | **Locked:** behind the site flag `recording_pause_enabled`, off by default |

## 5. Milestones
| Milestone | Outcome | Exit criteria |
|---|---|---|
| **M0 Prerequisites** | Existing gaps that block HA work are closed | ffmpeg in core; correlation ids and audit coverage; permissions seeded; e2e suite on main; websocket firewall; segment-playback spike answered |
| **M1 Core foundations** | Everything HA needs exists in `/api/v1` and websocket v2 | Contract 1.0.0 frozen; fixtures published; server suite and e2e smoke green |
| **M2 Integration 0.1** | Dev integration: cameras live, core entities, Repairs | e2e: setup → entities → occupancy → HA restart restores state |
| **M3 Integration 1.0** | Services, media browser, notifications, card session, Gold rules | Gold checklist green; notification relay verified on mobile data; repo split and PyPI publish (with the owner) |
| **M4 MQTT discovery** | Zero-install path | A clean HA with only MQTT discovers the devices |
| **M5 Assist** | LLM tools | An Assist query answered within the token's cameras |
| **M6 Spikes** | ONVIF server and Matter bridge go/no-go | A spike report for each |

## 6. Issues (execution order)

### M0: Prerequisites (core)
| ID | Issue | Edit map (key files and reuse) | Tests / acceptance | Size |
|---|---|---|---|---|
| HA-000 | Execution setup | This r3; design r3; the tracker; `integrations/home-assistant/` scaffold; `scripts/ha-dev/`; `.gitignore` for e2e artifacts; baseline test run | Scripts run; baseline recorded (§8) | S |
| HA-001 | ffmpeg in the core image | Root `Dockerfile` runtime stage (apt list, lines 81-91) | `GET /recordings/frame` returns a JPEG on a built image | S |
| HA-002 | Request context and correlation ids | New `core/request_context.py` (contextvar holding a mutable `RequestContext`); `middleware/request_logging.py:67-169` (validate inbound `X-Correlation-Id`, else the request id; set before the quiet-path return; response header); `AuditLog.correlation_id` plus migration; `write_audit_log` defaults from the context and merges the actor into `details.actor`; CORS allow/expose (`main.py:756-763`) | Inbound, invalid and generated ids; audit row carries it; actor mutation visible from both sync and async routes | M |
| HA-003 | Audit gaps | The `cameras._record_audit_log` pattern (`cameras.py:190`) at: PTZ move/stop (2235, 2284); `alerts_inbox.acknowledge` (305); `recordings.create_export_ticket` (881) and `set_recording_flag` (821); `routers/device_firewall.py` (91-136); `routers/skills.py` declare/release (133, 163) | One audit assertion per endpoint | M |
| HA-004 | Permissions | `scripts/init_db.py` seeds `camera_device.write`, `ptz.control`, `events.create`, `apps.actions`, `recordings.pause`, `api_tokens.manage`. **Grants:** `ptz.control` to every role holding `live.view`; nothing else beyond admin/full_access. Upgrade tuple in `main.py:216-258`; `backfill_permission(db, source, target)` generalised from `services/apps_view_backfill.py` | Fresh and upgrade DB; a viewer can still PTZ; an operator gains nothing | S |
| HA-005 | Land the e2e suite | Rebase `test/e2e-suite` onto main (backup tag); resolve conflicts | `tests/e2e/run.py -m smoke` green (needs the dev stack stopped: container names collide) | M |
| HA-006 | Websocket device firewall | `_mint_ws_ticket` (`events.py:111`) stores the firewall decision and IP; handler (202-341) re-checks before `accept()` using `enforcement_active_cached`, `get_client_ip`, `is_loopback`, `is_internal_service` | Enforcement on and unapproved → close 1008 | S |
| HA-007 | Spike: in-progress segment playback | Read-only probe of MediaMTX `/playback/list` and `/get` for `now-20s…now-5s` | Result recorded in the tracker; sets the HA-113 clip timing | S |

### M1: Core foundations
| ID | Issue | Edit map | Tests / acceptance | Size |
|---|---|---|---|---|
| HA-104 | `/system/info` + `services/site_settings.py` | `routers/system.py`; `site_id` UUID in `SecuritySetting`; `contract_version`, `features[]` (incl. `recording_pause_enabled`), opt-in `UPDATE_CHECK`; model on `apps._registry_info` (`apps.py:1004`) | Stable `site_id`; flag reflected | S |
| HA-105 | Health and camera stats | `SystemMonitorService.sample()` (`system_monitor_service.py:104-150`): volumes, memory, GPU (null); `days_retained`; `GET /cameras/{id}/stats`: 10 s `bytesReceived` sampler via `MediaMtxAdminService.get_active_path_info`; new reducers in `services/tier0_metrics.reduce_metrics` for latency and skipped per camera; `_derive_recording_state` | Reducer unit tests on sample metrics; endpoint with MediaMTX mocked | M |
| HA-101 | API tokens | `ApiToken` plus migration; `services/api_tokens.py` (the `app_keys` pattern: SHA-256, `compare_digest`, `onvr_`); `routers/api_tokens.py` (`api_tokens.manage`; tokens can't mint tokens). `core/auth.get_current_user(request, …)` returns a **read-only `TokenPrincipal`** (never mutates the User). It mutates the request-context actor, checks `allowed_cidrs`, and throttles `last_used`. **Central camera gate:** `camera_id`, `cam_id`, `camera_ids`, `cam-N` / `camN` handles; body endpoints check explicitly. `get_current_superuser` refuses tokens; `user_has_permission` / `RequirePermission` require the scope. Also fix: `camera_scope.visible/manageable_camera_ids`, `CameraService.get_camera_by_id` / `user_has_permission`, `recordings._can_view_camera` / `_viewable_cameras`, `streams._check_camera_permission`, the `GET /cameras` list, `recordings._authenticate_request`, `apps._service_or_user_principal`; the device firewall passes tokens. Grep for ORM-instance uses of `current_user`. | Matrix: token × every scoping helper; revoked/expired; CIDR; superuser refused; audit actor; admin row never modified | L |
| HA-102 | Token UI | `app/src/views/Settings.tsx` registry, `views/settings/ApiTokens.tsx`, react-query, en/fr locales | `npm run build`; manual SPA pass | M |
| HA-103 | Websocket ticket for tokens | `events.py:137` mint carries the token and its allow-list; `_ws_scope_for` (190) intersects | Token websocket sees only its cameras | S |
| HA-106 | Detection flag | `Camera.detection_enabled` plus migration; `CameraUpdate` / `CameraResponse`; `update_camera` pops `reason` before the setattr loop (like `assignments` at 1111) and audits it; roster `list_camera_agent_sources` (~822-834) emits `analyze` | Flag off → no tier0 for that camera (checked on the live `tracks` ws) | S |
| HA-107 | PTZ presets, manual events, protect | `onvif_digest_service`: `ptz_get/goto/set_preset_digest` (next to 687/719); `PTZService.presets`; routes `/cameras/{id}/ptz/presets[...]` with `ptz.control` + audit, and `ptz.control` on move/stop **in addition to** the existing ownership check (never replacing it); `timeline_service.record_manual_event` / end; `routers/timeline_events.py` `POST /events` (`events.create`), `PUT /events/{id}/end`, `POST /events/{id}/protect` (helper factored out of `set_recording_flag`) | Preset goto audited; manual event listed; protect flags the segments | M |
| HA-108 | Recording pause (flagged) + camera on/off for tokens | `POST /cameras/{id}/recording`: site flag first, then `recordings.pause`; reuses the body of `toggle_camera_recording` (2141-2206); persisted auto-resume via `spawn_background`; audit; rule comment at 2133 updated; `PUT /system/settings/recording-pause` (superuser). **Tokens changing `is_active` also need the flag.** | Flag off → 403; on → pause, resume, audit; token `is_active` blocked without the flag | M |
| HA-109 | Core zones | `CameraZone` plus migration and CRUD; zone editor reusing `GeometryEditor` (props 110-131); `TimelineEvent.zone_ids`; detect-pipeline `events_poster` sends an optional `best_box` → `TrackEventIn` → `record_track_visit` computes zones | CRUD; the event row has zones; detect-pipeline tests | L |
| HA-110 | live-state | `services/live_state.py` (pure logic): per camera/zone/label total and active (`not stationary`) counts, `LIVE_STATE_STALE_S`, track start/end, motion debounce, last plate/face, rate-limited last-object crop (`capture_frame_bytes` → `save_evidence_jpeg`). Hooked into `tier0_track_consumer._handle_message` before `to_overlay_payload`; the overlay flag gates only overlay publishing. `GET /live-state` | Synthetic tier0 frames: counts, staleness, start/end, zones; live fakecam counts | L |
| HA-111 | Websocket v2 | `event_bus_service`: `seq`, 5-min ring buffer, `event_type` filters, new publishers; `events.py`: `v=2`, `since`, `state_snapshot` first; extended alert publish (`alerts_inbox._handle_message` 352-360) | v1 shape pinned; resume/replay/snapshot; `scripts/ha-dev/ws_load.py` with 20 subscribers | L |
| HA-112 | Signed media | `services/media_signing.py` (HMAC, `kid`, rotation in site_settings); `POST /media/sign`, `GET /media/s/{token}`; sources `evidence_store.resolve_evidence`, event evidence, and `_stream_playback_clip` factored out of `recordings.export_clip` (956-1007); audited fetch; `release(db)` before streaming | Expiry, tamper, wrong kind → 404; rotation revokes | M |
| HA-113 | `media_ready` | Alerts when stored; events after `record_track_visit`; clip timing from HA-007 | Alert → `media_ready` → signed clip plays | M |
| HA-118 | Site mode v1 | `/site-mode` (`settings.view` / `settings.manage`), site_settings; gate in `alarm_actions.dispatch_alarm_actions` (201-204), bypassed on `force`; websocket `site_mode`; audit | Arming changes delivery | M |
| HA-114 | Entity descriptors | `services/entity_descriptors.py`: core descriptors (the recording switch only when the flag is on); app descriptors from `manifest_json["entities"]`; server-side state resolution (live_state, stats, a cached 5 s poll of the app's `/state` reusing the `get_app_status` fetch, a Python dot-path evaluator); `entity_state` + `GET /entities` (ETag); typed commands `POST /entities/{key}/command` (`core_control` enum, `app_action` via `invoke_app_action`, widened to tokens with `apps.actions` + audit); SDK `Entity` dataclass, `AppManifest.entities`, `to_dict`, `validate.check_manifest`, facade; version bump (**check `publish-sdk.yml` for auto-publish first**); `descriptors_changed`; retrofit `examples/abandoned-object` | Descriptors listed and states pushed; forbidden commands rejected; SDK tests | L |
| HA-115 | Contract | `server/contract/` JSON schemas; `CONTRACT_VERSION = "1.0.0"`; `scripts/contract_check.py` in CI | CI blocks a breaking diff | M |
| HA-116 | Search API | `/search`, structured, over events (incl. `zone_ids`) and alerts; camera-scoped | Filters and scoping | S |
| HA-117 | LAN exposure | Docs for `RTSPS_BIND_HOST`, `MEDIAMTX_WEBRTC_HOSTS`, `CORS_ORIGINS`; `scripts/mdns-announcer/` (host network, profile `mdns`, Linux-only) | TXT-builder unit tests; **unverified end to end on Docker Desktop** | M |

### M2: `pyopennvr` and integration 0.1
| ID | Issue | Contents | Size |
|---|---|---|---|
| HA-201 | `pyopennvr` | aiohttp client (injected session); `OpenNVRError` / `OpenNVRAuthError` / `OpenNVRConnectionError`; typed models; `EventStream` (ticket, v2, `since`, snapshot, backoff, tolerant reader); WHEP helper; contract-fixture tests | L |
| HA-202 | Skeleton | `ConfigEntry[OpenNVRData]` runtime_data; coordinator with `config_entry=` (push plus 30 s refresh); config flow (user, zeroconf, reauth, reconfigure, options); `async_set_unique_id(site_id)`; diagnostics; base entity; manifest (no MQTT); `hacs.json`; `brand/`; `quality_scale.yaml`; test helpers on phcc fixtures | M |
| HA-203 | Camera | WebRTC via WHEP (answer, candidate PATCH, DELETE, `WebRTCError`); client config with ICE; still image; detection toggle; `ON_OFF` **only when the flag is on**; RTSPS `stream_source` | L |
| HA-204 | Descriptor platforms | sensor, binary_sensor, switch, select, button, number, event, image; dynamic add/remove; stale-device cleanup; unknown platforms skipped | L |
| HA-205 | Update + alarm panel | — | S |
| HA-206 | Repairs | The ids in design §7.9; fix flows | M |
| HA-207 | E2E `ha-dev` | Automated onboarding and config flow; entities; occupancy; restart restore; revoke → Repair; audit correlation | M |

### M3: Integration 1.0
| ID | Issue | Contents | Size |
|---|---|---|---|
| HA-301 | Services | `services.yaml`, `SupportsResponse` | M |
| HA-302 | Media browser | Own identifiers; `requires_auth` proxy views; HLS prefix validation; own streaming proxy | L |
| HA-303 | Notification relay + blueprint | `/api/opennvr/{site}/m/{token}` (signed tokens only); blueprint | M |
| HA-304 | Card session | `opennvr/card_session`; allow-listed passthrough; CORS; ACC engine patch for the owner to submit | L |
| HA-305 | Gold readiness | `quality_scale.yaml`; translations (incl. exceptions and icons); docs; CI (py3.14 pytest + hassfest at `integrations/home-assistant/hass-opennvr`) | M |
| HA-306 | Split and release | `git subtree split` for both folders; the owner creates the repos, publishes `pyopennvr` to PyPI and tags the release; HACS action on the new repo | M |

### M4: MQTT discovery (core)
| ID | Issue | Contents |
|---|---|---|
| HA-401 | Real MQTT integration type | `integration_service.py`, `Integrations.tsx`, connection test (`aiomqtt`) |
| HA-402 | Discovery from descriptors | Device-based payloads; LWT; re-publish on HA online |
| HA-403 | Commands and events | `…/set` → typed commands (token-bound principal); CloudEvents on the event topic |
| HA-404 | Deprecate `home-assistant-relay` | ROADMAP, COMPARISONS, catalog note |

### M5: Assist
| ID | Issue | Contents |
|---|---|---|
| HA-501 | `llm.py` | `llm.API` registered once globally, five tools |
| HA-502 | Semantic search | Inspect and reuse the `footage-search` app first; `/search` stays the one contract |

### M6: Spikes
- **HA-601 ONVIF server** (Profile S + M PullPoint prototype).
- **HA-602 Matter bridge** (the first question is whether bridged cameras are allowed at all).

Each spike ends with `docs/design/spikes/<name>.md`.

## 7. Risks tracked during implementation
| Risk | Owner issue | Mitigation |
|---|---|---|
| In-progress segment not playable | HA-007 / HA-113 | Wait for the segment to close; send the image first and the clip later |
| WHEP trickle quirks | HA-203 | Non-trickle fallback (gather all candidates first) |
| Token gate misses a camera path | HA-101 | Per-helper test matrix; grep of camera params |
| Admin demoted through ORM mutation | HA-101 | `TokenPrincipal`; ORM-use grep; a test asserts the admin row is unchanged |
| e2e rebase conflicts | HA-005 | Resolve toward main; cherry-pick harness and smoke only if it balloons |
| HA API drift | M2+ | Pinned to 2026.9.2; bumping is its own issue |
| C: disk | all | One image tag; prune dangling layers per milestone; HA config on D: |

## 8. Test baseline (recorded in HA-000)
Recorded 2026-09-18 on the Windows dev host, base `58c59e1` (main `9fc355e` + design docs). Later runs must show **no failures beyond these**.

| Suite | Command | Result | Known host-only failures |
|---|---|---|---|
| server | `server\.venv\Scripts\python -m pytest tests -q` (py3.14) | 1519 passed, 7 skipped, **4 failed** | `test_m1b_mediamtx_hardening::test_mediamtx_external_rtsps_url_defaults_to_none`, `…::test_browser_urls_fall_through_to_internal_when_externals_unset` (dirty `server/.env`), `…::test_cert_script_is_idempotent` (no WSL bash), `test_m1c_transport_probe::test_probe_supported_against_real_tls_listener` (temp-file lock) |
| sdk | `uv run --frozen --group dev pytest -q` in `sdk/opennvr-app-sdk` | 636 passed, 1 skipped, **4 failed** | `test_credentials::test_site_key_until_an_app_key_is_issued` (POSIX file mode), `test_docs_site::test_no_page_is_orphaned_from_the_nav` (backslash paths), `test_frame_sources::test_factory_routes_file_scheme` (Windows `file://` parsing), `test_scaffold_and_config::test_scaffolded_app_smoke_test_passes` (WinError 10106 in a subprocess) |
| pyopennvr | `scripts/ha-dev/test-integration.ps1 -Suite lib` | 1 passed | — |
| hass-opennvr | `scripts/ha-dev/test-integration.ps1 -Suite ha` + `scripts/ha-dev/hassfest.ps1` | 1 passed; hassfest 0 invalid, 0 warnings | — |

All eight host failures come from the Windows host, not the code; they pass on Linux CI.

## 9. Progress
| ID | Status | Branch | Commit | Notes |
|---|---|---|---|---|
| HA-000 | done | SRB-ha-000-execution-setup | (this commit) | Scaffold + dev scripts verified: both suites pass in the py3.14 container; hassfest clean. Baselines in §8. The `test_*.py` gitignore needed a negation for `integrations/home-assistant/**/tests`. PowerShell 5.1 scripts use `$ErrorActionPreference='Continue'` plus `$LASTEXITCODE`, because docker writes progress to stderr. |
| HA-001 | done | SRB-ha-001-core-ffmpeg | (this commit) | Before: `core:main` `/recordings/frame` → 502 "Could not extract frame". After, on `core:ha-dev`: 200 image/jpeg for cams 1 and 3 (ffmpeg 7.1.5). The image grows ~330 MB (1.57 → 1.9 GB; Debian ffmpeg pulls codec libs), more than the ~100 MB estimated. Added `scripts/ha-dev/swap-core.ps1` (recreates core only, carries `OPENNVR_HOST_IP`/`OPENNVR_LAN_IPS`, never edits `.env`) and `mint-jwt.ps1`. |
| HA-002 | done | SRB-ha-002-correlation-ids | (this commit) | `core/request_context.py` (one contextvar holding a mutable object); middleware sets it before the quiet-path return and echoes `X-Correlation-Id`; `audit_logs.correlation_id` via migration `c4d8e2f1a9b3`. **The real head was `b7e4a1c9d302`, not `a3f19c7d2e60` as §2 assumed.** The audit API returns and filters `correlation_id`. Live: the migration applied on core:ha-dev and a logout with `X-Correlation-Id` produced an audit row carrying it. Server suite: 1534 passed, baseline 4 failed. **Carry-over:** the audit API field is checked live with HA-003's image; the audit UI doesn't show the column yet (later polish). |
| HA-003 | done | SRB-ha-003-audit-gaps | (this commit) | New `audit_service.audit_request` helper: it never fails the action being audited, and records the real client IP via `core.client_ip.get_client_ip` (trusted-proxy XFF only). Audited: `ptz.move`/`ptz.stop`, `alerts.ack` (only when something was silenced), `recording.protect`/`unprotect`, `recording.export`, `device_firewall.enforcement/approve/block/delete`, `skill.claim`/`release`. Live on core:ha-dev: a test alarm, ack and export ticket under one correlation id gave 2 audit rows carrying it, and the audit API returns and filters `correlation_id` (closes the HA-002 carry-over). Server suite 1545 passed, baseline 4 failed. |
| HA-004 | done | SRB-ha-004-permissions | (this commit) | New `services/permission_catalog.py` is the single list for both seed paths; the upgrade logic moved out of `main.py` into testable `seed_new_permissions(db)` (apps.install/apps.view rules unchanged). `backfill_permission(source, target)` generalised from `apps_view_backfill` (thin wrapper kept). Defaults: `ptz.control` to operator and viewer; nothing else beyond admin. Live upgrade on this install: all 6 seeded once; `ptz.control` granted to the 3 roles holding `live.view` (admin, operator, viewer); no other grants. Server suite 1551 passed, baseline 4 failed. **Rule for HA-107 (review correction):** PTZ today requires camera **ownership** or superuser (`CameraService.get_camera_by_id`), and the IP-keyed ONVIF tools require the `manage` tier. It is NOT "anyone who can watch". `ptz.control` must be enforced **in addition to** those checks, never instead of them, or every `live.view` holder would gain PTZ on shared cameras. The permission and its grants are created in one transaction. |
| HA-005 | done | SRB-ha-005-e2e-suite | (this commit) | The 8 `test/e2e-suite` commits are squashed into this one (backup tags `backup/test-e2e-suite-pre-ha005`, `backup/SRB-ha-005-e2e-suite-pre-squash`); the one `.gitignore` conflict is resolved. **Correction:** the suite runs ALONGSIDE the dev stack (containers `opennvr_e2e_*`, +20000 ports, own subnet); no downtime was needed. Fix added: `run.py` test-binds every published host port before `compose up` and moves an unbindable one (Docker's backend held 20080) to the next free port. Result against core:ha-dev: **47 passed, 3 skipped** (the first-time-setup tests need `--fresh`), 0 failed. Run: `$env:CORE_TAG='ha-dev'; server\.venv\Scripts\python.exe tests/e2e/run.py` (the detect-pipeline `ha-dev` tag is a local alias of `:main`). |
| HA-006 | done | SRB-ha-006-ws-device-firewall | (this commit) | Firewall facts (client IP, device token, internal-key) are bound to the ws ticket at mint and re-checked at the handshake (`_ws_firewall_allows`), mirroring the HTTP middleware order. While enforcing, the handshake must come from the minting IP (a leaked ticket can't borrow an approval). Kept in a parallel map, so the ticket store's shape is unchanged. Live (enforcement **left off**, deliberately, so the owner's browser isn't locked out): ticket → subscribed → tier0 tracks flowing; replay refused. Refusal paths are covered by unit tests. Server suite 1563 passed, baseline 4 failed. |
| HA-007 | done | SRB-ha-007-playback-spike | (this commit) | **Answer: yes, the in-progress segment is playable.** MediaMTX 1.15.4 `/playback/get` returned 200 video/mp4 for windows starting 20, 12 and 6 s ago on cam-1 and cam-3 (the current segment is listed while still growing). **HA-113 decision:** a clip's `media_ready` fires at event end + 2 s; no need to wait for segment close. Clips may start at the preceding keyframe, which is fine. Probe kept as `scripts/ha-dev/probe_playback.py` (read-only, runs inside core). |
| HA-008 | done | SRB-ha-008-client-ip-spoofing | (this commit) | **Pre-existing critical bug found by the M0 review.** nginx appends to `X-Forwarded-For`, and `get_client_ip` took the LEFT-most (client-written) entry, so `X-Forwarded-For: 127.0.0.1` resolved to loopback. That bypassed the device firewall, defeated the HA-006 ws IP binding, and let clients pick the IP stored by HA-003 audit rows. Now the header is walked from the right, skipping trusted hops (right-most hop if all are trusted; `X-Real-IP` fallback). The new tests fail 7/14 on the old code. Live: the same forged request was recorded as `127.0.0.1` before, `172.28.0.1` (the hop nginx saw) after. Server suite 1583 passed, baseline 4 failed. |
| M0 review | done | (folded into HA-002/003/004/006) | — | Independent review, 10 findings: #1 → HA-008; #2 PTZ premise corrected (ownership gates PTZ; `ptz.control` only ever narrows); #3 ONVIF-tools PTZ audited; #4 permission + grants in one transaction; #5 self-heal creates `index=True` indexes; #6 service ws ticket only from inside the stack; #7 end-to-end ws test; #8 audits write through their own session; #9–10 log correlation id, hint-not-attribution docs. |
| M0 exit | done | pushed as `SRB-ha-integration` | 41eaa2b | Final e2e 47 passed / 3 skipped. The owner chose **one PR for the whole implementation, one commit per issue**, so all further issues are committed directly on `SRB-ha-integration` and pushed at each milestone end. |
| HA-104 | done | SRB-ha-integration | (this commit) | `services/site_settings.py` (shared key/JSON helper: site id created once and race-safe, site name, `recording_pause_enabled` true only if exactly `true`); `core/contract.py` (`CONTRACT_VERSION` 0.1.0 until HA-115 freezes 1.0.0, `FEATURES`); `GET /system/info`; opt-in `UPDATE_CHECK` (off by default, 6 h cache, failure → null). Live: site_id stable across a core restart. Server suite 1592 passed, baseline 4 failed. |
| HA-105 | done | SRB-ha-integration | (this commit) | `GET /cameras/{id}/stats` (`services/camera_stats.py`) joins MediaMTX path info (ready; bitrate by differencing `bytesReceived` between reads: the first read is null, and a counter reset or a gap over 300 s is null), tier0 metrics (fps, target fps, mean `inference_ms`, `skipped_total`, tracks, frame age; the new `_sum_by_camera` sums across the model/reason labels that `_by_camera` silently dropped) and the DB (recording state via `_derive_recording_state`, `days_retained`). Absent sources are null, never errors. `/system/resources` gains `gpu: null`. Live: cam1 bitrate 1050 kbit/s on the 2nd read, inference 731 ms (CPU box). Note: `/system/resources` is empty for ~15 s after a core restart, until the first monitor sample (pre-existing). Server suite 1597 passed, baseline 4 failed. |
