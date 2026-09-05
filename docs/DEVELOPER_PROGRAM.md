# Build on OpenNVR — the developer program

OpenNVR is a self-hosted AI video platform. **Everything an app needs
is already running**: the cameras and their streams, Tier-0 detection
on every frame, an inference layer with pluggable models, an event
bus, an evidence store, an alert inbox that rings operators, users and
per-camera permissions, and a catalog every deployment opens. You
write the part that is yours — the rule, the model, the workflow — and
the platform does the rest.

This page is the deal. The technical on-ramp starts at
[FIRST_DETECTOR.md](FIRST_DETECTOR.md).

## The deal, in five lines

1. **You keep 100%.** OpenNVR takes no fee, processes no payments and
   never sees your licence logic. A paid app is *your* product, sold on
   *your* terms, verified by *your* code (`verify_license`), listed in
   every deployment's catalog for free.
2. **Your code stays yours.** The SDK, the wire formats and the
   contracts are **Apache-2.0**. Ship your app under any licence,
   closed included. Only OpenNVR itself is AGPL — and you never link
   against it. See [LICENSING.md](LICENSING.md).
3. **We don't break you.** The registry contract is versioned and the
   [compatibility promise](#the-compatibility-promise) below is tested
   in CI against every core change.
4. **You get the platform, not plumbing.** One client object
   ([APP_PLATFORM.md](APP_PLATFORM.md)) covers cameras, frames,
   inference, recordings, history, alerts, durable state and events.
   No NATS, no HTTP, no SQL of your own.
5. **You get distribution.** A listing in the curated index appears in
   the App Catalog of every OpenNVR install — installable from your
   image, or a `kind: external` link to wherever you sell it.

## What the platform gives you

| You need | The platform provides | Read |
|---|---|---|
| Cameras assigned to your app, with frames | `OpenNVR().cameras()`, `.snapshot()`; roster scoped by the operator's assignments | [APP_PLATFORM.md](APP_PLATFORM.md) |
| Detections without running a model | `Detector` archetype on the Tier-0 stream — zero GPU cost | [FIRST_DETECTOR.md](FIRST_DETECTOR.md) |
| Your own model | KAI-C adapters (`AI_ADAPTER_CONTRACT.md`), `nvr.ai.infer()` / `.stream()` | [AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md) |
| Alerts that reach a human | `AlertDispatcher` → the operator inbox, bell, Twilio/webhook actions | [APP_SURFACES.md](APP_SURFACES.md) |
| Events other apps can build on, and theirs | `DomainEventPublisher` / `DomainEventSubscriber` | [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md) |
| History, evidence photos, recordings | `nvr.timeline`, `nvr.recordings(cam)` | [APP_PLATFORM.md](APP_PLATFORM.md) |
| State that survives restarts | `nvr.state` (per-app key/value in core) | [APP_PLATFORM.md](APP_PLATFORM.md) |
| An identity and a scope | Per-app key issued at registration; only your cameras, only your rows | [APP_CREDENTIALS.md](APP_CREDENTIALS.md) |
| Who is using your app | `current_user()` inside `/ui` and actions — id, name, cameras they may see | [APP_SURFACES.md](APP_SURFACES.md) |
| A UI, a config form, operator actions | Declared in the manifest; the catalog renders them | [APP_SURFACES.md](APP_SURFACES.md) |
| A storefront and a licence gate | `pricing`, `price_note`, `entitlement` + `verify_license` | [APP_SURFACES.md](APP_SURFACES.md#5b-selling-your-app-pricing-and-licences) |
| The operator API (users, roles, cameras, assignments) | `/api/v1/*`, Swagger at `/docs` | [PLATFORM_API.md](PLATFORM_API.md) |

## How to ship

0. **Install the SDK** — `pip install opennvr-app-sdk` (Apache-2.0, on
   PyPI; the repository's examples use it as an editable path).
1. **Scaffold** — `opennvr-app new my-app --task object_detection` (the
   generator ships in the SDK) gives you a compiling app with a test,
   pinned to the PyPI SDK; fill in one method. ([EXTERNAL_APP_WALKTHROUGH.md](EXTERNAL_APP_WALKTHROUGH.md)
   is the whole path, paid app included.)
2. **Run it against a stack** — `OPENNVR_URL` + the site key to
   bootstrap; the app is issued its own key on first registration and
   uses it from then on.
3. **Make it a product** — manifest `params` (config form),
   `state_schema` (live views), `actions` (operator verbs), `has_ui`
   (a dashboard), `pricing` / `entitlement` if you sell it.
4. **List it** — build and digest-pin your image and append one entry
   to `server/config/apps_index.yml` ([CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md));
   or, if you distribute it yourself, a `kind: external` entry that
   links to you.

## Paid apps and models

Paid listings are welcome and expected. The rules are short:

* OpenNVR **takes no fee** and **does not process payments**. Selling,
  invoicing and licence issuance happen on your side.
* A licensed app declares `entitlement: license_key`. The
  administrator enters the key in the catalog; core stores it
  encrypted and asks **your app** whether it is valid. You return the
  verdict — plan, expiry, limits. Core enforces it (the app cannot be
  enabled until you say yes) and never returns the key to anyone.
* Models are sold the same way: ship them inside your app image or as
  a KAI-C adapter, gate them with the same licence hook.
* The catalog shows your `pricing` badge and `price_note` verbatim.
  Keep the note factual; the platform quotes no prices of its own.
* First-party apps in this repository stay free.

## The compatibility promise

Apps are an investment; the platform must not spend it.

* **The registry contract is versioned.** `POST /apps/register` returns
  `registry.api_version` (semver-style `MAJOR.MINOR`) and
  `registry.min_sdk_version`. Additive changes bump MINOR; a breaking
  change to the register / config / state / actions / entitlement
  shapes bumps MAJOR and is announced in the changelog one minor
  release ahead.
* **Old SDKs keep working.** `min_sdk_version` only moves when an older
  SDK would *misbehave*, never merely because it lacks a feature, and
  the SDK logs a warning rather than failing when it is behind.
* **Event contracts are additive.** Domain-event schemas
  (`plate.recognized.v1`, …) gain fields; a changed field is a new
  `vN` subject ([EVENT_CONTRACTS.md](EVENT_CONTRACTS.md)).
* **It is tested.** `server/tests/test_registry_contract.py` pins the
  response shapes and the version relationship; the example apps run
  against every core PR in CI (`.github/workflows/ci.yml`), so an app
  that passes today keeps passing.
* **Deprecations are loud.** A field or route on its way out is marked
  in the response, in the docs and in the changelog for at least two
  minor releases before removal.

## Getting seen

* Every install shows your listing; the catalog groups by category and
  shows the pricing badge and author.
* Community apps are showcased in the project README and release
  notes — open your listing PR and say a line about it.
* A **verified** badge (identity confirmed, image reproducible from
  source — set by reviewers, see
  [CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md)) and a **Featured** row at
  the top of every catalog. Install counts are on the roadmap
  ([ROADMAP.md](ROADMAP.md)).

## Getting help

* Questions and design threads: GitHub Discussions.
* Bugs in the SDK or the contract: an issue tagged `sdk`.
* Something you need the platform to expose: an issue tagged
  `platform-api` — the rule of this project is that anything an
  example app needs from core goes into the SDK first, so a real
  third-party need is the strongest case there is.
