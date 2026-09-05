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
app's manifest `provides`, or the app id). When no camera names the app,
it sees every camera — the additive rule of
[CAMERA_ASSIGNMENTS.md](CAMERA_ASSIGNMENTS.md), the same rule the SDK's
`cameras_for_skill` applies client-side, now enforced where the frames are
handed out.

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

## Version negotiation

The register response's `registry.min_sdk_version` is the oldest SDK the
server still speaks to; the SDK logs a warning (never fails) when it is
older. `api_version` is bumped on any change to the register / config /
state / actions shapes.
