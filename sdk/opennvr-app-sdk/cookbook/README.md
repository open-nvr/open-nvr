# The SDK cookbook

One runnable file per class, each showing how it is constructed or
subclassed and which APIs it uses. Every file is imported and exercised
by `tests/test_cookbook.py`, so nothing here can drift from the SDK — if
a signature changes, these break in CI before they mislead anyone.

Read them in order for a tour, or jump to the one that names your
problem.

## Write an app

| File | Class | When you reach for it |
|---|---|---|
| [01_app_facade.py](01_app_facade.py) | `App`, `DetectionEvent` | **Start here.** The decorator front door — most apps need nothing else. |
| [02_detector.py](02_detector.py) | `Detector` | The class the facade compiles to. Consume inference another app is already driving. |
| [03_frame_app.py](03_frame_app.py) | `FrameApp`, `KaiCClient` | Drive your own inference: poll frames, call a model, pay the GPU. |
| [04_alert_subscriber.py](04_alert_subscriber.py) | `AlertSubscriber` | React to alerts other apps fire — relays, SIEM bridges, notifiers. |
| [05_domain_event_subscriber.py](05_domain_event_subscriber.py) | `DomainEventSubscriber` | React to contracted domain events (`plate.recognized.v1`, …). |

## Describe it to the platform

| File | Covers |
|---|---|
| [09_manifest_and_surfaces.py](09_manifest_and_surfaces.py) | `AppManifest`, `Param`, `AlertType`, `StateView`, `Action` — every field the App Catalog reads, and the `state_snapshot` / `on_action` / `ui_html` that answer them. |
| [10_selling_an_app.py](10_selling_an_app.py) | `entitlement`, `verify_license`, `Entitlement`, `UserContext` — charging for an app, with OpenNVR taking no part in the transaction. |
| [17_contract_server.py](17_contract_server.py) | `ContractServer` standalone, and the generated `/openapi.json` + `/asyncapi.json`. |

## Use the platform

| File | Covers |
|---|---|
| [06_platform_client.py](06_platform_client.py) | `OpenNVR` — cameras, snapshots, recordings, timeline, evidence, alerts inbox, durable state, inference. |
| [07_async_client.py](07_async_client.py) | `AsyncOpenNVR` — the same surface, fanned out concurrently. |
| [13_events_client.py](13_events_client.py) | `EventsClient` — the platform's memory: what was seen, and the frame that proves it. |
| [14_infer_stream.py](14_infer_stream.py) | `InferStream` — many frames down one warm WebSocket session. |
| [15_cameras_and_credentials.py](15_cameras_and_credentials.py) | `discover_cameras`, `cameras_for_skill`, `AppCredentials` — which cameras, and who am I. |
| [11_tier0.py](11_tier0.py) | Tier-0: counts and best frames from the always-on detector, at zero cost. |

## The pieces every rule uses

| File | Covers |
|---|---|
| [08_state_and_geometry.py](08_state_and_geometry.py) | `keyed_state`, `Zone`, `Tripwire` — *how long* and *where*, which is most of every rule. |
| [18_alerts_and_channels.py](18_alerts_and_channels.py) | `Alert`, `AlertDispatcher`, the channels, and writing your own. |
| [12_domain_event_publisher.py](12_domain_event_publisher.py) | `DomainEventPublisher` — becoming another app's input. |
| [19_egress_and_networking.py](19_egress_and_networking.py) | `proxy_address`, `connect_via_proxy` — reaching the outside world under the egress allow-list. |
| [16_testing.py](16_testing.py) | `opennvr_app_sdk.testing` — the tests worth writing, with no broker, core or Docker. |

## Try one

```bash
pip install opennvr-app-sdk
opennvr-app new my-app          # a runnable app + tests, facade-first
cd my-app && opennvr-app dev    # watch the rule fire on a simulated camera
```
