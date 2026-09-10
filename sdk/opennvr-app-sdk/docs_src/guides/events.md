# The event bus

## Domain events

`opennvr.events.<domain>.<event>.v<N>.<camera_id>` — versioned in the
subject, so a v2 can run beside v1 during a migration and every
subscriber picks explicitly. The envelopes are normative and
CI-enforced:
[EVENT_CONTRACTS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/EVENT_CONTRACTS.md).

Publish the **typed** payload, not a dict: the class carries the
contract's required fields, so a malformed event fails at publish time
rather than in someone else's app.

```python
--8<-- "cookbook/12_domain_event_publisher.py:27:40"
```

Consuming is the mirror image — name the schemas, not the subjects:

```python
class Gate(DomainEventSubscriber):
    subscriptions = ["plate.recognized.v1"]

    def on_event(self, event):
        plate = event.typed          # -> PlateRecognized | None
```

## Scopes

Some domain events carry PII. A plate read is one. Consuming those is a
**declared capability**: name it in `requires_scopes`, and it is granted
at install, audited, visible in the App Catalog, and published in your
app's [AsyncAPI document](../specs.md). Without the scope the bus does
not deliver the event.

```python
requires_scopes=["events:plate.recognized"]
```

## Alerts

An alert is for a human; a domain event is for another app. The alert
subject mirrors the alert's own source block, so subscribers filter
without parsing the body:

```
opennvr.alerts.>                       every alert
opennvr.alerts.app.>                   every app-emitted alert
opennvr.alerts.*.*.cam-front-door      one camera
opennvr.alerts.app.loitering.>         one app
```

Prefer `opennvr.alerts.app.>` over `opennvr.alerts.app.*.*`: `>` matches
one or more tokens, so it survives a future contract revision that adds
a fifth segment.

## Tier-0

The always-on detector. `snapshot_from_event` reduces a Tier-0 payload
to what apps ask of it — counts per label, a speakable phrase, and which
tracks have a fetchable best frame. Or set `consume_tier0 = True` on a
`Detector` and Tier-0 tracks arrive as ordinary detections, so one rule
serves both sources.

It is off by default on purpose: an app also subscribed to a heavy
adapter would otherwise see the same object twice and alert twice.

Full examples:
[`05_domain_event_subscriber.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/05_domain_event_subscriber.py),
[`12_domain_event_publisher.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/12_domain_event_publisher.py),
[`11_tier0.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/11_tier0.py).
