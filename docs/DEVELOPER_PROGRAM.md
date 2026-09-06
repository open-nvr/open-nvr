# Build on OpenNVR

OpenNVR is a self-hosted AI video platform. Everything an app needs is
already running on every install: cameras and their streams, detection on
every frame, pluggable models, an event bus, an evidence store, an alert
inbox that reaches operators, users with per-camera permissions, and a
catalog that every deployment opens. **You write the rule, the model or the
workflow. The platform does the rest.**

This page is the whole deal, in the order you will ask about it. The
technical on-ramp is [FIRST_DETECTOR.md](FIRST_DETECTOR.md) — a working app
in fifteen minutes.

## The deal

1. **Your app is open source, and it is yours.** Every app in the catalog
   lives in a public repository under the `open-nvr` GitHub organisation,
   under **AGPL-3.0 or Apache-2.0 — your choice**. You keep the copyright.
   Your name and contact are on the listing, in the README and in the
   source headers. We never rewrite history: the first commit is yours.
2. **We build it, host it and ship it.** CI builds your image from your
   tagged source, pins the digest into the index, and every OpenNVR install
   can install it with one click. You don't run a registry, a CDN or a
   download page.
3. **You can charge for it.** OpenNVR takes **no fee**. Sell a fine-tuned
   model, a data service, a hosted notifier, support — the platform gives
   you the licence hook (`entitlement: license_key`, verified by *your*
   code) and the catalog shows your price. What is open is the code;
   what you sell is what the code needs. See [Selling](#selling-your-app).
4. **We don't break you.** The app contract is versioned and tested on
   every change to core — the [compatibility promise](#the-compatibility-promise).
5. **Operators can trust what they install.** Because every catalog app
   is open, built from source and reviewed, the App Catalog can say
   something no closed platform can: *nothing you install here can hide
   what it does with your video.* That is what makes your app installable
   in a hospital, a port or a ministry.

If you would rather ship closed code, you can still be listed — as an
**external** app that links to you. External apps are never installed by
the platform and carry a "not reviewed by OpenNVR" mark. It is an honest
place, not a lesser one; it is simply not the catalog.

## Why build here

*Because the hard part is done.* A detector app is one method. The SDK
owns the NATS loop, alert fan-out, the contract server, registration,
live config, credentials and the platform client. [Plate VIP](EXTERNAL_APP_WALKTHROUGH.md),
a complete paid app with a watch list, cooldowns that survive restarts and
a licence gate, is about 200 lines — and 50 of those are the licence demo.

*Because your users are already there.* Every install shows the catalog.
There is no store approval queue and no revenue share — a reviewed PR is
the whole process.

*Because you are not locked in.* The SDK and the wire contracts are
Apache-2.0; an app that talks to OpenNVR is a container that speaks HTTP
and NATS. Your code runs anywhere those exist.

*Because the users who need this cannot use the alternatives.* Regulated
sectors and NDAA-restricted procurement cannot buy the cloud incumbents
or the restricted vendors. An open, self-hosted, auditable platform is
what they are allowed to install — and an app in its catalog is what
they are allowed to add.

## What the platform gives you

| You need | The platform provides | Read |
|---|---|---|
| Cameras assigned to your app, with frames | `OpenNVR().cameras()`, `.snapshot()`; roster scoped by the operator's assignments | [APP_PLATFORM.md](APP_PLATFORM.md) |
| Detections without running a model | `Detector` on the Tier-0 stream — zero GPU cost | [FIRST_DETECTOR.md](FIRST_DETECTOR.md) |
| Your own model | KAI-C adapters, `nvr.ai.infer()` / `.stream()` | [AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md) |
| Alerts that reach a human | `Alert` → the operator inbox, bell, actions | [APP_SURFACES.md](APP_SURFACES.md) |
| Events other apps publish, and yours | `DomainEventSubscriber` / `DomainEventPublisher` | [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md) |
| History, evidence photos, recordings | `nvr.timeline`, `nvr.recordings(cam)` | [APP_PLATFORM.md](APP_PLATFORM.md) |
| State that survives restarts | `nvr.state` | [APP_PLATFORM.md](APP_PLATFORM.md) |
| An identity and a scope | Per-app key; only your cameras, only your rows | [APP_CREDENTIALS.md](APP_CREDENTIALS.md) |
| Who is using your app | `current_user()` in `/ui` and actions | [APP_SURFACES.md](APP_SURFACES.md) |
| A config form, live views, operator actions, a UI | Declared in the manifest; the catalog renders them | [APP_SURFACES.md](APP_SURFACES.md) |
| A storefront and a licence gate | `pricing`, `price_note`, `entitlement` + `verify_license` | [APP_SURFACES.md](APP_SURFACES.md#5b-selling-your-app-pricing-and-licences) |
| Async, for a UI or an agent loop | `opennvr_app_sdk.aio.AsyncOpenNVR` | [APP_PLATFORM.md](APP_PLATFORM.md) |
| The operator API | `/api/v1/*`, Swagger at `/docs` | [PLATFORM_API.md](PLATFORM_API.md) |

## How to ship

1. **Install the SDK and scaffold.**
   `pip install opennvr-app-sdk && opennvr-app new my-app --task object_detection`
   gives you a running app with a test. Fill in one method.
2. **Run it against a stack.** Point it at any OpenNVR
   ([LOCAL_SETUP.md](LOCAL_SETUP.md) gets one up in minutes); it
   self-registers, is issued its own key and appears in the catalog.
3. **Make it a product.** Manifest `params` (config form), `state_schema`
   (live views), `actions` (operator verbs), `has_ui` (a page), and
   `pricing` / `entitlement` if you sell something.
4. **Ask for a repository.** Open an issue titled *App: my-app* with a
   link to your code. We create `open-nvr/app-my-app`, transfer or seed it
   from your repository, and you are its maintainer.
5. **List it.** One PR adding an entry to `server/config/apps_index.yml`
   — [CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md). CI builds and pins the
   image; a reviewer merges; every install sees it.

Reviews get a first response within **five working days**; issues tagged
`sdk` within **two**. Security reports go through a
[private advisory](https://github.com/open-nvr/open-nvr/security/advisories/new).

## Selling your app

Paid apps are welcome and expected. The rule is one sentence: **the code
in the catalog is open; what you sell is something the code needs.**
That is the model Frigate+ proved — the NVR is free, the better models
cost $50 a year — and it is the only model that keeps the catalog
auditable.

* Declare `pricing` (`paid`, `subscription`, `contact`) and a factual
  `price_note`; the catalog shows both.
* Declare `entitlement: license_key` and implement `verify_license`. The
  administrator pastes a key; core asks **your app** whether it is valid;
  the app cannot be enabled until you say yes. Core stores the key
  encrypted and never returns it.
* Offer a trial: return `Entitlement(valid=True, plan="trial",
  expires_at=…)` — the catalog shows the expiry.
* Verification must work offline or degrade gracefully; an OpenNVR site
  may have no internet.
* OpenNVR takes no fee and processes no payments. Your shop, your terms,
  your invoice.

What you can sell this way: a fine-tuned model for the customer's site, a
plate or face database, a hosted notifier or reporting service,
integration with your own product, priority support. What you cannot:
closed code in the catalog. Ship that as an external listing.

## The compatibility promise

* **The registry contract is versioned.** `POST /apps/register` returns
  `registry.api_version` and `registry.min_sdk_version`. Additive
  changes bump the minor; a breaking change to the register / config /
  state / actions / entitlement shapes bumps the major and is announced
  in `CHANGELOG.md` one minor release ahead.
* **Old SDKs keep working.** `min_sdk_version` moves only when an older
  SDK would misbehave, never because it lacks a feature; the SDK warns,
  it does not fail.
* **Event contracts are additive.** A changed field is a new `vN`
  subject.
* **It is tested.** `server/tests/test_registry_contract.py` pins the
  shapes; every example app runs against every core PR in CI.
* **Deprecations are loud**: marked in the response, the docs and the
  changelog for two minor releases before removal.

| Core | `api_version` | Minimum SDK | Notable |
|---|---|---|---|
| main (Sep 2026) | 1.3 | 0.2.0 | `X-OpenNVR-Call`: core proves itself per app; the site key no longer reaches apps on SDK ≥ 0.6 |
| 0.2.x line | 1.2 | 0.2.0 | per-app keys, user context, platform client, entitlements, async client, `opennvr-app new` |
| 0.1.4 | 1.0 | — | registry contract: register, config, state, actions |

## Security: what an app can and cannot do

This is what an operator reads before installing your app, so it is
worth knowing.

* An app holds **its own key**, sees **only the cameras assigned to it**,
  and reads only its own config, state and alerts. It never sees another
  app's data — and it **never receives the site key**: core proves
  itself to your app with a per-app signed token, and your app joins
  the event bus as its own user, with subject permissions derived from
  your manifest.
* An app declares its **network egress** (`network_egress: [...]` in the
  index entry). The operator sees the declared hosts on the card before
  installing; a catalog app with an empty list is one that never talks
  to the internet.
* Catalog images are **built from your tagged source by CI** and
  digest-pinned; what a reviewer read is what runs.
* Video, plates and faces **do not leave the site** unless the operator
  enables a feature that says so, in words, on the card.

Do not design around these; design with them. An app that respects them
is installable where the closed alternatives are not.

## Getting seen

* Every install shows your listing, with your name and a link to your
  contact.
* New apps are announced in the release notes and on the project's
  channels; say a line about yours in the listing PR.
* **Maintainer-verified** marks apps the OpenNVR maintainers run in
  production; a **Featured** row highlights a few — both are editorial,
  set by reviewers ([CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md)).

## Getting help

* Design questions and "how would I…": GitHub Discussions.
* SDK bugs: an issue tagged `sdk` (two working days).
* Something you need the platform to expose: an issue tagged
  `platform-api`. The rule of this project is that anything an app needs
  from core goes into the SDK first — a real third-party need is the
  strongest case there is, and most of the SDK exists because an app
  asked.
* The terms under which apps are listed: [APP_LISTING_TERMS.md](APP_LISTING_TERMS.md).
