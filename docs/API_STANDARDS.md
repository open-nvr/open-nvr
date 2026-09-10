# API standards

Every API surface in OpenNVR is described by an open specification, and
every one of those descriptions is **generated from the implementation**
rather than maintained beside it. A hand-written spec drifts within a
release; a generated one cannot.

| Surface | Who serves it | Standard | Where |
|---|---|---|---|
| Operator / platform API | `server` (core) | **OpenAPI 3.1** | `/openapi.json`, Swagger at `/docs`, ReDoc at `/redoc` |
| KAI-C gateway | `kai-c` | **OpenAPI 3.1** | `/openapi.json` |
| AI adapters | each adapter (`opennvr-adapter-sdk`) | **OpenAPI 3.1** | `/openapi.json` on the adapter |
| App contract | each app (`opennvr-app-sdk`) | **OpenAPI 3.1** | `/openapi.json` on the app's contract port |
| Event bus | the app / the platform | **AsyncAPI 3.0** | `/asyncapi.json` on the app's contract port |
| Event envelopes | — | JSON Schema, normative prose | [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md) (CI-enforced) |

The first three are FastAPI, so OpenAPI comes for free and is always
current. The last three are the ones this project had to build, because
an app's contract server is a stdlib HTTP server and the event bus is
not HTTP at all.

## The app contract, in OpenAPI

An app's manifest already declares its params, actions, alert types and
state views — that is what lets the App Catalog render a config form and
a dashboard with no app-specific code. The same declaration generates
the app's OpenAPI document, so the two can never disagree:

- a declared `Action` **is** a `POST /actions/{name}` path, with its
  `params` as a typed request-body schema and its `confirm` flag in the
  description;
- a declared `Param` **is** a JSON Schema — Python types map to their
  JSON equivalents, and the catalog's UI types (`geometry.polygon`)
  map to the shape they carry on the wire plus an `x-opennvr-ui` hint,
  so a generic OpenAPI consumer still sees something usable;
- `has_ui` adds `GET /ui`; `entitlement: license_key` adds
  `POST /entitlement/verify`; an app that declares neither has neither
  path in its spec.

```bash
opennvr-app spec                     # OpenAPI 3.1, JSON, to stdout
opennvr-app spec --format asyncapi   # the bus surface
opennvr-app spec --yaml -o api.yaml  # for the docs site or a client generator
curl http://my-app:9210/openapi.json # …or ask a running app
```

Point Swagger UI or an SDK generator at any running OpenNVR app and it
works. Every example app in this repository generates a document that
passes `openapi-spec-validator`, and the SDK's test suite asserts it.

## The event bus, in AsyncAPI

REST specs stop at the door of an event-driven system, so the bus gets
**AsyncAPI 3.0** — the same idea, for channels rather than paths. An
app's document names three things, all derived from its manifest:

- what it **receives**: `subscribes`, typed by its subject tree, since a
  `Detector` on `opennvr.inference.>`, a `DomainEventSubscriber` on
  `opennvr.events.plate.recognized.v1.>` and an `AlertSubscriber` on
  `opennvr.alerts.>` carry three different envelopes;
- what it **sends**: `opennvr.alerts.app.<id>.{camera_id}`, with the
  declared alert types and their severities;
- what it **requests**: each `requires_scopes` entry becomes a channel,
  because a scope is how an app asks for PII-bearing domain events, and
  that belongs in the spec rather than in prose.

[EVENT_CONTRACTS.md](EVENT_CONTRACTS.md) stays normative for the
envelopes themselves — it is enforced by
`server/tests/test_event_contracts.py`, so no first-party subject exists
without a contract. AsyncAPI is how that contract reaches tooling.

## Versioning

- The **app contract** carries its own `api_version` (currently 1.3),
  surfaced as `x-opennvr-contract-version` in every generated document.
  Core degrades gracefully against older apps; see
  [APP_SURFACES.md](APP_SURFACES.md).
- **Domain events** carry their major version in the subject
  (`…​.v1.…`), so v2 can run beside v1 during a migration and every
  subscriber picks explicitly.
- **The platform API** is `/api/v1/*`, and the compatibility promise in
  [DEVELOPER_PROGRAM.md](DEVELOPER_PROGRAM.md) covers what apps depend
  on.

## Deliberate non-goals

- **No gRPC / protobuf.** The bus is NATS with JSON envelopes because
  operators debug it with `nats sub` and a terminal, and because
  contracts a human can read get followed. Throughput is not the
  constraint on this system; comprehension is.
- **No API gateway of our own.** Core's `/api/v1` is the door; apps are
  never exposed to the internet directly, and the one HTTP surface an
  app serves is on the internal network behind core's proxy
  ([APP_NETWORK.md](APP_NETWORK.md)).
