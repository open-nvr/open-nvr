# `opennvr_app_sdk` — public API index

The SDK is the *only* import a vision app needs. This page is the map
of what it exports (`opennvr_app_sdk.__all__`), grouped by what you
are trying to do; the docstrings in the source are the reference for
each name and `make sdk-docs` renders them to HTML. Concepts and
walkthroughs live in [FIRST_DETECTOR.md](FIRST_DETECTOR.md),
[APP_PLATFORM.md](APP_PLATFORM.md), [APP_SURFACES.md](APP_SURFACES.md)
and [APP_CREDENTIALS.md](APP_CREDENTIALS.md).

Version: `opennvr_app_sdk.__version__` (`0.4.0`). Licence: Apache-2.0.

```bash
pip install opennvr-app-sdk            # or: uv add opennvr-app-sdk
pip install "opennvr-app-sdk[nats]"    # + NATS client for the subscribe loops
```

## Pick an archetype — the class you subclass

| Archetype | Subclass | Implement | Run with | Source of frames/events |
|---|---|---|---|---|
| **Detector** | `Detector` | `on_detections(camera_id, detections, event) -> Iterable[Alert]` | `app(MyDetector).run()` | Tier-0 / adapter inference events on NATS — no model of your own |
| **FrameApp** | `FrameApp` | `on_frame(camera_id, jpeg) -> Iterable[Alert]` | `app(MyApp).run()` | Frames you pull; you call inference via `KaiCClient` or `nvr.ai` |
| **DomainEventSubscriber** | `DomainEventSubscriber` | `subscriptions = [...]`, `on_event(DomainEvent)` | `domain_event_app(MyApp).run()` | Domain events (`plate.recognized.v1`, …) other apps publish |
| **AlertSubscriber** | `AlertSubscriber` | `on_alert(alert, subject)` | `alert_app(MySub).run()` | `opennvr.alerts.*` — relays, SIEM bridges |

All four mix in `ContractMixin`, which gives every app the registry
contract for free: `/health`, `/state`, `/manifest`, `/actions/{name}`,
`/entitlement/verify` served by `ContractServer`; registration with
core (`register_with_opennvr`); live config (`on_config_update`,
`start_config_poll`); credentials (`credentials`); the calling user
(`current_user`); and licensing (`verify_license`,
`on_entitlement_update`, `entitlement`). Optional hooks to override:
`setup()`, `state_snapshot()`, `health_snapshot()`, `on_action(name,
params)`.

Runners: `AppRunner`, `AlertSubscriberRunner` (what `app()` /
`alert_app()` / `domain_event_app()` return; `.run(argv)` parses the
CLI, loads config, wires signals).

## Describe the app — the manifest

| Name | Purpose |
|---|---|
| `AppManifest` | Identity + schema: `id`, `name`, `version`, `category`, `summary`, `requires_tasks`, `requires_adapters`, `requires_scopes`, `provides`, `subscribes`, `params`, `emits`, `state_schema`, `actions`, `has_ui` / `ui_mode` / `ui_url`, `description`, `author`, `website`, `license`, `use_cases`, `contact`, `pricing`, `price_note`, `entitlement` |
| `Param` | One config field the catalog renders (`key`, `type`, `default`, `per_camera`, …) |
| `AlertType` | One kind of alert the app `emits` |
| `StateView` | One live view of `/state` the catalog renders (`state_schema`) |
| `Action` | One operator verb the catalog offers (`POST /actions/{name}`) |
| `PRICING_MODELS` | `free`, `paid`, `subscription`, `contact` |
| `ENTITLEMENT_MODES` | `none`, `license_key` |
| `Entitlement` | What `verify_license` returns: `valid`, `plan`, `expires_at`, `message`, `limits` |

## Talk to the platform — `OpenNVR`

```python
from opennvr_app_sdk import OpenNVR
nvr = OpenNVR()                      # OPENNVR_URL + the app's own key
```

| Name | Purpose |
|---|---|
| `OpenNVR(url=None, *, token=None, kaic_url=None, timeout=…)` | The client; everything below hangs off it |
| `.cameras() -> list[Camera]`, `.camera(x)` | The app's roster (only cameras assigned to it) |
| `.snapshot(camera) -> bytes \| None` | Current JPEG |
| `.recordings(camera)` → `RecordingsAPI` | `.list(start, end)`, `.url(start, duration)`, `.frame_at(at)` |
| `.timeline` → `TimelineAPI` | `.search(camera=, label=, …)`, `.evidence(event_id)`, `.plate_stats()`, `.plate_summary()`, `.plate_sessions()` |
| `.alerts` → `AlertsAPI` | `.inbox(unacked=, limit=, after_id=)` — what this app raised, and whether anyone acknowledged it |
| `.state` → `StateAPI` | `.get`, `.set`, `.delete`, `.items(prefix)` — durable per-app key/value in core |
| `.ai` → `AIAPI` | `.capabilities()`, `.infer(adapter, jpeg, task=…)`, `.stream(adapter, camera_id=…)` |
| `Camera` | `id`, `handle` (`camN`), `name`, `role`, `frame_url`, `assignments`; `.has_skill(s)` |
| `Recording` | `start`, `duration` |
| `PlatformError` | Raised by writes; reads degrade to `None` / `[]` and log |
| `InferStream` | The WebSocket inference session behind `nvr.ai.stream()`; `open()`, `infer(jpeg)`, `close()`, context-manager |
| `AsyncOpenNVR` (`opennvr_app_sdk.aio`) | The same client `await`-ed, for FastAPI/agent loops: `await nvr.cameras()`, `await nvr.state.set(...)`, `async with`; `http_client=` shares a pool; no `ai.stream()` yet |

