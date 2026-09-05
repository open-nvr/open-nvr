# The operator API — provisioning OpenNVR from code

Everything the web UI does goes through `/api/v1/*`, so everything the
UI does can be scripted: creating users and roles, adding cameras,
assigning skills, granting per-camera access, installing and licensing
apps. This page is the map. The full, always-current reference is the
generated Swagger UI at **`/docs`** on any running stack (`/redoc` for
the reading version, `/openapi.json` for tooling).

Two other doors exist and are *not* this page:

* **Apps** talk to core with their own key through the SDK's
  `OpenNVR()` client — [APP_PLATFORM.md](APP_PLATFORM.md). They do not
  hold user credentials.
* **The OpenNVR Agent** (camera-agent) and adapters use the internal
  key on `/internal/*` — [TWO_DOORS.md](TWO_DOORS.md).

## 1. Authenticating

| Step | Route | Notes |
|---|---|---|
| Is this a fresh install? | `POST /auth/check-setup` | Returns `needs_setup`, `registration_open` |
| First administrator | `POST /auth/first-time-setup` | One-shot, needs the setup token printed by `init_db` |
| Log in | `POST /auth/login` (form) / `POST /auth/login-json` | Returns access + refresh JWTs; MFA-enrolled users pass the TOTP too |
| Refresh / log out | `POST /auth/refresh`, `POST /auth/logout` | |
| Who am I | `GET /auth/me`, `GET /users/me/permissions` | The second one is what the UI uses to hide controls |
| MFA | `POST /auth/mfa/setup`, `/mfa/verify`, `/mfa/disable` | |

Send `Authorization: Bearer <access token>` on every other call.

`POST /auth/register` — self-registration — is **closed by default**
(403). An operator opens it with the `public_registration_enabled`
setting; self-registered users get the default role and can never be
superusers by that path.

## 2. Users, roles, permissions

### Users — `/users`

| | Route | Who |
|---|---|---|
| Create | `POST /users/` | superuser (`users.manage`) |
| List / read | `GET /users/`, `GET /users/{id}` | `users.view` |
| Update | `PUT /users/{id}` | superuser; `PUT /users/me` for yourself |
| Deactivate / reactivate | `DELETE /users/{id}`, `POST /users/{id}/activate` | superuser |

`UserCreate` / `UserUpdate` carry `is_superuser`. Minting or promoting
a superuser, or demoting one, requires the **caller's current TOTP
code in the `X-MFA-Code` header** — a superuser sees every camera and
holds every permission, so it is the most privileged write in the API.
The last active superuser cannot be demoted, deactivated or deleted.

```bash
curl -X POST $NVR/api/v1/users/ -H "Authorization: Bearer $TOK" \
     -H "X-MFA-Code: 123456" -H "Content-Type: application/json" \
     -d '{"username":"ops","email":"ops@example.com","password":"…",
          "role_id":2,"is_superuser":false}'
```

### Roles and the permission catalogue — `/roles`, `/permissions`

Roles are named bundles of permissions; `init_db` seeds `admin`,
`operator` and `viewer`. `GET /permissions/` lists the catalogue,
`PUT /permissions/roles/{role_id}` sets a role's bundle, `POST /roles/`
creates a new role.

The catalogue is `resource.action` strings. The ones that gate what a
developer or integrator usually needs:

| Permission | Gates |
|---|---|
| `cameras.view` / `cameras.manage` | Read cameras / create, update, delete cameras and their assignments |
| `recordings.view` / `recordings.manage` | Playback / retention and deletion |
| `live.view` | Live streams |
| `alerts.view` / `alerts.manage` | The inbox / acknowledging and policy |
| `ai.view` / `ai.manage` | Adapters and models / configuring them, granting adapter permissions |
| `byom.manage` | Uploading and managing custom models |
| `apps.install` | One-click install of catalog apps (superuser still enables them) |
| `users.view` / `users.manage`, `roles.view` / `roles.manage`, `permissions.manage` | Identity administration |
| `settings.view` / `settings.manage` | Site settings |
| `audit.view`, `compliance.view` | Read the audit log / compliance reports |
| `network.*`, `onvif.discover`, `firmware.*`, `integrations.*`, `cloud.*`, `byok.manage` | The corresponding admin pages |

A superuser implicitly holds every permission.

## 3. Cameras — `/cameras`

