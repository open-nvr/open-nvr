# Publish a model on OpenNVR

You have a model. This is how it becomes something any OpenNVR
deployment can install and any app can use — without you writing an
integration for each app, and without the operator writing one for you.

The technical on-ramp is the SDK's own quickstart; this page is the
deal, the review, and the one idea everything rests on.

## Apps ask for a task, never for your adapter

An app's manifest says `requires_tasks: [license_plate_recognition]`.
It does not name an adapter, and it cannot. **Any adapter advertising
that task satisfies it**, so a better plate reader is a drop-in for the
shipped one and an operator swaps it without touching a single app.

That is the whole bargain of the contract, and it cuts both ways:

- **Match an existing task convention** (§5.x in
  [AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md), listed in
  `server/config/tasks.yml`) and every app that wants that capability
  can use your model on day one.
- **Invent a task name** and nothing asks for it. Your adapter installs,
  reports healthy, and receives no work — a failure that is completely
  silent. `opennvr-adapter validate` and the index validator both check
  this for exactly that reason.

An **alias** counts as a match. `tasks.yml` records the non-canonical
spellings that mean the same capability — `audio_transcription` for
`speech_to_text`, `visual_qa` for `vqa` — and the catalog, the routing
layer and `?task=` all compare canonical names, so advertising either
spelling reaches the same apps.

If your model genuinely does something no convention covers, propose the
convention first: open an issue against
`server/config/tasks.yml`.

## The deal

1. **Your adapter is yours.** `opennvr-adapter-sdk` is Apache-2.0 and
   talks to the platform over HTTP. Ship it under any licence — open,
   proprietary, or classified. You keep the copyright, your name is on
   the listing, and the image is published wherever you choose.
2. **OpenNVR takes no fee.** Sell the weights, the fine-tune, a hosted
   endpoint the adapter fronts, support — none of it passes through us.
3. **We check the contract, not the model.** A listing review is about
   whether a deployment can safely install and route work to your
   adapter. We do not benchmark your accuracy, and we do not rank
   adapters; the catalog shows the choices for a task and the operator
   decides.
4. **We don't break you.** The contract is versioned: SDK 1.x targets
   contract v1, and a contract v2 would ship SDK 2.x rather than
   changing v1 underneath you.

## Build it

```bash
pip install opennvr-adapter-sdk
opennvr-adapter new my-model --task object_detection
cd my-model
opennvr-adapter dev          # load the model, drive every endpoint
opennvr-adapter validate .   # the full conformance run
```

`validate` is the bar. It runs the same checks KAI-C runs, in-process,
and a green run means a deployment will accept your adapter. Put it in
your CI.

## Get it right where it is easy to get wrong

These are the things a review sends back, in the order they come up:

- **Normalized coordinates.** Boxes are 0–1 of the frame, not pixels.
  A pixel box passes every test you write and lands in the wrong place
  on the operator's screen.
- **A real fingerprint.** Hash the weights. KAI-C *skips* drift
  detection for a null fingerprint, so an adapter without one is
  silently exempt from the tamper check that protects the operator.
  `Adapter(weights=...)` does this for you.
- **The error category.** A body your model cannot use is a
  `transport_error` (400) and is not retried; a model that failed on
  valid input is a `model_error` (500). Misclassify the first as the
  second and one bad frame becomes a retry storm.
- **Honest permissions.** KAI-C refuses to register an adapter asking
  for more than the operator granted, so over-declaring blocks the
  install — and an undeclared egress host in an audit log gets the
  adapter removed.
- **Honest health.** `/health` must go red when the model did not load.
  A green dot on a dead adapter routes real work into a hole.
- **Say what it is bad at.** In the README and the listing summary. An
  operator who finds out later removes it; one who was told up front
  keeps it.

## List it

```bash
opennvr-adapter listing . --image ghcr.io/you/my-model:1.0.0 \
    --source https://github.com/you/my-model
```

That reads your adapter's own `/capabilities` and prints an index entry
— identity, version, advertised tasks, permissions, model info,
streaming support. Everything it cannot know is left as `TODO` for you:
the summary, the model card, the contact.

Fill those in and open a pull request adding the entry to
[`server/config/adapters_index.yml`](../server/config/adapters_index.yml).
CI runs `scripts/validate_adapters_index.py`, which is the same shape
check plus the task-convention check above.

Because the entry is generated from `/capabilities`, a listing cannot
claim a task your adapter does not advertise or a permission it does not
request. That is deliberate: it is what lets the catalog be trusted
without anyone auditing your source.

### What the review looks at

| | |
|---|---|
| Conformance | `opennvr-adapter validate` green, in your CI |
| Task | An existing convention, or a proposed one with a rationale |
| Image | Public, multi-arch where you can, and pinned in the listing |
| Model card | Where the weights came from, and their licence |
| Permissions | Match what the adapter requests; egress hosts named |
| Summary | One sentence an operator can decide on, including the trade-off |

### Distributing it yourself

You do not have to be in the catalog. An adapter is a container that
answers HTTP: an operator can run yours and point KAI-C at it without
anyone's permission. The catalog is discovery, not a gate.

## What an operator sees

`GET /api/v1/adapters/index` — the catalog, grouped by task, so the
question "what can read plates on this deployment?" has an answer. Your
entry sits beside the first-party one; `tier` records the relationship,
not a ranking.

## The contract

- [AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md) — the wire spec.
- Your adapter publishes its own **OpenAPI 3.1** at `/openapi.json` and
  **AsyncAPI 3.0** at `/asyncapi.json`, both generated from the contract
  types it returns.
- [DEVELOPER_PROGRAM.md](DEVELOPER_PROGRAM.md) — the equivalent deal for
  apps, which is the other half of this ecosystem.
