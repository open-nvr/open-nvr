<!--
Copyright (c) 2026 OpenNVR
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# App surfaces — skill, features, and "UI" for your app, all declared

> **Who this is for.** You've built (or are building) an app on the
> [OpenNVR App SDK](../sdk/opennvr-app-sdk/) — probably by following
> [**Your first detector in 15 minutes**](./FIRST_DETECTOR.md) — and now
> you want it to feel like a *product*: a live card in the App Catalog,
> operator-editable settings that apply without restarts, its own
> feature forms, and a presence the **OpenNVR Agent** can talk about.
> This guide is the map. Everything below is **declarative** — you
> describe surfaces in your manifest and config; the platform renders
> and routes them. You never ship a frontend.

The one-sentence version: **an OpenNVR app is a headless rule with a
declared face.** The declaration lives in two places — the
`AppManifest` (what you are, what you emit, what operators can
configure/see/do) and five optional config keys (how the platform
reaches you). Get those right and every surface below lights up.

---

## 0. The payoff, per surface

| Surface | Where it shows up | What you declare |
|---|---|---|
| Catalog card + install | App Catalog (store) | an [`apps_index.yml` entry](./CONTRIBUTING_APPS.md) + compose service |
| Config form | Catalog → Configure | `AppManifest.params` |
| **Live config** (no restart) | same form → running app | `on_config_update()` override |
| Status dot + health | Catalog card | the contract keys (§2) |
| **Live state views** | Catalog card, after "Check" | `AppManifest.state_schema` |
| **Feature forms (actions)** | Catalog card buttons | `AppManifest.actions` + `on_action()` |
| **Agent skill** | OpenNVR Agent skills panel + conversation | the contract keys (§2) — automatic |
| **Announcements** | notification feed, webhooks, voice | just emit alerts (you already do) |

---

## 1. The alert path — announcements come free

You already have this: every alert your app dispatches through the
SDK's `AlertDispatcher` rides `opennvr.alerts.>` on the bus. The
platform fans it out — the operator UI's alerts inbox, the webhook
fan-out, the Home Assistant relay if installed, **and the agent's
proactive channel**: "the license-plate app flagged a watchlist plate"
reaches the user as a notification (and spoken, on voice deployments)
with the tab closed. Conversationally, `recent_app_alerts` answers
"any alerts from my apps in the last hour?".

You do nothing beyond emitting well-formed alerts (§11.5 envelope —
the SDK does this for you). Declare your alert kinds in
`AppManifest.emits` so the catalog documents them.

## 2. The contract keys — one block that unlocks skill + status

Five optional config keys (read by the SDK's `ContractMixin` — see
[`contract.py`](../sdk/opennvr-app-sdk/opennvr_app_sdk/contract.py)):

```yaml
# config.docker.yml
contract_port: 92XX               # /health /manifest /state (+ /actions)
contract_host: "your-app-id"      # hostname advertised at registration
opennvr_url: "http://opennvr-core:8000"   # → self-registration + live config
# contract_bind_host / opennvr_token: rarely needed; the token falls
# back to the OPENNVR_INTERNAL_API_KEY env var the compose overlay sets.
```

With these set, on boot your app:

1. serves the **contract surface** — `GET /health` (status dot, stall
   detection), `GET /manifest`, `GET /state`;
2. **self-registers** with the app registry — your card appears under
   *Installed* with a live dot, and the **OpenNVR Agent's app door
   lists your app as a conversational skill** (`app:<your-id>` in its
   skills panel). The operator can ask "is the loitering app healthy?
   what's it seeing?" and the agent answers from your `/health` +
   `/state`;
3. starts **live config delivery** — see §3.

Every shipped example now carries this block in its
`config.docker.yml`; copy any of them. Pick the next free port in the
`92xx` range (see `docker-compose.apps.yml` for taken ones).

**No agent code is involved anywhere.** The agent discovers apps by
reading the registry — being registered *is* being a skill.

## 3. Live config — operator edits without restarts

`AppManifest.params` already gives you an auto-generated config form.
By default an edit lands in the registry and applies on your next
restart. To apply it **live**, override one hook:

