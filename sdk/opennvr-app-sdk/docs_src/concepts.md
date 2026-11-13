# Concepts

## The four archetypes

An app is defined by where its input comes from. Pick by that, not by
what the app does.

| Archetype | Input | You implement | Cost |
|---|---|---|---|
| **`Detector`** | `opennvr.inference.>` — results another app is already driving | `on_detections(camera_id, detections, event)` | none: the GPU is already paid |
| **`FrameApp`** | frames you poll, inference you call | `on_frame(camera_id, jpeg)` | one inference per frame, yours |
| **`DomainEventSubscriber`** | contracted domain events (`plate.recognized.v1`) | `on_event(DomainEvent)` | none |
| **`AlertSubscriber`** | `opennvr.alerts.>` | `on_alert(alert, subject)` | none |

`App` — the facade — compiles to a `Detector`, which is the right
starting point for most apps: the platform is usually already running
the model you need, and riding its stream is free.

Reach for `FrameApp` only when nothing on the deployment produces what
you need and you must run a model yourself.

## Alerts and events are different things

- An **alert** is for a human. It lands in the operator inbox, it has a
  severity, and someone is expected to look at it. Envelope: §11.5.
- A **domain event** is for another app. It is versioned, contracted,
  and nobody is expected to read it. Envelope:
  [EVENT_CONTRACTS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/EVENT_CONTRACTS.md).

An app that recognises plates publishes a domain event; a gate
controller, a visitor log and a dashboard all react to it without any of
them knowing the plate reader exists. Only the gate controller fires an
alert, and only when the plate is unknown.

## Declare, don't build

The SDK's central bet is that an app which **declares** its surfaces
needs no frontend. From `AppManifest` the App Catalog builds:

- the **config form** from `params` — including a zone editor on a
  camera still for `geometry.polygon`;
- the **dashboard** from `state_schema` — metrics, gauges, tables, logs,
  galleries, over whatever `state_snapshot()` returns;
- the **buttons** from `actions`, proxied to `POST /actions/{name}`;
- the **store listing** from `description`, `use_cases`, `pricing`;
- the **licence gate** from `entitlement`.

The same manifest generates your app's
[OpenAPI and AsyncAPI documents](specs.md), so the spec cannot drift
from the app either.

## Identity and scope

An app never holds the deployment's site key. At registration core
issues it a credential of its own, scoped to the cameras an operator
assigned it. `nvr.cameras()` returns that roster, not the site's — an
app cannot see everything by accident, and an operator can see exactly
what each app can reach.

Egress works the same way: apps sit on an internal network with no
direct route out, and outbound traffic goes through the deployment's
proxy against a host allow-list the operator can audit. That is what
makes a third-party app installable in a hospital or a ministry.

## The event timeline, not the wall clock

Rules that measure duration should use the event's own timestamp
(`Detector.parse_event_ts`, or `event.ts` on the facade), never
`time.time()`. A replayed, delayed or reordered event must not skew a
dwell timer. `keyed_state` takes `at=` for exactly this reason.