Lower-level helpers that predate the client and remain public:
`discover_cameras(url)`, `cameras_for_skill(...)`,
`filter_cameras_for_skill(...)`, `full_frame_polygon()`, `EventsClient`
/ `StoredEvent` (async event search), `KaiCClient` / `KaiCError`
(direct KAI-C HTTP), and the Tier-0 helpers `Tier0Snapshot`,
`snapshot_from_event`, `tier0_to_detections`, `is_tier0_subject`,
`describe_counts`, `BestFrameClient`, `make_best_frame_fetch`.

## Raise alerts

| Name | Purpose |
|---|---|
| `Alert` | `title`, `description`, `camera_id`, `severity` (`low`/`medium`/`high`/`critical`), `evidence`, `tags`, `correlation_id`; `.to_wire()` |
| `AlertSource`, `set_default_source(...)` | Who raised it (app id/name/version) — set once at startup |
| `AlertDispatcher`, `build_dispatcher(webhook_url=…, nats_alerts_url=…)` | Fan-out: `StdoutChannel` always, `WebhookChannel` and `NatsAlertChannel` opt-in |
| `alert_subject(alert)`, `DEFAULT_ALERT_SUBJECT_PREFIX` | The NATS subject an alert lands on (`opennvr.alerts.<severity>.<camera>`) |
| `AlertChannel` | Protocol for your own channel: `send(alert) -> bool` |

Alerts published on NATS reach the operator inbox, the bell and the
alarm actions without any further code.

## Publish and consume domain events

| Name | Purpose |
|---|---|
| `DomainEventPublisher` | `.publish(schema, camera_id, payload)` on `opennvr.events.<schema>.<camera>` |
| `domain_envelope(...)`, `domain_subject(schema, camera_id)` | The wire helpers |
| `DomainEvent` | Parsed envelope: `id`, `schema`, `camera_id`, `ts`, `payload`, `producer`, `correlation_id`, `subject` |
| `DomainEventSubscriber`, `domain_event_app`, `parse_domain_event` | The consuming archetype (above) |

Schemas and their payloads: [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md).

## Identity

| Name | Purpose |
|---|---|
| `AppCredentials`, `auth_headers()` | The app's own key (`OPENNVR_APP_KEY`, or `.opennvr/app.key`), falling back to the site key only to bootstrap |
| `UserContext`, `current_user()` | Inside `/ui` and `/actions/*`: the user core forwarded — `id`, `username`, `is_superuser`, `.can_see(cam)`, `.can_manage(cam)`, `.visible(ids)` |

## Rules — geometry and state

| Name | Purpose |
|---|---|
| `Zone` (`.contains`, `.from_config`), `Tripwire` (`.side`, `.crossing`), `Point`, `bbox_center(bbox, w, h)` | Polygons and lines in frame coordinates; `from_config` reads what the catalog's zone editor stored |
| `KeyedState`, `StateRecord`, `keyed_state(ttl=…)` | Per-key TTL memory for dwell/cooldown/tracking rules; `Detector.keyed_state(ttl)` is the shortcut |

## Frames (FrameApp)

| Name | Purpose |
|---|---|
| `FrameSource` | Protocol: `get_frame(camera_id) -> bytes \| None` |
| `build_frame_source(camera_id=, url=)` | `rtsp://`, `http(s)://` snapshot, or `file://` from one URL |
| `HttpSnapshotSource`, `FileFrameSource`, `CameraFrameSource`, `DictFrameSource`, `dict_frame_source(...)`, `FrameSourceError` | The concrete sources and the mapping wrapper |

Prefer `nvr.snapshot(camera)` (server-side capture, no RTSP in your
container) unless you need full frame rate.

## Config

`load_yaml(path)`, `require(cfg, key)` — the two helpers the runners
use; pass your own `load_config` to `app()` for anything richer.

## Releasing

The package is published to PyPI as
[`opennvr-app-sdk`](https://pypi.org/project/opennvr-app-sdk/) by
`.github/workflows/publish-sdk.yml` through PyPI trusted publishing —
no token lives in the repository. A release is:

1. Bump `opennvr_app_sdk/_version.py` **and** `pyproject.toml` to the
   same version, add the CHANGELOG entry, merge.
2. `git tag sdk-v<version> && git push origin sdk-v<version>`.

The workflow refuses a tag that does not match both files, runs the
suite on 3.11–3.13, builds the sdist and wheel, `twine check`s them,
installs the wheel in a clean venv and imports it, then publishes
(the `pypi` environment may require a maintainer's approval). Every
PR touching `sdk/**` runs the same pipeline minus the publish.

## Stability

Every name above is covered by the
[compatibility promise](DEVELOPER_PROGRAM.md#the-compatibility-promise):
removals and signature changes are announced a minor release ahead and
the deprecated name keeps working, with a warning, until then. Names
starting with `_` and modules not re-exported from the package root
are internal.
