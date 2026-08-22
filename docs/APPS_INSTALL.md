# Opt-in one-click App install — desired-state + reconciler

OpenNVR's App Catalog can **discover** curated apps (browse-and-install)
and, when an operator opts in, **install** them with one click. This
document describes the security-critical core of that install path.

The design has one non-negotiable invariant:

> **The web app never runs Docker and never holds the Docker socket.**
> It only writes desired state. A separate, minimally-privileged
> reconciler applies it.

This is the sovereignty moat: a compromised web app cannot spawn
containers, and an air-gapped / sovereign deployment can leave one-click
install off entirely and still install apps via the copy-paste command.

---

## The two halves

```
   ┌───────────────────────┐        writes desired state         ┌──────────────────────────┐
   │  Web app (opennvr-core)│  ────────────────────────────────▶ │  app_install_intents      │
   │                        │   POST /apps/index/{id}/install     │  (DB table)               │
   │  • validates the id    │   POST /apps/index/{id}/uninstall   │                           │
   │    against the index   │                                     │  id, image, image_digest, │
   │  • writes ONE row      │                                     │  desired, status, message │
   │  • audits              │                                     └──────────────────────────┘
   │                        │                                                  ▲  │
   │  NO docker.            │                                        polls /   │  │ writes back
   │  NO subprocess.        │                                        applies   │  │ status
   │  NO socket.            │                                                  │  ▼
   └───────────────────────┘                                     ┌──────────────────────────┐
                                                                 │  app-installer reconciler │
                                                                 │  (scripts/app-installer)  │
                                                                 │                           │
                                                                 │  • THE ONLY component     │
                                                                 │    with the docker socket │
                                                                 │  • NOT network-facing     │
                                                                 │  • runs `docker compose`  │
                                                                 │    up / down per intent   │
                                                                 └──────────────────────────┘
```

* **Web app** (`server/routers/apps.py`): the `POST /install` and
  `POST /uninstall` endpoints do exactly three things — (a) enforce the
  gates, (b) validate the id against the curated index and copy
  `image`/`image_digest` from it, and (c) upsert a desired-state row and
  audit. There is **no** `subprocess`, `docker`, or `compose exec` call
  anywhere in the server process.

* **Reconciler** (`scripts/app-installer/`): a tiny, stdlib + SQLAlchemy
  + Docker-CLI service. It polls `app_install_intents`, and for each
  pending row drives `docker compose` up (`desired="installed"`) or down
  (`desired="absent"`), then writes back `status`/`message`. It opens no
  listening ports.

---

## Desired-state model — `app_install_intents`

One row per curated app (primary key is the app id, so re-requesting is
an upsert). Migration: `server/migrations/versions/
c8f42a1b6e93_add_app_install_intents.py` (mirrors the `installed_apps`
migration).

| column          | type          | meaning                                                        |
| --------------- | ------------- | -------------------------------------------------------------- |
| `id`            | `String(100)` | curated app id — **must exist in `apps_index.yml`** (PK)        |
| `image`         | `String(500)` | canonical image ref, **copied from the index** (never user input) |
| `image_digest`  | `String(100)` | `sha256:…` the reconciler pins to, or `NULL` (unpinned)         |
| `desired`       | `String(20)`  | `installed` \| `absent` — what the operator wants               |
| `status`        | `String(20)`  | `pending` \| `applied` \| `failed` — where the reconciler is    |
| `message`       | `Text`        | last-reconcile note (compose stderr on failure, etc.)          |
| `requested_by`  | `String(100)` | actor username who set the current desired state               |
| `requested_at`  | `DateTime`    | when the desired state was last (re)requested                  |
| `updated_at`    | `DateTime`    | when the reconciler last wrote back status                     |

**Why a DB table, not a state file?** The repo already models installed
apps as a queryable, migratable table (`installed_apps`), audit as rows,
and RBAC as rows. A table is consistent with that, is trivially
cross-referenced with `installed_apps` for the catalog UI, and gives the
reconciler a natural least-privilege boundary (SELECT the row, UPDATE
`status`/`message` only). A state directory would have been simpler to
mount but weaker to query and inconsistent with the codebase.

---

## Endpoints and their gates

All three live under the `/apps` router (`/api/v1/apps/...`).

| endpoint                                    | gate(s)                                          |
| ------------------------------------------- | ------------------------------------------------ |
| `POST /apps/index/{id}/install`             | `APPS_INSTALL_ENABLED` **and** `apps.install`    |
| `POST /apps/index/{id}/uninstall`           | `APPS_INSTALL_ENABLED` **and** `apps.install`    |
| `GET  /apps/index/{id}/install-status`      | authenticated user (read-only; any role)         |

Every gate (both 403 paths; at runtime the RBAC dependency actually
resolves first, so a caller lacking the permission gets its 403 even
while the flag is off):

1. **OPT-IN — `APPS_INSTALL_ENABLED`** (default **false**). When off, the
   two POST endpoints return `403 "one-click install disabled; use the
   copy-paste command"`. The command-display path (`GET /apps/index`)
   stays available. **Sovereign / air-gapped: leave this off.**