```python
def on_config_update(self, config: dict) -> None:
    # Called from the SDK's poll thread on the first fetch and on
    # every change after. MUST be idempotent (the first call usually
    # re-delivers what the boot config already set). Swap state with
    # ONE attribute rebind so your run loop never sees a mixed pair.
    allow = {p.upper().strip() for p in (config.get("allowlist") or []) if p.strip()}
    deny  = {p.upper().strip() for p in (config.get("denylist") or []) if p.strip()}
    if (allow, deny) == self._watchlists:
        return
    self._watchlists = (allow, deny)
```

That's the real [license-plate-recognition](../examples/license-plate-recognition/)
implementation: an operator adds a plate in the catalog form and the
very next read routes severity through it. Apply live only what's
truly a hot knob (thresholds, watchlists, labels); topology changes
(cameras, adapters) may honestly need a restart — the SDK's default
hook logs exactly that, so doing nothing is also correct.

## 4. State views — "detailed reports" without a frontend

Expose live state via `state_snapshot()` (you likely already do), then
declare how to render it:

```python
from opennvr_app_sdk import StateView

AppManifest(
    ...,
    state_schema=[
        StateView(name="cameras", label="Live occupancy", kind="table",
                  path="cameras", columns=["id", "level", "last_count"]),
        StateView(name="denylist_size", label="Denylist", kind="metric",
                  path="denylist_size"),
    ],
)
```

`kind="metric"` renders a stat chip; `kind="table"` renders a compact
table (a list of dicts, a list of scalars, or a dict-of-dicts — the
key becomes the leading `id` column). `path` is a dot-path into your
`/state` dict; a missing path renders as an em-dash, never an error.
The catalog shows the views on your card the moment the operator
clicks **Check**. Live exemplars:
[occupancy-counting](../examples/occupancy-counting/) (table),
[license-plate-recognition](../examples/license-plate-recognition/)
(metrics that move as live config applies).

## 5. Actions — your app's feature set as operator forms

For verbs — *search my footage*, *enroll this face*, *export a
report* — declare an `Action` and implement `on_action`:

```python
from opennvr_app_sdk import Action, Param

AppManifest(
    ...,
    actions=[
        Action("search", "Search footage",
               params=[Param("query", str, required=True),
                       Param("limit", int, default=10)],
               description="Parse the query and search the footage index."),
    ],
)

def on_action(self, name: str, params: dict) -> dict:
    if name != "search":
        raise KeyError(name)          # → 404
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("'query' must be a non-empty string")  # → 400
    ...
    return {"results": [{"camera": ..., "when": ..., "caption": ...}]}
```

The catalog renders a button + form on your card; a result with a
`"results"` list renders as a table, anything else pretty-prints.
Live exemplar: [footage-search](../examples/footage-search/), whose
natural-language query moved from `docker compose exec` to a catalog
form with ~40 lines.

Rules of the road:

- **Only manifest-declared names dispatch** — the manifest is the
  single source of truth for what operators can invoke.
- `on_action` runs on the contract server's **thread** — open your own
  connections (don't share the run loop's sqlite handle), keep it
  bounded (the proxy times out at 10s), return JSON-serializable data.
- Bodies are capped at 64 KB — actions take form fields, not uploads.

### The governance boundary (read this before shipping an action)

Actions are **operator verbs**, and the platform enforces that in
layers: the catalog invokes them through a server proxy that requires
a **user JWT** (never the service key — the OpenNVR Agent *cannot*
invoke your action, by test-pinned design); your app's own `/actions`
endpoint requires the deployment's `X-Internal-Api-Key`; and every
invocation is audit-logged (param **keys** only — values like search
terms stay out of the log). The agent can *read* your state and *relay*
your alerts; it can never *act* on your app. Design your actions
assuming an authenticated human is on the other end — because one is.

### Who is asking: `current_user()`

The proxy tells you **which** human. Core signs the caller's identity
for your app (`X-OpenNVR-User`, a 60-second JWT over the SHA-256 of
your app key — see [APP_CREDENTIALS.md](APP_CREDENTIALS.md)) on every
`/ui` view and every action, and the SDK verifies it before your code
runs. Inside `ui_html()` and `on_action()`:

```python
from opennvr_app_sdk import current_user

def on_action(self, name, params):
    user = current_user()            # or self.current_user
    if user is None:                 # older core / no app key yet
        raise ValueError("no operator identity")
    if not user.can_manage(params["camera_id"]):
        raise ValueError("not permitted on that camera")   # → 400
    ...

def ui_html(self):
    user = self.current_user
    cams = user.visible(self.cameras) if user else []
    ...
```

