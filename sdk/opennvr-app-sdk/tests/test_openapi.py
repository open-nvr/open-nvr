# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Generated specs for an app's own surfaces.

The claim these tests defend is that the documents are *derived*, never
maintained: a declared action is a path, a declared param is a schema,
and an app that declares no licence gate has no `/entitlement/verify`.
A hand-written spec drifts; a generated one cannot.
"""
from __future__ import annotations

import json

import pytest

from opennvr_app_sdk import (
    Action, AlertType, App, AppManifest, Param, StateView,
    contract_asyncapi, contract_openapi,
)
from opennvr_app_sdk.openapi import CONTRACT_API_VERSION, param_schema, prune
from opennvr_app_sdk.testing import RecorderChannel, app_config

try:
    from openapi_spec_validator import validate as validate_openapi
except ImportError:  # pragma: no cover — optional dev dependency
    validate_openapi = None

requires_validator = pytest.mark.skipif(
    validate_openapi is None, reason="openapi-spec-validator not installed")


def manifest(**overrides) -> AppManifest:
    base = dict(
        id="gate-watch", name="Gate Watch", version="2.1.0", category="perimeter",
        summary="Watches the gate.", subscribes="opennvr.inference.>",
        emits=[AlertType("vehicle-at-gate", severity="high")],
    )
    base.update(overrides)
    return AppManifest(**base)


# ── OpenAPI: the shape ──────────────────────────────────────────────


def test_every_app_documents_the_three_contract_endpoints():
    spec = contract_openapi(manifest())
    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == {"/health", "/manifest", "/state"}
    assert spec["info"]["version"] == "2.1.0"
    assert spec["info"]["x-opennvr-app-id"] == "gate-watch"
    assert spec["info"]["x-opennvr-contract-version"] == CONTRACT_API_VERSION


def test_actions_become_paths_with_typed_bodies():
    spec = contract_openapi(manifest(actions=[Action(
        name="search", label="Search footage", confirm=True,
        params=[Param("query", str, required=True, description="What to look for."),
                Param("hours", int, default=24)],
    )]))
    op = spec["paths"]["/actions/search"]["post"]
    assert op["operationId"] == "actionSearch"
    assert op["summary"] == "Search footage"
    assert "asks for confirmation" in op["description"]
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body["properties"]["query"] == {
        "type": "string", "description": "What to look for."}
    assert body["properties"]["hours"] == {"type": "integer", "default": 24}
    assert body["required"] == ["query"]
    assert body["additionalProperties"] is False
    # Actions are the only write, and they are gated.
    assert op["security"] == [{"internalKey": [], "callToken": []}]
    assert set(op["responses"]) >= {"200", "400", "401", "404", "413", "500"}


def test_no_actions_means_no_action_paths():
    assert not [p for p in contract_openapi(manifest()) ["paths"] if "/actions" in p]


def test_entitlement_path_appears_only_for_a_licensed_app():
    assert "/entitlement/verify" not in contract_openapi(manifest())["paths"]
    spec = contract_openapi(manifest(
        pricing="paid", price_note="$29/camera/year", entitlement="license_key"))
    assert "/entitlement/verify" in spec["paths"]
    assert "Entitlement" in spec["components"]["schemas"]
    assert "no part in the transaction" in \
        spec["paths"]["/entitlement/verify"]["post"]["description"]


def test_ui_path_appears_only_for_an_internal_ui():
    assert "/ui" not in contract_openapi(manifest())["paths"]
    assert "/ui" in contract_openapi(manifest(has_ui=True))["paths"]
    # An external UI is not served on the contract port at all.
    external = manifest(has_ui=True, ui_mode="external", ui_url="http://{host}:8090/")
    assert "/ui" not in contract_openapi(external)["paths"]


def test_state_schema_documents_the_state_payload():
    spec = contract_openapi(manifest(state_schema=[
        StateView(name="plates", label="Recent plates", kind="table", path="recent"),
        StateView(name="size", label="Denylist", kind="metric", path="denylist_size"),
    ]))
    props = spec["components"]["schemas"]["State"]["properties"]
    assert set(props) == {"recent", "denylist_size"}
    assert "Recent plates (table)" in props["recent"]["description"]


def test_server_variables_name_the_app_and_its_port():
    spec = contract_openapi(manifest(), port=9210)
    variables = spec["servers"][0]["variables"]
    assert variables["host"]["default"] == "gate-watch"
    assert variables["port"]["default"] == "9210"


def test_author_and_licence_reach_the_info_block():
    spec = contract_openapi(manifest(author="ACME", website="https://acme.example",
                                     license="Apache-2.0"))
    assert spec["info"]["license"] == {"name": "Apache-2.0"}
    assert spec["info"]["contact"]["name"] == "ACME"


# ── OpenAPI: param typing ───────────────────────────────────────────


@pytest.mark.parametrize("type_, expected", [
    (float, "number"), (int, "integer"), (str, "string"),
    (bool, "boolean"), (list, "array"), (dict, "object"),
])
def test_python_types_map_to_json_schema(type_, expected):
    assert param_schema(Param("x", type_))["type"] == expected


def test_unknown_types_degrade_to_string_not_to_a_blob():
    assert param_schema(Param("x", "something.custom"))["type"] == "string"


def test_geometry_types_carry_their_shape_and_a_ui_hint():
    schema = param_schema(Param("zones", "geometry.polygon", per_camera=True))
    # per_camera means "keyed by camera id" on the wire.
    assert schema["type"] == "object"
    assert schema["x-opennvr-per-camera"] is True
    inner = schema["additionalProperties"]
    assert inner["x-opennvr-ui"] == "geometry.polygon"
    assert inner["items"]["minItems"] == 2       # an [x, y] vertex
    assert inner["minItems"] == 3                # a polygon


def test_suggestions_become_examples():
    schema = param_schema(Param("labels", list, suggestions=["person", "car"]))
    assert schema["examples"] == ["person", "car"]


# ── OpenAPI: validity ───────────────────────────────────────────────


@requires_validator
def test_the_generated_document_is_a_valid_openapi_31_document():
    spec = prune(contract_openapi(manifest(
        has_ui=True, author="ACME", website="https://acme.example",
        license="Apache-2.0", pricing="paid", price_note="$29",
        entitlement="license_key",
        state_schema=[StateView(name="n", label="L", kind="gauge", path="zones")],
        actions=[Action(name="search", label="Search", params=[
            Param("query", str, required=True),
            Param("zone", "geometry.polygon", per_camera=True),
        ])],
    )))
    validate_openapi(spec)


@requires_validator
def test_a_facade_app_generates_a_valid_document():
    app = App("driveway-watch", name="Driveway Watch", category="perimeter",
              summary="s")

    @app.on_detection("person", zone="driveway", dwell=30, severity="high")
    def loitering(event):
        event.alert("x")

    validate_openapi(prune(contract_openapi(app.manifest())))


def test_prune_drops_nulls_but_keeps_falsey_values():
    assert prune({"a": None, "b": 0, "c": "", "d": [None, {"e": None, "f": False}]}) \
        == {"b": 0, "c": "", "d": [None, {"f": False}]}


# ── AsyncAPI ────────────────────────────────────────────────────────


def test_asyncapi_documents_what_the_app_consumes_and_publishes():
    spec = contract_asyncapi(manifest())
    assert spec["asyncapi"] == "3.0.0"
    assert spec["servers"]["bus"]["protocol"] == "nats"
    assert spec["channels"]["inference"]["address"] == "opennvr.inference.>"
    assert spec["channels"]["alerts"]["address"] == \
        "opennvr.alerts.app.gate-watch.{camera_id}"
    assert set(spec["operations"]) == {"receiveInference", "sendAlerts"}
    assert spec["operations"]["receiveInference"]["action"] == "receive"
    assert spec["operations"]["sendAlerts"]["action"] == "send"
    assert "vehicle-at-gate (high)" in spec["operations"]["sendAlerts"]["description"]


def test_the_subscribe_channel_is_typed_by_its_subject_tree():
    """A Detector, a DomainEventSubscriber and an AlertSubscriber all
    fill ``subscribes``, but carry different envelopes."""
    cases = {
        "opennvr.inference.>": ("inference", "InferenceCompleted"),
        "opennvr.events.plate.recognized.v1.>": ("domainEvents", "DomainEvent"),
        "opennvr.alerts.>": ("alertStream", "Alert"),
    }
    for address, (key, message) in cases.items():
        spec = contract_asyncapi(manifest(subscribes=address, emits=[]))
        channel = spec["channels"][key]
        assert channel["address"] == address
        assert message in json.dumps(channel["messages"])


def test_requested_scopes_become_channels():
    spec = contract_asyncapi(manifest(
        requires_scopes=["events:plate.recognized", "events:visit.recorded"]))
    assert spec["channels"]["eventPlateRecognized"]["address"] == \
        "opennvr.events.plate.recognized.v1.{camera_id}"
    assert "declared, granted and audited" in \
        spec["channels"]["eventPlateRecognized"]["description"]
    assert "receiveVisitRecorded" in spec["operations"]


def test_a_scope_the_app_already_subscribes_to_is_not_duplicated():
    spec = contract_asyncapi(manifest(
        subscribes="opennvr.events.plate.recognized.v1.>",
        requires_scopes=["events:plate.recognized"]))
    addresses = [c["address"] for c in spec["channels"].values()]
    assert addresses.count("opennvr.events.plate.recognized.v1.>") == 1


def test_an_app_with_no_bus_surface_documents_no_channels():
    spec = contract_asyncapi(AppManifest(
        id="x", name="X", version="1.0.0", category="analytics"))
    assert spec["channels"] == {} and spec["operations"] == {}


# ── Served by the app itself ────────────────────────────────────────


def test_the_contract_server_serves_both_specs():
    import httpx

    app = App("served", name="Served", category="analytics", summary="s")

    @app.on_detection("person")
    def rule(event):
        event.alert("x")

    detector = app.build(app_config(contract_port=0), RecorderChannel().dispatcher())
    server = detector.start_contract_server()
    port = server.port
    try:
        base = f"http://127.0.0.1:{port}"
        openapi = httpx.get(f"{base}/openapi.json", trust_env=False).json()
        asyncapi = httpx.get(f"{base}/asyncapi.json", trust_env=False).json()
    finally:
        detector.stop_contract_server()

    assert openapi["openapi"] == "3.1.0"
    assert openapi["info"]["x-opennvr-app-id"] == "served"
    # The bound port is reflected back, so the document is usable as-is.
    assert openapi["servers"][0]["variables"]["port"]["default"] == str(port)
    assert asyncapi["asyncapi"] == "3.0.0"
    # No nulls survive to the wire.
    assert "null" not in json.dumps(openapi["info"])


# ── The `opennvr-app spec` command ──────────────────────────────────


SPEC_APP = '''
from opennvr_app_sdk import App

app = App("spec-demo", name="Spec Demo", category="analytics", summary="s")


@app.on_detection("person")
def rule(event):
    event.alert("x")
'''


def _write(tmp_path, source=SPEC_APP):
    app_dir = tmp_path / "spec_demo"
    app_dir.mkdir()
    (app_dir / "spec_demo.py").write_text(source)
    return app_dir


def test_spec_command_prints_openapi(tmp_path, capsys):
    from opennvr_app_sdk import scaffold

    assert scaffold.main(["spec", str(_write(tmp_path))]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["openapi"] == "3.1.0"
    assert document["info"]["x-opennvr-app-id"] == "spec-demo"


def test_spec_command_prints_asyncapi_as_yaml(tmp_path, capsys):
    import yaml

    from opennvr_app_sdk import scaffold

    assert scaffold.main(
        ["spec", str(_write(tmp_path)), "--format", "asyncapi", "--yaml"]) == 0
    document = yaml.safe_load(capsys.readouterr().out)
    assert document["asyncapi"] == "3.0.0"


def test_spec_command_writes_a_file(tmp_path, capsys):
    from opennvr_app_sdk import scaffold

    out = tmp_path / "generated" / "openapi.json"
    assert scaffold.main(["spec", str(_write(tmp_path)), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["openapi"] == "3.1.0"
    assert "wrote openapi" in capsys.readouterr().err


def test_spec_command_reports_a_missing_app(tmp_path, capsys):
    from opennvr_app_sdk import scaffold

    (tmp_path / "empty").mkdir()
    assert scaffold.main(["spec", str(tmp_path / "empty")]) == 2
    assert "no app module found" in capsys.readouterr().err


@requires_validator
def test_a_scaffolded_app_emits_a_valid_spec(tmp_path):
    from opennvr_app_sdk import scaffold

    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    out = tmp_path / "openapi.json"
    assert scaffold.main(["spec", str(app_dir), "-o", str(out)]) == 0
    validate_openapi(json.loads(out.read_text()))
