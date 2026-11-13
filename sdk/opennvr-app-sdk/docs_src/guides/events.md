# The event bus

## Domain events

`opennvr.events.<domain>.<event>.v<N>.<camera_id>` — versioned in the
subject, so a v2 can run beside v1 during a migration and every
subscriber picks explicitly. The envelopes are normative and
CI-enforced:
[EVENT_CONTRACTS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/EVENT_CONTRACTS.md).

From a facade rule, publishing is one call — the envelope, the producer
(`app:<id>`), the camera and the correlation id are already known:

```python
app.publishes("occupancy.changed.v1")        # so it appears in the spec

@app.on_detection("person")
def count(event):
    event.publish("occupancy.changed.v1",
                  {"count": event.count("person"), "level": "normal"})
```

Underneath, and from a base class, it is `DomainEventPublisher`. Publish
the **typed** payload where you can, not a dict: the class carries the
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

## Asking the store, and "not yet"

`EventsClient.search(label=…, attrs=["blue"], start=…, end=…)` returns the
visits the store can vouch for, newest first — `[]` for a genuinely empty
window, `None` when the question could not be asked (say those apart to
the user: "nothing came" and "I couldn't check" are different answers).

Search matches what a skill **said** about a visit — `attrs=["blue"]` —
not only what the detector classified. Those claims are written after the
fact, by enrichers that run when the box can afford them; so some of the
visits your window matches may not carry the claim *yet*. Core answers
from what exists and queues the rest for description now, and the list it
returns says so:

```python
visits = await events.search(label="car", attrs=["blue"], start=since)
if visits is None:
    return "I couldn't check the history."
if visits.pending and visits.pending["missing"]:
    p = visits.pending
    note = (f" {p['missing']} visits in that window haven't been described"
            f" yet — about {round(p['eta_s'])} s; ask me again then.")
```

`visits` is still a list (an app built against the list contract keeps
working); `.pending` is `None` when nothing was needed, and the same block
is mirrored on `events.last_pending` for code that holds the client rather
than the rows. The `enrichment_request_done` system alert on the bus
announces when the request finished, if you would rather re-ask than wait.
Operators bound the work with `EVENTS_ENRICHMENT_REQUEST_CAP`; see
[`docs/ENRICHMENT.md`](https://github.com/open-nvr/open-nvr/blob/main/docs/ENRICHMENT.md).
