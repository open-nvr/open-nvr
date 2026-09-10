# Writing a rule

Almost every rule reduces to one question: *has this object been
somewhere, for long enough?* Geometry answers the **where**,
`keyed_state` answers the **how long**, and an `Alert` is what you do
about it. The facade's `zone=` and `dwell=` wrap exactly these.

## Where — zones and tripwires

Coordinates are **normalized** (0–1 of the frame) throughout, matching
the platform's `NormalizedBBox`, so a rule written against one camera
resolution works on all of them. The catalog's zone editor emits
normalized vertices for the same reason.

```python
--8<-- "cookbook/08_state_and_geometry.py:26:40"
```

A tripwire adds direction, which is what separates "12 people entered"
from "12 people milled about the door".

## How long — keyed TTL state

```python
--8<-- "cookbook/08_state_and_geometry.py:47:75"
```

Two things to get right:

- **TTL is event time.** Pass `at=` from the event's own timestamp, not
  the wall clock, or a replayed batch will skew every timer.
- **Latch, then let the TTL re-arm it.** `record.alerted` fires once per
  presence episode; when the key is not touched for the TTL it is
  garbage-collected, which is what makes the next appearance a new
  episode.

## What to fire

Keep the title to one line an operator can act on, put the numbers in
`evidence`, and thread the `correlation_id` so the alert joins the
inference, the audit line and the evidence frame in one chain.

```python
--8<-- "cookbook/18_alerts_and_channels.py:20:39"
```

Full example: [`08_state_and_geometry.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/08_state_and_geometry.py),
[`18_alerts_and_channels.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/18_alerts_and_channels.py).
