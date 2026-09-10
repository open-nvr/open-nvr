# App credentials — every app gets its own key

**Status:** shipped (server `api_version` 1.1, SDK ≥ 0.2.0).

Until this change every SDK app booted with the deployment's
`INTERNAL_API_KEY` — the same secret the detect-pipeline and KAI-C hold.
Any app could therefore read every camera, every other app's config and
live state, and revoking one app meant rotating the key for the whole
stack. That is acceptable for the platform's own components and wrong for
a catalog of third-party apps.

## The model

| Credential | Who holds it | What it opens |
|---|---|---|
| **App key** `oak_<app-id>_<32 hex>` | one installed app | its own `GET /apps/{id}/config` and `/status`, re-registering itself, and the internal door (`/internal/camera-agent/cameras`, `/events`, evidence, `/recordings/frame`) **for its own camera roster** |
| **Site key** `INTERNAL_API_KEY` | platform components (detect-pipeline, KAI-C, the OpenNVR Agent) and bootstrap | everything the internal door serves, unscoped; the pipeline's write routes |
| **User JWT** | people | the operator API, per-camera RBAC applied |

An app's **roster** is the cameras the operator assigned to it on the
camera settings page (`Camera.assignments[].skill` naming one of the
app's manifest `provides`, or the app id). It is **closed by default**:
an app sees the cameras it was pointed at and no others, and an app
nobody has assigned a camera sees nothing
([CAMERA_ASSIGNMENTS.md](CAMERA_ASSIGNMENTS.md)) — the same rule the
SDK's `cameras_for_skill` applies client-side, enforced here where the
frames are actually handed out. This reverses the earlier additive rule,
under which an unassigned app got the whole fleet.

## The handshake

1. The app boots holding only the site key (`OPENNVR_INTERNAL_API_KEY`)
   and registers: `POST /api/v1/apps/register` with
   `{"url", "manifest", "sdk_version", "wants_key": true}`.
2. Core mints the app key, stores its SHA-256, and returns the key **once**
   in the response (`api_key`) alongside a compatibility line
   (`registry: {server_version, api_version, min_sdk_version}`).
3. The SDK persists it (`OPENNVR_APP_KEY_FILE`, default `.opennvr/app.key`
   under the working directory — mount a volume there, or set
   `OPENNVR_APP_KEY` outright) and sends it on every core call from then
   on: the config poll, camera discovery, the events store.
4. On a later boot the app registers **with its own key**; nothing new is
   minted. If the key was lost (fresh container, no volume) the app
   registers with the site key and `wants_key` again — the old key is
   invalidated, a new one issued. If core answers 401 to the app key
   (rotated or revoked by an administrator) the SDK discards it and
   bootstraps again at the next registration.

An app never needs to know any of this: `Detector` / `FrameApp` /
`AlertSubscriber` do it inside `register_with_opennvr()`. Apps that build
their own clients take headers from `opennvr_app_sdk.AppCredentials`
(`.headers()` / `.token()`) so a rotation lands everywhere at once.

## Operating it

* `POST /api/v1/apps/{id}/key/rotate` (superuser) — new key, returned once;
  the old one stops working immediately.
* `DELETE /api/v1/apps/{id}/key` (superuser) — revoke; the app can no
  longer read its config or its roster until it re-registers with the
  site key.
* `GET /api/v1/apps` shows `has_api_key` / `api_key_issued_at` per app; the
  key itself is never readable back.
* Audit rows: `app.register` carries `key_issued` and `sdk_version`;
  `app.key.rotate` / `app.key.revoke` name the administrator.

## Core calling your app, without the site key

Two things core does *to* your app are writes: invoking an action and
asking whether a licence key is valid. Until SDK 0.6 those were gated
on the deployment's `INTERNAL_API_KEY`, which meant every app was
handed the site-wide credential on every call. Now core proves itself
per app instead:

* Core sends **`X-OpenNVR-Call`** — an HS256 token signed with the
  sha256 of *your* app key (the same secret as `X-OpenNVR-User`), bound
  to your app id, to a purpose (`action` or `entitlement`) and to a
  60-second window.
* The SDK's contract server verifies it (`verify_call_token`) before
  `on_action` or `verify_license` runs. A token for another app, another
  purpose, or a stale one is a 401.
* Your app **never receives the site key**. The `opennvr_token` in your
  config is used once, to bootstrap registration; after that you hold
  only your own key.

Compatibility: an app registered with an SDK older than 0.6 cannot
verify the token, so core still sends `X-Internal-Api-Key` to it
(decided from the `sdk_version` it registered with) until it upgrades.
An SDK ≥ 0.6 app talking to a core older than `api_version` 1.3 accepts
the legacy site-key gate and logs one warning asking for the upgrade.
Forwarding the site key to old apps is removed at `api_version` 2.0.

## The apps bus: your own NATS user, not the site token

The last place the site key used to reach an app was the event bus:
every app joined NATS with `INTERNAL_API_KEY` and could publish and
subscribe to anything. Now:

* The stack runs a second NATS server, **`nats-apps`**, as a leaf of the
  platform bus. Apps join *that* one, as **user = app id, password =
  the app's own key**. Core tells a registering app where it is
  (`registry.bus = {url, auth: "app_key"}`); the SDK remembers it next
  to the key and uses it for the subscribe loop, alert fan-out and
  domain-event publishing. Nothing to configure in the app.
* Each app's **permissions come from its manifest**
  (`server/services/nats_users.py`): every app may read the platform's
  inference broadcasts (`opennvr.inference.>`, `opennvr.tier0.>`) and
  the alert stream; a domain-event family (`opennvr.events.plate.
  recognized.>`) only when the manifest declared
  `requires_scopes: ["events:plate.recognized"]` — this is where
  `requires_scopes` becomes a wall, not an audit row. An app may
  publish its **own** alert subjects (`opennvr.alerts.app.<id>.>`) and,
  if it `provides` a skill, domain events.
* Core renders the users file (bcrypt of each key — the key itself is
  never on disk) on start-up and on every key issue, rotate or revoke;
  `nats-apps` reloads within seconds. **Revoking an app's key
  disconnects it from the bus.**

Compatibility: an app on an older SDK, or one whose core advertises no
bus, keeps using the configured `nats_url` + `nats_token` on the
platform bus, exactly as before. Set `NATS_APPS_URL=` (empty) in `.env`
to turn the apps bus off.

### When alerts stop

The apps bus reaches the platform bus over **one leaf link**, and
everything crosses it: app alerts to the inbox, domain events to core,
the platform's detections to the apps. When that link is down nothing
looks broken — both NATS containers are healthy, every app logs
`joining NATS at ['nats://nats-apps:4222'] as <app id>` and stays
connected — and the operator's alerts simply stop. So:

* **Core watches the link** (`services/apps_bus_watch.py`): it polls
  `nats-apps`' monitoring endpoint (`/leafz` on port 8222, derived from
  `NATS_APPS_URL`; override with `NATS_APPS_MONITOR_URL`) every 30 s. No
  leaf connection for more than two minutes logs an error, raises a
  **high** inbox alert ("Apps bus is not linked to the platform bus")
  once an hour while it lasts, and shows under `GET /api/v1/apps/bus`
  (`linked`, `remotes`, `unlinked_for_s`). Recovery is logged.
