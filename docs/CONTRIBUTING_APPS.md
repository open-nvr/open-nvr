# Submitting an app to the OpenNVR App Store

This is the developer-facing guide for getting **your** app into the curated
OpenNVR App Store — the browse-and-install catalog an operator sees under
**Settings → App Catalog**. The model is the same one Homebrew taps and the
Home Assistant add-on store use: **you build and publish your app; you open a
PR that adds one entry to a curated index; a reviewer merges it.**

The index is one file — [`server/config/apps_index.yml`](../server/config/apps_index.yml).
Landing your app there is five steps:

1. [Build your app on the App SDK](#1-build-your-app-on-the-app-sdk)
2. [Publish your image and get its digest](#2-publish-your-image-and-get-its-digest)
3. [Add one entry to `apps_index.yml`](#3-add-one-entry-to-apps_indexyml)
4. [Validate it — `make validate-apps-index`](#4-validate-it)
5. [Open the PR](#5-open-the-pr)

---

## The trust model (read this first)

The App Store index is **curated and reviewed**, not open-write. Anyone can
propose an app; a maintainer reviews and merges the PR. That review is the
whole security boundary, because the index is what the one-click installer
trusts:

- The install endpoints (`POST /apps/index/{id}/install`) copy `image` and
  `image_digest` **from the curated index entry, never from the caller** —
  an operator can only install an app that a reviewer already vetted into
  this file (see [`docs/APPS_INSTALL.md`](APPS_INSTALL.md)).
- **A pinned `image_digest` is what makes one-click install trustworthy.**
  With a digest, the reconciler deploys `image@sha256:…` — the exact bytes
  the review vouched for (supply-chain integrity). Without one, it logs a
  loud `UNPINNED — dev only` warning and installs the floating tag. **A
  production / sovereign deployment installs only apps whose entry carries a
  digest**, so pin yours.

So your entry earns trust by being reviewable: the image is pinned, the
declared tasks are real, the manifest matches, and there are no secrets in
the file. The rest of this guide is how to satisfy that.

**Paid and external listings are welcome.** OpenNVR takes no fee and
processes no payments; a paid app is your product, and its licence is
checked by *your* code. Two extra fields cover it — `pricing` /
`price_note` for the badge the catalog shows, `entitlement: license_key`
if the administrator must enter a key before the app can be enabled. An
app you distribute yourself lists as `kind: external` with an
`external_url`: the catalog shows the card and a **Learn more** link
and never installs it. The rules and the reasoning are in
[`docs/DEVELOPER_PROGRAM.md`](DEVELOPER_PROGRAM.md); the licence hook
in [`docs/APP_SURFACES.md` §5b](APP_SURFACES.md).

---

## 1. Build your app on the App SDK

Your app is a container that speaks the OpenNVR App SDK contract. It
subscribes to the stack's NATS inference stream (or drives KAI-C directly),
serves the SDK's HTTP endpoints (`/health` `/manifest` `/state`), and
**self-registers with the app registry on boot** using the deployment's
`INTERNAL_API_KEY`. Once registered it shows up in the App Catalog with a
live status dot and an auto-generated config form.

> **New to the SDK? Start with the on-ramp.**
> [`docs/FIRST_DETECTOR.md`](FIRST_DETECTOR.md) — "Your first OpenNVR detector
> in 15 minutes" — scaffolds a runnable app with `opennvr-app new` (or `scripts/create_opennvr_app.py` in-tree),
> walks you through filling in the rule and getting its tests green, and lands
> you right back here at step 3 to publish. Come back once you have a working
> app under `examples/<id>/`.

- **SDK + manifest.** Build on
  [`sdk/opennvr-app-sdk/`](../sdk/opennvr-app-sdk/). Every app ships an
  [`AppManifest`](../sdk/opennvr-app-sdk/opennvr_app_sdk/manifest.py) — its
  declarative identity: `id`, `name`, `version`, `category`, `summary`,
  `requires_tasks`, `params`, `emits` — plus the optional surfaces that
  make it a full citizen (agent skill, live config, state views, action
  forms): see [**APP_SURFACES.md**](./APP_SURFACES.md). **The index
  entry mirrors this manifest** (see step 3), so decide these here
  first.
- **Start from a shipped example.** Everything under
  [`examples/`](../examples/README.md) is a copy-as-template starting point.
  The examples README's grid (drives-inference vs subscribes;
  inference-events vs alerts) tells you which shape is closest to yours —
  copy that folder and replace the predicate. (Don't confuse that grid with
  the platform's **"two doors"** — the two doors are the App Catalog and the
  OpenNVR Agent, the two front doors through which one `Detector` class is
  reached; see [`TWO_DOORS.md`](./TWO_DOORS.md). The grid is a different
  axis: how an app *consumes* the pipeline.)
- **Declare real tasks.** `requires_tasks` names the adapter task types your
  app depends on (`object_detection`, `multi_object_tracking`,
  `face_recognition`, `ocr`, `image_captioning`, …). These are the
  free-text task names from the adapter contract —
  [`docs/AI_ADAPTER_CONTRACT.md` §4 (`tasks_advertised`)](AI_ADAPTER_CONTRACT.md).
  The vocabulary is deliberately open, but **prefer a canonical name** from
  [`server/config/tasks.yml`](../server/config/tasks.yml) (the canonical
  task registry; `use_case_map.yml` supplements it) when one fits — the
  catalog greys out an app whose tasks no installed adapter advertises,
  so a task nobody provides makes your app un-runnable, and non-canonical
  spellings (`ocr`, `object_tracking`) won't match adapters advertising
  the canonical ones.

## 2. Publish your image and get its digest

Publish to GHCR (recommended) or any registry the operator's host can pull
from. Then capture the **immutable digest** your tag resolved to — that is
what pins the install.

```bash
# Build + push (GitHub Container Registry example)
docker build -t ghcr.io/<you>/my-app:1.0.0 -t ghcr.io/<you>/my-app:latest examples/my-app
docker push ghcr.io/<you>/my-app:1.0.0
docker push ghcr.io/<you>/my-app:latest

# Read back the sha256 digest the tag now points at
docker buildx imagetools inspect ghcr.io/<you>/my-app:latest --format '{{.Manifest.Digest}}'
# -> sha256:3f8c...e91   (this is your image_digest)
```

Make the package **public** so an operator can pull it without a login.

> Don't have a published image yet? An app that ships only as a local
> `build:` overlay (like the first-party detectors today) may omit
> `image_digest` and set `build_context` instead — but it will install
> **unpinned (dev only)**. Publish + pin before you expect production
> operators to install it.

## 3. Add one entry to `apps_index.yml`

Append **one** entry to
[`server/config/apps_index.yml`](../server/config/apps_index.yml). Copy the
annotated template —
[`docs/apps-index-entry.template.yml`](apps-index-entry.template.yml) — and
fill every field. Here it is inline for reference:

```yaml
- id: my-app                       # unique, kebab-case; matches AppManifest.id + examples/<id>/
  name: My App                     # human title on the catalog card
  summary: Alerts when <the thing your app watches for> happens.
  category: perimeter              # perimeter | analytics | vehicle | doorstep | forensics | integration
  version: "1.0.0"                 # matches AppManifest.version — QUOTE IT (a bare 1.0 is a YAML float; rejected)
  image: ghcr.io/<you>/my-app:latest       # well-formed ref: ghcr.io/... or opennvr/...
  image_digest: sha256:3f8c...e91          # from step 2 — pins the exact bytes (supply-chain integrity)
  requires_tasks: [object_detection]       # mirrors AppManifest.requires_tasks; prefer canonical names
  emits: [my_alert]                        # mirrors AppManifest.emits
  docs_url: https://github.com/<you>/<repo>/blob/main/README.md   # https:// URL (repo-relative paths are rejected)
  install:
    compose: |                     # the exact copy-paste an operator runs; secrets via ${VAR} ONLY
      services:
        my-app:
          profiles: [apps]
          image: ghcr.io/<you>/my-app:latest
          restart: unless-stopped
          environment:
            - OPENNVR_INTERNAL_API_KEY=${INTERNAL_API_KEY}   # from .env — never a literal
            - NATS_URL=nats://nats:4222
            - OPENNVR_URL=http://opennvr-core:8000
          expose:
            - "9210"
          networks:
            - opennvr_internal
    command: docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile apps up -d my-app
```

Rules the validator enforces (details in step 4):

- `id`, `name`, `summary`, `category`, `version`, `image`, `requires_tasks`,
  `docs_url`, and `install` (with a non-empty `compose` **and** `command`)
  are all required — **with the right YAML types** (quote your `version`: a
  bare `1.0` parses as a float and is rejected).
- `id` is unique, kebab-case, **and must exist as a service in
  `docker-compose.apps.yml`** — your submission includes the service block
  (plus its config-init companion; mirror `loitering-detection`) in the
  same PR. Both install paths run `docker compose … up -d <id>` against
  that overlay, so an entry without a service block is uninstallable for
  everyone and the gate refuses it.
- `install.command` is **exactly** the canonical
  `docker compose -f docker-compose.yml -f docker-compose.apps.yml
  --profile apps up -d <your-id>` — nothing free-form. The store renders
  this with a Copy button, so it is operator-executed content.
- `install.compose` must parse as YAML, may not use `privileged`,
  `cap_add`, `security_opt`, `network_mode`, `pid`, `ipc`, `devices`, or
  published `ports` (apps are internal-network + `expose` only), may not
  mount the Docker socket or absolute host paths, and its service `image`
  must match the entry's declared `image` (or the overlay's
  `${<ID>_IMAGE:-opennvr/<id>:local-build}` pin slot).
- `image` is a well-formed `ghcr.io/...` or `opennvr/...` ref;
  `image_digest`, if present, is `sha256:` + 64 hex chars.
- `docs_url` is an `https://` URL (link your README on GitHub).
- **No secrets.** The compose block *and* the command reference `${VAR}`
  placeholders (e.g. `${INTERNAL_API_KEY}`) from the operator's `.env` —
  never a literal key/password/token, in `KEY=value` **or** `KEY: value`
  form.

## 4. Validate it

Run the submission gate before you push. It's stdlib + PyYAML only, so it
works in a clean checkout with no backend running:

```bash
make validate-apps-index
# or directly:
python3 scripts/validate_apps_index.py
```

It prints one clear message per problem and exits non-zero on any hard
failure — required-field/type gaps, a non-kebab or duplicate `id`, an id
with no `docker-compose.apps.yml` service block, a non-canonical install
command, a dangerous compose directive (privileged / socket / host mounts
/ published ports), a snippet image diverging from the entry's, a
malformed `image` / `image_digest`, a non-https `docs_url`, a plaintext
secret, or an empty install block. An **unknown `requires_tasks` name only
warns** (free-text is allowed by the adapter contract), nudging you toward
a canonical name from `server/config/tasks.yml`. The same validator runs
in CI over the shipped index (`server/tests/test_validate_apps_index.py`),
so a green local run is the signal your entry will pass review
mechanically.

## 5. Open the PR

Branch off `main`, commit your one-entry addition, and open a PR (see the
general flow in [`CONTRIBUTING.md`](../CONTRIBUTING.md)). Keep it to the
index entry plus your app under `examples/<id>/` — one topic per PR.

**What reviewers check:**

- **Declared tasks are real / canonical.** `requires_tasks` names tasks an
  adapter can actually advertise — canonical where one fits
  (`use_case_map.yml`), and not a task nobody provides.
- **The image is pinned.** `image_digest` is present and correct, so
  one-click install is trustworthy. An unpinned entry is dev-only and won't
  be recommended for production operators.
- **The manifest matches.** `id` / `name` / `version` / `category` /
  `summary` / `requires_tasks` / `emits` in the index agree with your
  app's `AppManifest` — the index is a mirror, not a second source of truth.
- **No secrets.** The compose block uses `${VAR}` placeholders only; no
  literal key ever lands in the file.
- **The app self-registers.** On boot your app calls `POST /apps/register`
  with the deployment's `INTERNAL_API_KEY`, is issued its own key, and
  appears in the catalog — so "installed" actually means "running and
  registered".
- **Pricing is honest.** `pricing` is one of `free` / `paid` /
  `subscription` / `contact`; `price_note` is short and factual ("$49
  one-time", "per camera, monthly") and names no third party. A `paid` or
  `subscription` app that gates features declares `entitlement:
  license_key` and implements `verify_license` — a reviewer will try
  enabling it without a key and expect a 402.
- **External listings link somewhere real.** `kind: external` needs an
  https `external_url` under your control and an `author`; the
  validator rejects an install block on an external entry.

Once merged, your app is browsable in every OpenNVR deployment's App Catalog
and installable via the copy-paste command (always) or one click (where the
operator has opted in). Community apps are showcased in the project README
and the release notes — say a line about yours in the PR. If yours is a
first-party example, your name goes on it.

---

## Related reading

- [`docs/DEVELOPER_PROGRAM.md`](DEVELOPER_PROGRAM.md) — the deal for
  third-party developers: no fee, your licence, the compatibility promise.
- [`docs/FIRST_DETECTOR.md`](FIRST_DETECTOR.md) — the on-ramp: scaffold a
  runnable app with the generator, fill in the rule, get its tests green, run
  it against the stack — then land here to publish it.
- [`docs/APPS_INSTALL.md`](APPS_INSTALL.md) — the install security model:
  desired-state + reconciler, RBAC, digest pinning, audit. Explains *why*
  the pinned digest in your entry matters.
- [`docs/AI_ADAPTER_CONTRACT.md`](AI_ADAPTER_CONTRACT.md) §4 — the
  `tasks_advertised` vocabulary your `requires_tasks` reference.
- [`examples/README.md`](../examples/README.md) — the gallery, the
  drives-vs-subscribes grid, and how an example folder is structured.
- [`docs/apps-index-entry.template.yml`](apps-index-entry.template.yml) —
  the copy-paste entry template.
