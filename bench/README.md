# bench — measuring whether the tier-0 architecture earns its keep

Two numbers settle the "is tier-0 + event memory worth it, or are we losing
data" question instead of guessing.

## 1. `tier0_recall.py` — what the cheap detector SEES

Recall bounds everything downstream: a person tier-0 misses is an event never
remembered and an alert that never fires. Point it at a detector's `/infer`
endpoint and a labelled image manifest; it reports presence recall/precision
per label, **broken down by condition** (static / edge / close / low-light) so
the hard cases aren't hidden by the easy-case average.

```bash
python bench/tier0_recall.py --url http://localhost:9108/infer --manifest labels.json
```

Manifest: `{"images":[{"path":"...","truth":{"person":1},"condition":"static"}, ...]}`
(`truth` = ground-truth presence counts; 0 = absent).

**Read it as:** low recall on `static`/`close`/`edge` is the exact blind spot
behind "the agent didn't see the person sitting there." Track it over time; it
is what the automatic index costs you.

## 2. `event_memory_dividend.py` — what the store is WORTH

A look-only agent sees nothing between questions. Every stored event is a
moment it would have missed. This queries the events API over a window and
reports the dividend by label and hour, calling out the **unattended hours**
(overnight/quiet) — the clearest "would have been lost without the store" set.

```bash
python bench/event_memory_dividend.py --url http://localhost:8000 --token "$JWT" --days 7
```

**Read it as:** a large unattended count means the memory is doing real work a
query-only agent structurally cannot.

## The verdict these produce

- Good recall + meaningful memory dividend → the architecture earns its keep;
  keep it, and hold consumers to the liveness rule (cheap layer never
  substitutes for a live look on a "now" question).
- Poor recall or ~nothing consuming the stream → that's the QA "no observable
  difference"; harden recall or simplify — but now you *know*, from evidence.

The scoring/summary logic is stdlib-only and unit-tested (`test_bench.py`); the
network parts need a running detector / backend.