* **The link authenticates with `INTERNAL_API_KEY`** — `nats-apps`
  presents it as the `opennvr-apps-bus` user on the platform server's
  port 7422. `nats/apps.conf` is a *template*: nats-server does not
  expand `$VAR` inside a URL, so `apps-entrypoint.sh` renders the key
  (percent-encoded — a base64 key carries `/`, `+`, `=`) into
  `/tmp/apps.conf` before starting, and refuses to start if the
  placeholder is still there. Both containers must see the same value.
* **`nats-apps` must be *up* first.** `docker compose ps nats-apps`
  showing `Restarting` means its config did not parse — read
  `docker logs opennvr_nats_apps`. nats-server joins every `include`
  onto the config file's directory (absolute paths too), which is why
  the entrypoint renders the config next to the users file and the
  template includes a bare `users.conf`. While it restarts, apps log
  `Temporary failure in name resolution` for `nats-apps` and reconnect
  forever; core raises "Apps bus is down" after two minutes.
* To look yourself: `curl -s http://nats-apps:8222/leafz` from inside
  the stack (`leafs` must not be empty); `docker logs opennvr_nats_apps`
  for `Leafnode Error 'Authorization Violation'`; `docker logs
  opennvr_nats` for `authentication error` on 7422.

Do **not** "fix" this by letting apps fall back to the platform bus with
the site token: that silently trades every app's manifest-scoped
permissions for the unrestricted bus the apps bus exists to take away.
An unlinked apps bus is an outage to surface, not a path to route
around.

## Version negotiation

The register response's `registry.min_sdk_version` is the oldest SDK the
server still speaks to; the SDK logs a warning (never fails) when it is
older. `api_version` is bumped on any change to the register / config /
state / actions shapes.

## User identity for your app

The same key hash is the shared secret behind `X-OpenNVR-User`: on every
proxied `/ui` view and action, core attaches a 60-second HS256 token
naming the operator (id, username, superuser flag, the camera ids they
may view and manage). The SDK verifies it against `sha256(app key)` and
exposes it as `current_user()` — see
[APP_SURFACES.md](APP_SURFACES.md#who-is-asking-current_user). No key
issued → no identity forwarded.