2. **RBAC — `apps.install`.** Install/uninstall require this named
   permission (not just any authenticated user). It is seeded by
   `server/scripts/init_db.py` on fresh installs — and idempotently
   re-ensured on every server startup, so upgraded deployments whose
   permission table predates it get the row too — and granted to no
   default role except admin; an operator must explicitly grant it.
   Superusers bypass, matching the rest of the RBAC surface.
3. **INDEX-ONLY.** The id must be present in `server/config/apps_index.yml`
   (404 otherwise). No arbitrary image or user-supplied field ever
   reaches the desired state — `image` and `image_digest` are copied
   **from the curated index entry**, not from the request body.
4. **AUDIT.** Every install/uninstall writes an audit row
   (`app.install.request` / `app.uninstall.request`) with the actor
   username, app id, image + digest, and desired action.
5. **INSTALLER RE-VALIDATION (defense in depth).** The gates above live
   in the web app — the very component the trust split assumes could be
   compromised. So the reconciler does NOT trust intent rows: it bakes
   its **own copy of the curated index** into its image and treats a row
   purely as a *selector* — the `image`/`image_digest` that actually
   deploy come from the installer's index, never from the row, a
   divergent row is logged and ignored, and an id that isn't kebab-case
   or isn't in the index is marked `failed` without ever touching
   Docker. An attacker who can write arbitrary rows to
   `app_install_intents` can therefore only start/stop apps the curated
   index already vouches for.

The install endpoint then upserts the intent (`desired="installed"`,
`status="pending"`) and returns it. Uninstall flips `desired="absent"`
and also drops the app's registration row so the catalog card moves back
to "Available to install" immediately.

---

## Digest pinning

`IndexEntry` gains an optional `image_digest` (`sha256:…`). When present,
the reconciler deploys the **digest-pinned** image
(`image@sha256:…`) — the exact bytes the curated index vouched for
(supply-chain integrity). When absent, the reconciler logs a loud

```
UNPINNED — dev only: app 'x' has no image_digest; deploying '…' without
supply-chain pinning. Do NOT run unpinned images in production.
```

warning and proceeds (so local dev with `:local-build` still works).

### How the pin reaches `docker compose` (wired, end-to-end)

The pin is not just logged — it takes effect through a per-service image
override env var:

1. `reconciler.image_env_key(app_id)` computes the var name via ONE
   shared transform — upper-snake-case + `_IMAGE`
   (`license-plate-recognition` → `LICENSE_PLATE_RECOGNITION_IMAGE`).
2. For a `desired="installed"` intent **with** a digest, the reconciler
   builds `{<KEY>: <image>@sha256:<digest>}` (`_run_env`) and passes it
   to the runner, which **merges it over `os.environ`** for the
   `docker compose … up -d <id>` subprocess.
3. Each app service in `docker-compose.apps.yml` declares
   `image: ${<KEY>:-opennvr/<id>:local-build}` alongside its `build:`
   block. With the var set, compose resolves the service image to the
   pinned ref and **pulls that exact digest**; with the var unset it
   resolves to the `:local-build` tag and uses the `build:` block. The
   argv stays a plain list (no shell), so nothing about the pin string
   is injectable.

The transform is unit-tested on both sides and the env-var name must
match between the reconciler and compose — that is the whole contract.

For a `desired="installed"` intent **without** a digest, no override is
passed (compose falls back to the local build) and the loud UNPINNED
warning above fires. Teardown (`desired="absent"`) passes no override —
`rm` doesn't resolve an image.

> **Unpinned = not for production.** A production / sovereign deployment
> should install only apps whose curated index entry carries an
> `image_digest`.

> **Submitting an app?** The curated index is populated by a reviewed,
> validated, PR-based flow — publish + digest-pin your image, add one entry
> to `apps_index.yml`, run `make validate-apps-index`, open a PR. That
> pinned digest is exactly what makes one-click install of *your* app
> trustworthy. See **[docs/CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md)**.

### Current state (be honest)

Pinning is fully **wired and takes effect** the moment the curated index
carries an `image_digest` for an app *and* that image is published to a
registry the host can pull from. **The app images ARE now published**:
`.github/workflows/publish-app-images.yml` pushes every catalog app to
`ghcr.io/open-nvr/<id>` on each main push and release tag (multi-arch,
same tag policy as core), so the index's `image` refs resolve. What the
index still ships WITHOUT is a per-entry `image_digest` — so an install
today logs the UNPINNED warning and pulls the tag. Recording digests in
the index at release time is the remaining step to make pinning bite;
the mechanism itself is complete and tested, and the local `build:`
overlay with `:local-build` tags remains the dev / air-gapped path.

---

## The reconciler

`scripts/app-installer/` — the single privileged, non-network-facing
component.

