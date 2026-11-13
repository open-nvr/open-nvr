# Specs your app publishes

Every OpenNVR app self-describes in the two standards its consumers
already have tooling for. Both documents are generated from the app's
own `AppManifest`, so they cannot drift from the app: a declared
`Action` **is** a path, a declared `Param` **is** a schema.

```bash
opennvr-app spec                     # OpenAPI 3.1, JSON, stdout
opennvr-app spec --format asyncapi   # the NATS surface
opennvr-app spec --yaml -o api.yaml  # for a docs site or a client generator
curl http://my-app:9210/openapi.json # …or ask a running app
```

## OpenAPI 3.1 — the HTTP surface

Served at `GET /openapi.json` on the app's contract port.

| Path | Appears when | Purpose |
|---|---|---|
| `GET /health` | always | Liveness plus pipeline vitals; drives the catalog's status dot. |
| `GET /manifest` | always | The declarative identity core stores at registration. |
| `GET /state` | always | Live standing state, documented by the manifest's `state_schema`. |
| `GET /ui` | `has_ui` + `ui_mode="internal"` | The embedded dashboard, rendered sandboxed. |
| `POST /actions/{name}` | one per declared `Action` | Operator verbs. User-JWT only, via core's proxy. |
| `POST /entitlement/verify` | `entitlement="license_key"` | Your licence check. |

Param typing: Python types map to their JSON equivalents; the catalog's
UI types map to the shape they carry on the wire plus an `x-opennvr-ui`
hint, so a generic consumer still sees something usable.
`geometry.polygon` becomes an array of normalized `[x, y]` vertices;
`per_camera=True` becomes an object keyed by camera id.

## AsyncAPI 3.0 — the bus surface

Served at `GET /asyncapi.json`. Three groups of channels, all derived
from the manifest:

- what the app **receives** — `subscribes`, typed by its subject tree,
  because a `Detector` on `opennvr.inference.>`, a
  `DomainEventSubscriber` on `opennvr.events.plate.recognized.v1.>` and
  an `AlertSubscriber` on `opennvr.alerts.>` carry three different
  envelopes;
- what it **sends** — `opennvr.alerts.app.<id>.{camera_id}`, with the
  declared alert types and severities;
- what it **requests** — one channel per `requires_scopes` entry, since a
  scope is how an app asks for PII-bearing domain events, and that
  belongs in the spec rather than in prose.

## The rest of the platform

Core, KAI-C and every AI adapter are FastAPI and publish OpenAPI 3.1 at
`/openapi.json`, with Swagger UI at `/docs`. The full map, including
versioning rules and the deliberate non-goals, is
[API_STANDARDS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/API_STANDARDS.md).

::: opennvr_app_sdk.openapi
    options:
      members: false
