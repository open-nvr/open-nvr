# The outside-the-repo walk — building a paid app on the published SDK

Everything in `examples/` is built inside this repository, where the
SDK is an editable path and core is one directory away. A third-party
developer has none of that: they `pip install opennvr-app-sdk`, work in
their own repository, sell their app on their own site, and list it as
`kind: external`. This page is that walk, done for real against SDK
0.4.0 on PyPI, with what it found. Repeat it after any SDK release —
whatever breaks here is the next roadmap.

## The app: Plate VIP

A deliberately small but complete **paid** app, in its own repository,
depending on nothing but the wheel:

* **Archetype:** `DomainEventSubscriber` on `plate.recognized.v1` — it
  builds on the LPR app's events, so it needs no model and no frames.
* **Rule:** a watch list from the catalog config form (`params`,
  applied live through `on_config_update`), one alert per plate per
  cooldown; the cooldown is kept in core (`nvr.state`) so a restart
  never re-alerts.
* **Alerts:** `build_dispatcher` → the operator inbox over NATS.
* **Commerce:** manifest `pricing: subscription`, `price_note`,
  `entitlement: license_key`; `verify_license` checks a signed key the
  vendor issues (`issue_key`). Core's `PUT /apps/plate-vip/license` →
  `POST /entitlement/verify` exchange is exercised in the app's own
  tests against the SDK's contract server, with the site key.
* **Listing:** a `kind: external` index entry pointing at the vendor's
  page; the catalog shows the card, the pricing badge, *Learn more*,
  and never offers Install.

```
plate-vip/
├── plate_vip.py           # ~200 lines, of which the rule is 25
├── tests/test_plate_vip.py
├── pyproject.toml         # dependencies = ["opennvr-app-sdk>=0.4.0,<1.0"]
├── Dockerfile             # FROM python:3.12-slim; pip install opennvr-app-sdk
└── config.example.yml
```

Result: `uv sync` from PyPI, 7 tests green, the licence round-trip
identical to what core does, `docker build` with no checkout. The
platform surface an outsider needs **is all there**: the archetype, the
contract server, per-app credentials, the platform client, durable
state, the licence hook and the external listing all came from the
wheel.

## What it found

| # | Finding | Status |
|---|---|---|
| 1 | `scripts/create_opennvr_app.py --dest <outside>` still wrote an **editable path** to this checkout's SDK and a Dockerfile that only builds from the repo root — an outsider's first `uv sync` fails. | **Fixed:** `--sdk auto\|path\|pypi`; out-of-tree defaults to PyPI (`opennvr-app-sdk>=<version>,<1.0`, no `[tool.uv.sources]`, a self-contained Dockerfile). `server/tests/test_create_app_scaffold.py`. |
| 2 | Five server tests assumed **every index entry is installable** (`image`, `install`, a compose service) — the first real external listing would have broken CI. | **Fixed:** kind-aware (`test_apps_index.py`, `test_validate_apps_index.py`). |
| 3 | `DomainEventSubscriber` and `AlertSubscriber` hand you no dispatcher: unlike `Detector`, an event-driven app must `set_default_source` + `build_dispatcher` itself and call `.fire()`. | Open — a `self.dispatcher` built from the standard config keys would make the archetypes symmetric. |
| 4 | Every app re-declares the same ten config fields (`nats_*`, `contract_*`, `opennvr_url/token`, `webhook_url`, `nats_alerts_*`) and their YAML parsing. | Open — an SDK `BaseAppConfig` + `load_base_config()` that apps extend. |
| 5 | The generator needs a clone of this repository to run at all. | Open — ship it as `opennvr-app-sdk`'s console script (`opennvr-app new <id>`), templates inside the wheel. |
| 6 | Nothing shows an operator *why* an external app is greyed until they enter a key: the 402 text is right, the card could say "licence required" up front. | Open (frontend polish). |

Nothing in the list is a contract problem: the registry, entitlement
and platform routes behaved exactly as documented, and `min_sdk_version`
was satisfied by the wheel. The gaps are on-ramp ergonomics — the
generator and the boilerplate — which is what you would expect the
first outsider to hit.

## Repeating the walk

```bash
python3 scripts/create_opennvr_app.py my-app --task object_detection --dest ~/somewhere-else
cd ~/somewhere-else/my-app && uv sync && uv run pytest -q     # from PyPI, no checkout
```

Then make it paid (`pricing`, `entitlement`, `verify_license`), point a
`kind: external` index entry at it, run `make validate-apps-index`, and
enable it in a stack: the catalog must refuse with 402 until the key
you issued is entered.