`UserContext` carries `user_id`, `username`, `is_superuser`, `cameras`
(ids the user may *view*, `None` = every camera), `manage` (ids they may
*control*), and `purpose` (`"ui"` / `"action"`). Per-camera RBAC is
enforced by core on its own routes; this is how your app applies the
same assignment to what *it* shows and does. A token that fails any
check is treated as absent — never trusted partially.

Two things also changed on the proxies since this section was first
written: enabling/disabling an app is **superuser-only**, and
`PUT /apps/{id}/config` accepts only per-camera entries (zones,
tripwires) from a non-superuser, for cameras they manage — the
site-wide params of your manifest are an administrator's to change.
`GET /apps/{id}/status` returns a non-superuser only their cameras'
slice of your `/state`, keyed by the `camN` handles the SDK already
uses.

## 5b. Selling your app: pricing and licences

Three manifest fields turn a listing into a product:

```python
AppManifest(
    ...,
    pricing="subscription",           # free | paid | subscription | contact
    price_note="$29 / camera / year", # the human line under the badge
    entitlement="license_key",        # none | license_key
)
```

`pricing` and `price_note` are display only — the catalog shows the
badge on your card and on your App Store listing (the index entry
mirrors them; a listing can also be `kind: external` and link out to
where you distribute the app). `entitlement: license_key` is the gate:
an administrator enters a key in the catalog, core stores it encrypted
and asks **your app** whether it is valid, and refuses to enable the
app until you say yes. You decide what a key means:

```python
from opennvr_app_sdk import Entitlement

class MyApp(Detector):
    manifest = AppManifest(..., entitlement="license_key")

    def verify_license(self, license_key: str) -> Entitlement:
        claims = my_signature_check(license_key)          # or call your licence server
        if claims is None:
            return Entitlement(valid=False, message="unknown or tampered key")
        return Entitlement(valid=True, plan=claims.plan,
                           expires_at=claims.expires, limits={"cameras": claims.cameras})

    def on_entitlement_update(self, entitlement):        # optional
        self.max_cameras = entitlement.get("limits", {}).get("cameras")
```

Core calls `POST /entitlement/verify` on your contract port (site-key
gated like actions), records the verdict — status, plan, expiry,
message, limits — shows it to the administrator, and re-delivers it on
your live config poll as `self.entitlement` so you can feature-gate
yourself. Re-verification runs on every key change and on demand
(`POST /apps/{id}/license/verify`); an app that is unreachable when
asked keeps its previous verdict, so a restart never silently
un-licenses a site. Core never sees your licence logic and never
returns the key to anyone.

## 6. "What if I really need custom UI?"

You don't ship one — that's the platform bet that keeps a community
app ~200 auditable lines and the store reviewable. In practice the
ladder is:

1. **Params** cover settings. 2. **State views** cover dashboards.
3. **Actions** cover interactive features. If your idea genuinely
exceeds all three (a drawing canvas, a media browser), open an issue —
the right fix is usually a new declarative `kind` (the way
`geometry.polygon` params became a zone editor) so *every* app gets
it, not a bespoke frontend for one.

---

## Checklist — "my app is a full citizen"

- [ ] `AppManifest` with `id/name/version/category/summary`,
      `requires_tasks` using **canonical names** from
      [`server/config/tasks.yml`](../server/config/tasks.yml), `emits`
- [ ] `params` for every operator knob; `on_config_update` for the hot ones
- [ ] contract keys in `config.docker.yml` (§2) → status dot + agent skill
- [ ] `state_snapshot()` + `state_schema` for live views
- [ ] `actions` + `on_action` for feature verbs (governance rules, §5)
- [ ] store listing: [`CONTRIBUTING_APPS.md`](./CONTRIBUTING_APPS.md)
      (index entry + compose service + `make validate-apps-index`)

*See also:* [`FIRST_DETECTOR.md`](./FIRST_DETECTOR.md) (build the rule) ·
[`TWO_DOORS.md`](./TWO_DOORS.md) (why one class serves two doors, and
the agent boundary in full) ·
[`CONTRIBUTING_APPS.md`](./CONTRIBUTING_APPS.md) (ship it to the store).
