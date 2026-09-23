# RFC-0003 — Attaching what an app learns to a visit

**Status:** draft, needs a decision
**Blocks:** smart-doorbell's parallel visit log, `face_id` having any
producer at all, person journeys
**Author:** OpenNVR

## The problem, precisely

An app that polls frames has no way to say which visit its observation
belongs to.

`Detector` apps are fine: they receive an inference event that already
carries the identifiers, so a claim they make lands on a known
`event_id`. `FrameApp` apps are not. `on_frame(camera_id, frame_bytes)`
is the entire context — a camera, some bytes, and the moment it was
called. No `event_id`, no `track_id`. Whatever the app works out from
that frame, it cannot attach to anything.

This is not a small hole. It is the reason for three separate gaps:

**smart-doorbell keeps its own visit history.** It recognises a face
and has nowhere to put the name, so it maintains a parallel record in
the SDK's key/value store — a second list of who came to the door,
with its own cap, its own thumbnail policy and its own ageing rules.
Its source file argues the case well, and the argument is right as far
as it goes:

> a `FrameApp` polls snapshots. It has a frame and a face; it has no
> `event_id` and no `track_id`. Attaching a name to a platform visit
> would mean guessing which visit the frame belonged to by matching
> timestamps, and a guessed identity written into the shared store is
> indistinguishable, afterwards, from a measured one.

**`face_id` has no producer.** It is declared in the descriptor
vocabulary, priced in journey scoring, and written by nothing.
`test_descriptor_producers.py` records this as deliberate: core will
not name people, and the kind is left to an app the operator installed
on purpose. But no such app can write one, because no such app can
address a visit.

**Person journeys do not exist.** The journey code has `face_id`
branches. They are unreachable, for the same reason.

So one missing capability, three symptoms, and a growing incentive for
every app that learns something about a person to keep its own record —
which is the duplication this whole consolidation has been removing.

## What the objection actually is

The doorbell's argument is not "binding is impossible". It is that a
guessed subject becomes **indistinguishable from a measured one** once
it is written down.

That is a claim about the record, not about the binding. And the
descriptor store already has most of the machinery for exactly this
kind of honesty. A `VisitDescriptor` carries:

- `confidence` — what the skill thought of its own claim
- `source_task`, `source_adapter`, `model_fingerprint` — who said it
- `correlation_id` — "the difference between a descriptor being an
  assertion and being evidence", per its own docstring

Every one of those qualifies *the claim*. None of them qualifies **the
subject** — how this claim came to be attached to this visit rather
than the one before it. That is the missing column, and it is why the
doorbell's objection currently has no answer except abstention.

The proposal below is therefore not "let apps guess". It is: make the
binding a first-class, recorded, queryable property, so that a reader
three months later can tell a claim bound by identity from one bound by
a timestamp match — and filter accordingly.

## Options

### A. Core resolves, and says how

Add a route that turns (camera, instant) into a visit, and a `binding`
column on `visit_descriptors` recording how the subject was determined:

| binding | meaning |
|---|---|
| `direct` | the producer held the `event_id` (every `Detector` today) |
| `window` | core matched camera + instant **inside** a visit's own span |
| `nearest` | no visit covered the instant; the closest within a bounded tolerance was used |

`window` is not a guess in the way the doorbell means. The visit's span
is core's own record of when that object was present; an instant inside
it is a lookup, with a defined answer, made by the component that owns
the data. `nearest` *is* a guess, which is why it is a different value
and why a reader can refuse it.

Ambiguity is an answer, not an error: two visits overlapping the
instant on that camera returns both and binds neither. A doorbell frame
during a two-person arrival should produce no identity claim rather
than a coin toss.

**Cost:** a migration, a route, an SDK method, and every existing row
backfills to `direct`.

### B. Core stamps the frame

When core serves a frame to an app, it includes the open visit id(s) in
a response header. The app is *told* which visit it is looking at and
never matches anything.

Strictly better where it applies, and it composes with A rather than
competing: a frame that arrives stamped produces a `direct` binding.

**Cost:** only works for frames core serves. An app polling a camera's
RTSP directly — which `FrameApp` supports — gets nothing, so A is still
needed underneath. Also means the frame-serving path has to look up
open visits per request, on the hot path.

### C. Apps post observations; core binds

The app sends `{camera, at, kind, value, confidence, evidence}` to a
new route and never sees an `event_id` at all. Core binds and records
how.

Cleanest app-side surface, and it keeps the binding rules in one place
where they can change without an SDK release. It is really A with the
join moved server-side, and could follow A once the semantics have
settled in practice.

### D. Do nothing

Defensible, and worth stating plainly rather than dismissing. The
doorbell works. Its log is capped and lives in core's durable store, so
it is not a second database in the way footage-search's SQLite index
was — no second retention policy, no second disk.

What it costs: every future app that learns something about a person
solves this again, differently; `face_id` and person journeys stay
dead code that is priced, tested and unreachable; and an operator
asking "who came to the door last Tuesday" gets a different answer from
the app than from the platform, with no way to tell which is right.

## Recommendation

**A, with the `binding` column, and B later as an optimisation.**

A is what unblocks the three gaps. The `binding` column is what makes
it acceptable — without it the doorbell's objection stands and should
stand. B is a performance and precision improvement on the same model,
not an alternative to it, and can wait until something is actually
using A.

C is where this probably ends up, but proposing it first would mean
settling the binding semantics and the app-facing surface in one go. A
gets the semantics into the schema where they can be argued about with
real rows in front of us.

## What this does not decide

The decision this RFC exists for is **not** primarily technical. It is:

**may an app write an identity claim into the shared store at all?**

Everything above assumes yes and asks how to do it honestly. If the
answer is no — if a name attached to a person should stay inside the
app the operator installed on purpose, and never enter the store every
other app can read — then the right outcome is option D plus deleting
the `face_id` kind, the journey branches that price it, and the note in
`test_descriptor_producers.py` that has been holding the question open.
That is a smaller, cleaner tree than the one we have now, and it is a
legitimate product position for a surveillance platform to take.

What should not continue is the current state: the kind declared, the
scoring written, the branches tested, and nothing able to produce one —
a decision deferred so long it reads like an oversight.

A few things that stay open whichever way this goes:

- **Tolerance for `nearest`.** Seconds, presumably, and per-camera,
  since a doorbell and a car park behave differently. Needs real data.
- **Retraction.** A face match corrected later has to be able to
  withdraw the claim, the way `clear_plate` drops a plate claim rather
  than blanking a column.
- **Who may read `face_id`.** `descriptor_store` deliberately keeps it
  out of the projected words so it cannot be reached by free-text
  search. Whether it should be scoped further — a permission, not just
  an omission — is a separate question this RFC does not answer.
- **Backfill.** Existing rows become `direct`, which is true for all of
  them today. Worth asserting in the migration rather than assuming.

## Acceptance, if A is chosen

- A `FrameApp` can attach a claim to a visit without keeping its own
  record of visits.
- Every `visit_descriptor` row says how its subject was determined, and
  a reader can exclude `nearest` in one filter.
- An ambiguous instant binds nothing, and there is a test that says so.
- smart-doorbell's `visit_log.py` is deleted, and the doorbell's feed
  and stranger wall read from the platform.
- `face_id` has a producer, or the kind is removed — not neither.