| | Route | Who |
|---|---|---|
| Create | `POST /cameras/` | `cameras.manage`; the creator becomes the camera's **owner** |
| List / read | `GET /cameras/`, `GET /cameras/{id}` | scoped — see below |
| Update / delete | `PUT /cameras/{id}`, `DELETE /cameras/{id}` | owner, `can_manage` grantee, or superuser |
| Discover | `GET /cameras/by-ip/{ip}`, `POST /cameras/{id}/test-connection`, `POST /cameras/{id}/probe-transport` | |
| Streams | `GET /cameras/{id}/stream/urls`, `GET /cameras/{id}/snapshot`, PTZ routes | |
| Skills | `GET /cameras/assignable-skills`; `assignments` on create/update | `cameras.manage` |

### Assignments — what a camera *does*

`assignments` is a list of `{"skill": "...", "labels": [...]?}` (max 8,
one per skill). A skill is an open vocabulary
(`license_plate_recognition`, `occupancy_counting`, `loitering`, …).
Assignments are how an operator points an app at a camera: an app's
roster is every camera assigned a skill the app `provides` (or the app
id itself) — [CAMERA_ASSIGNMENTS.md](CAMERA_ASSIGNMENTS.md),
[APP_CREDENTIALS.md](APP_CREDENTIALS.md).

### Per-camera access — who may *see* a camera

Every camera has an owner; other users see it only through a grant.

| | Route |
|---|---|
| Grant / update | `POST /cameras/{id}/permissions` — `{"user_id", "can_view", "can_manage"}` |
| List grants | `GET /cameras/{id}/permissions` |
| Revoke | `DELETE /cameras/{id}/permissions/{user_id}` |
| Check | `GET /cameras/{id}/permissions/check` |

Owner or superuser may grant. The scope this produces is applied
everywhere a camera appears — timeline, recordings, occupancy, alerts,
app config and state — and is forwarded to apps as the signed user
context ([APP_SURFACES.md §5](APP_SURFACES.md)). Superusers see all
cameras; owner-less cameras are superuser-only.

## 4. Apps and licences — `/apps`

| | Route | Who |
|---|---|---|
| Catalog (curated index, merged with installed state) | `GET /apps/index` | any user |
| Install / uninstall a listing | `POST /apps/index/{id}/install`, `/uninstall`, `GET …/install-status` | `apps.install` |
| Installed apps | `GET /apps` | any user (scoped) |
| Enable / disable | `POST /apps/{id}/enable`, `/disable` | superuser — enable returns **402** while a licensed app has no valid key |
| Config | `GET/PUT /apps/{id}/config` | read: any user (own cameras only) / write: superuser or camera manager for per-camera keys |
| Live status, UI, actions | `GET /apps/{id}/status`, `GET /apps/{id}/ui`, `POST /apps/{id}/actions/{name}` | user context forwarded |
| Licence key | `PUT /apps/{id}/license`, `POST /apps/{id}/license/verify`, `DELETE /apps/{id}/license` | superuser; the key is stored encrypted and never returned |
| App credential | `POST /apps/{id}/key/rotate`, `DELETE /apps/{id}/key` | superuser |

Apps register *themselves* (`POST /apps/register`) with the SDK — an
operator never calls that. `kind: external` listings in the index are
links, not installs (400 on install).

## 5. Everything else

Recordings (`/recordings`), the timeline (`/events`), occupancy
(`/occupancy`), the alerts inbox (`/alerts-inbox`), AI models and
adapters (`/ai-models`, `/ai-model-management`, `/skills`),
integrations, network and firewall, audit logs (`/audit-logs`),
compliance, cloud — all under `/api/v1`, all in `/docs`, all subject to
the same permission catalogue and camera scope.

## Conventions

* JSON in, JSON out; errors are `{"detail": "..."}` with the HTTP
  status carrying the meaning (401 unauthenticated, 403 forbidden, 404
  not found *or* not visible to you, 402 licence required, 409 conflict).
* IDs are integers for users, roles and cameras; strings for apps and
  adapters.
* Timestamps are RFC 3339 / ISO 8601, UTC.
* Every write is audited (`GET /audit-logs`).
* Breaking changes to operator routes follow the same rule as the app
  contract: announced in `CHANGELOG.md` one minor release ahead
  ([DEVELOPER_PROGRAM.md](DEVELOPER_PROGRAM.md#the-compatibility-promise)).