* `reconciler.py` — the pure reconcile core. `reconcile_intent(intent,
  runner, index=…)` returns `(status, message)`; the docker/subprocess
  call is an **injected `runner(argv, env)`** callable so unit tests pass
  a fake and never touch real Docker. The `index` is the installer's own
  baked copy of `apps_index.yml` (its **trust anchor** — see gate 5
  above): install intents must select an id in it, and the image/digest
  that deploy come from it, never from the DB row.
  `reconcile_once(store, runner, index=…)` sweeps every pending intent
  (skips `applied`, plus any id the caller's failure backoff is holding),
  calling `docker compose up -d <id>` for `installed` and
  `docker compose rm -s -f <id>` for `absent` (teardown needs only the
  kebab-case id check, so a de-listed app stays uninstallable). A
  non-zero exit → `status="failed"` with stderr in `message`. For a
  pinned entry the runner also receives the `{<ID>_IMAGE:
  image@sha256:…}` override (see *Digest pinning*), which
  `docker_runner` merges over `os.environ`.
* `store.py` — the production `IntentStore`: SQLAlchemy Core over
  `app_install_intents`. Least privilege: it only needs SELECT on the
  row and UPDATE of `status`/`message`/`updated_at` (it never INSERTs —
  only the web app does). The status write is **compare-and-swapped on
  the `desired` value the sweep acted on**, so an operator flipping
  install→uninstall mid-reconcile is never clobbered into a silently
  lost request — the guarded UPDATE matches nothing and the new request
  is picked up next sweep.
* `main.py` — the poll loop wiring `SqlIntentStore` + the real
  `docker_runner` into `reconcile_once`. Loads the baked index at
  startup and **refuses to start without it** (fail-closed). Applies
  exponential failure backoff per app (10s doubling to a 15-minute cap)
  so a permanently-failing intent doesn't churn Docker every poll tick.
* `tests/test_reconciler.py` — unit tests with a fake runner + fake
  store: the trust boundary (uncurated/non-kebab ids refused, DB-supplied
  images ignored in favor of the index), pending→up→applied, failed
  run→failed+message, absent→down (including for de-listed ids),
  unpinned→warning, digest-pin shape, CAS write-back, backoff skip,
  sweep semantics. No Docker.

### The single privileged mount

`docker-compose.installer.yml` (profile `app-installer`) is the only
place the Docker socket is mounted — into this one tiny service, nowhere
else in the stack. Be honest about what the `:ro` on that mount buys:
it makes the socket *inode* read-only, not the daemon API — full daemon
control (host-root-equivalent power) is available over a read-only-
mounted socket. The real containment is everything around the mount:
opt-in, non-network-facing, a minimal single-purpose image, and the
baked-index re-validation (gate 5) that caps what any attacker who
reaches the intents table can make it do.

The repo is mounted read-only **at the same path as on the host**
(`${PWD}:${PWD}`) — load-bearing, not cosmetic: the reconciler runs
`docker compose` inside the container, and compose resolves relative
binds like `./examples/…:/template` on the client side while the HOST
daemon performs the mount. Identical paths keep those binds pointing at
real host files. (Bring the installer up from the repo root, as every
command in this doc already does.)

The installer container **runs as root** (its Dockerfile sets no `USER`).
This is deliberate and required: the bind-mounted `/var/run/docker.sock`
has host-dependent group ownership, so root is the reliable way to open
it. This is acceptable because the installer is the **single privileged
component**, is **opt-in** (only started via the app-installer overlay),
and is **non-network-facing** (no `EXPOSE`, no listening ports) — its
only reachable surfaces are the DB and the socket. The web app, by
contrast, never touches the socket at all.

---

## How to enable

Deliberate operator decision — off by default.

```bash
# 1. Opt in (gates the endpoints).
echo "APPS_INSTALL_ENABLED=true" >> .env

# 2. Grant the apps.install permission to the operator role(s)
#    (Settings → Roles, or the permissions API). Admins already have it.

# 3. Bring up the single privileged reconciler.
docker compose -f docker-compose.yml -f docker-compose.installer.yml \
    --profile app-installer up -d app-installer
```

To stop reconciling (this does **not** uninstall apps):

```bash
docker compose -f docker-compose.yml -f docker-compose.installer.yml \
    --profile app-installer down
```

**If an install sits "pending":** the reconciler probably isn't running —
the endpoints being enabled (`APPS_INSTALL_ENABLED`) and the installer
container running are two separate opt-ins. The App Catalog stops polling
after ~5 minutes of pending and points here rather than showing
"Installing…" forever; bring the installer up (step 3) and the intent is
picked up on its next sweep.

**Sovereign / air-gapped guidance:** leave `APPS_INSTALL_ENABLED` unset,
do not run the installer overlay, and install apps with the copy-paste
`docker compose … up` command shown in the App Catalog. That path never
requires the socket-holding reconciler at all.

---

## What is deferred

* **Real end-to-end install.** Exercising the reconciler against a live
  daemon needs Docker on the host and **published + digested** app
  images on GHCR (today the apps ship as a `build:` overlay with
  `:local-build` tags). The reconcile logic is fully unit-tested with a
  fake runner; the missing piece is a published, digest-pinned registry.
* **Reconcile → installed_apps reflection.** The app still self-registers
  into `installed_apps` on boot (unchanged). Tying "intent applied" back
  to the catalog's installed/enabled state can be layered on later.
