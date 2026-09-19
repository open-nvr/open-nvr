# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-115: the HA-facing contract, held to the code.

``server/contract/contract.json`` is what Home Assistant (pyopennvr) is
built against. These tests fail when the code drifts from it:

* the version matches ``core.contract.CONTRACT_VERSION``;
* every contract endpoint exists, with the same API-token scope;
* every websocket event type is one the bus publishes, and the descriptor
  fields/platforms/controls are what the code produces and handles;
* the fixtures and REAL responses (routes, websocket frames, descriptors)
  conform to the schemas;
* ``scripts/contract_check.py`` enforces the bump rules.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from core.contract_schema import errors
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "server/contract/contract.json").read_text(encoding="utf-8"))
SCHEMAS = CONTRACT["schemas"]
FIXTURES = ROOT / "server/contract/fixtures"


def _conforms(value, schema_name):
    errs = errors(value, SCHEMAS[schema_name], SCHEMAS)
    assert not errs, f"{schema_name}: {errs[:5]}"


def test_version_matches_the_code():
    from core.contract import CONTRACT_VERSION, FEATURES

    assert CONTRACT["contract_version"] == CONTRACT_VERSION == "1.1.0"
    assert len(set(FEATURES)) == len(FEATURES)
    # Removing a feature flag is breaking, so the contract lists them.
    assert CONTRACT["features"] == list(FEATURES)


def test_every_endpoint_exists_with_its_token_scope():
    from fastapi import FastAPI

    from services.api_tokens import TOKEN_ROUTES

    app = FastAPI()
    for mod in ("system", "cameras", "recordings", "streams", "timeline_events",
                "alerts_inbox", "events", "zones", "live_state", "media", "site_mode",
                "entities", "search", "api_tokens"):
        app.include_router(importlib.import_module(f"routers.{mod}").router, prefix="/api/v1")
    real = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    for e in CONTRACT["rest"]:
        key = (e["method"], e["path"])
        assert key in real, f"{key} is in the contract but not served"
        assert TOKEN_ROUTES.get(key) == e["token_scope"], f"{key}: token scope drifted"
        if e["response"]:
            assert e["response"] in SCHEMAS, e["response"]


def test_ws_types_and_descriptor_fields_match_the_code():
    from services import entity_descriptors as ed, event_bus_service as ebs

    published = {v for k, v in vars(ebs).items() if k.startswith("EVENT_")}
    for name in CONTRACT["ws_v2"]["event_types"]:
        assert name in published, f"ws event type {name!r} is never published"
    d = CONTRACT["descriptor"]
    assert set(d["platforms"]) == set(ed.PLATFORMS)
    fields = set(d["required"]) | set(d["optional"])
    import dataclasses

    produced = {f.name for f in dataclasses.fields(ed.Descriptor)} - {"state_path"}
    assert produced | {"descriptor_version"} == fields
    handled = {c for c in d["core_controls"]}
    # Commands run in services/entity_commands.py (HTTP and MQTT alike).
    import services.entity_commands as cmds

    source = Path(cmds.__file__).read_text(encoding="utf-8")
    for control in handled:
        assert f'"{control}"' in source, f"core control {control!r} is not handled"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixtures_conform(path):
    assert path.stem in SCHEMAS, f"fixture {path.name} has no schema"
    _conforms(json.loads(path.read_text(encoding="utf-8")), path.stem)


def test_real_responses_conform(env, monkeypatch):  # noqa: F811
    import services.live_state as ls_mod
    from services import event_bus_service as ebs

    monkeypatch.setattr(ls_mod, "_instance", ls_mod.LiveState())
    monkeypatch.setattr(ebs, "_event_bus_instance", ebs.EventBus())
    A = env.jwt("admin")
    get = lambda p, **kw: env.client.get(f"/api/v1{p}", headers=A, **kw).json()  # noqa: E731
    _conforms(get("/system/info"), "system_info")
    _conforms(get("/cameras/"), "camera_list")
    _conforms(get("/cameras/1"), "camera")
    env.client.post("/api/v1/cameras/1/zones", headers=A,
                    json={"name": "z", "polygon": [[0, 0], [1, 0], [1, 1]]})
    _conforms(get("/cameras/1/zones"), "zone_list")
    _conforms(get("/live-state"), "live_state")
    _conforms(get("/site-mode"), "site_mode")
    _conforms(get("/entities"), "entity_list")
    _conforms(get("/entities/states"), "entity_states")
    _conforms(get("/search"), "search")
    _conforms(get("/events"), "event_list")
    ticket = env.client.post("/api/v1/events/ws-ticket", headers=A).json()
    _conforms(ticket, "ws_ticket")
    with env.client.websocket_connect(f"/api/v1/events/ws?ticket={ticket['ticket']}&v=2") as ws:
        _conforms(ws.receive_json(), "ws_subscribed")
        _conforms(ws.receive_json(), "ws_state_snapshot")
    tok = _mint(env, scopes=["cameras.view", "recordings.view", "events.create"])["token"]
    ev = env.client.post("/api/v1/events", headers=_as(tok), json={"camera_id": 1})
    _conforms(ev.json(), "event")


# ── the bump rules ────────────────────────────────────────────────────

sys.path.insert(0, str(ROOT / "scripts"))
import contract_check as cc  # noqa: E402


def _with(version, **changes):
    c = json.loads(json.dumps(CONTRACT))
    c["contract_version"] = version
    for k, fn in changes.items():
        fn(c)
    return c


def test_bump_rules():
    base = _with("1.0.0")
    assert cc.verdict(None, base) == []
    assert cc.verdict(base, _with("1.0.0")) == []
    drop = {"x": lambda c: c["rest"].pop()}
    assert cc.verdict(base, _with("1.1.0", **drop))                   # breaking, minor: no
    assert cc.verdict(base, _with("2.0.0", **drop)) == []             # major: yes
    add = {"x": lambda c: c["ws_v2"]["event_types"].__setitem__("brand_new", ["payload"])}
    assert cc.verdict(base, _with("1.0.0", **add))                    # additive, no bump
    assert cc.verdict(base, _with("1.1.0", **add)) == []
    assert cc.verdict(base, _with("0.9.0"))                           # backwards
    feature_gone = {"x": lambda c: c["features"].pop()}
    assert cc.verdict(base, _with("1.1.0", **feature_gone))
    narrow = {"x": lambda c: c["schemas"]["system_info"]["required"].remove("site_id")}
    assert cc.verdict(base, _with("1.1.0", **narrow))
    retype = {"x": lambda c: c["schemas"]["system_info"]["properties"]["uptime_s"]
              .__setitem__("type", ["integer", "null"])}
    assert cc.verdict(base, _with("1.1.0", **retype))


def test_the_checker_passes_on_this_tree():
    """The script end to end: the version in code matches the file, a base
    without the contract passes, and a base that can't be resolved fails."""
    import subprocess

    root_commit = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"],
                                 capture_output=True, text=True, cwd=ROOT).stdout.split()[0]
    r = subprocess.run([sys.executable, str(ROOT / "scripts/contract_check.py"),
                        "--base", root_commit], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "contract_check" in r.stdout
    missing = subprocess.run([sys.executable, str(ROOT / "scripts/contract_check.py"),
                              "--base", "no/such/ref"], capture_output=True, text=True,
                             cwd=ROOT)
    assert missing.returncode != 0 and "not found" in (missing.stderr + missing.stdout)
