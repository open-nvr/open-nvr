# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""AppManifest / Param / AlertType — the future GET /manifest payload."""
from __future__ import annotations

from opennvr_app_sdk.manifest import AlertType, AppManifest, Param


def _manifest() -> AppManifest:
    return AppManifest(
        id="loitering-detection",
        name="Loitering Detection",
        version="1.0.0",
        category="perimeter",
        summary="Alerts when a watched object dwells in a zone.",
        requires_tasks=["object_detection"],
        subscribes="opennvr.inference.>",
        params=[
            Param("watch_labels", list, default=["person"]),
            Param("threshold_seconds", float, default=30.0),
            Param("zones", "geometry.polygon", per_camera=True,
                  description="Drawn in the catalog UI."),
        ],
        emits=[AlertType("loitering", severity="high")],
    )


def test_to_dict_shape():
    d = _manifest().to_dict()
    assert d["id"] == "loitering-detection"
    assert d["category"] == "perimeter"
    assert d["requires_tasks"] == ["object_detection"]
    assert d["subscribes"] == "opennvr.inference.>"
    assert len(d["params"]) == 3
    assert d["emits"] == [
        {"name": "loitering", "severity": "high", "description": ""},
    ]


def test_param_python_types_render_by_name():
    d = _manifest().to_dict()
    by_name = {p["name"]: p for p in d["params"]}
    assert by_name["watch_labels"]["type"] == "list"
    assert by_name["threshold_seconds"]["type"] == "float"
    assert by_name["watch_labels"]["default"] == ["person"]
    assert by_name["watch_labels"]["per_camera"] is False


def test_param_ui_schema_types_pass_through():
    d = _manifest().to_dict()
    zones = next(p for p in d["params"] if p["name"] == "zones")
    assert zones["type"] == "geometry.polygon"
    assert zones["per_camera"] is True


def test_manifest_defaults():
    m = AppManifest(id="x", name="X", version="0.1", category="test")
    d = m.to_dict()
    assert d["summary"] == ""
    assert d["requires_tasks"] == []
    assert d["subscribes"] is None
    assert d["params"] == []
    assert d["emits"] == []


def test_to_dict_is_json_serializable():
    import json
    json.dumps(_manifest().to_dict())  # must not raise


def test_alert_type_default_severity_is_medium():
    assert AlertType("thing").severity == "medium"


def test_state_view_serializes_and_defaults_empty():
    from opennvr_app_sdk import AppManifest, StateView

    # Absent → empty list on the wire (older catalogs just ignore it).
    bare = AppManifest(id="x", name="X", version="1", category="test")
    assert bare.to_dict()["state_schema"] == []

    m = AppManifest(
        id="x", name="X", version="1", category="test",
        state_schema=[
            StateView(name="n", label="N", kind="metric", path="a.b"),
            StateView(name="t", label="T", kind="table", path="rows",
                      columns=["id", "count"], description="d"),
        ],
    )
    wire = m.to_dict()["state_schema"]
    assert wire[0] == {
        "name": "n", "label": "N", "kind": "metric", "path": "a.b",
        "columns": [], "description": "",
    }
    assert wire[1]["columns"] == ["id", "count"]


# ── App-UI mode + store listing ─────────────────────────────────────


def test_ui_and_listing_defaults():
    d = AppManifest(id="x", name="X", version="1", category="test").to_dict()
    assert d["ui_mode"] == "internal"
    assert d["ui_url"] == ""
    assert d["description"] == ""
    assert d["author"] == ""
    assert d["website"] == ""
    assert d["license"] == ""
    assert d["use_cases"] == []
    assert d["contact"] == ""


def test_external_ui_mode_serializes():
    m = AppManifest(
        id="x", name="X", version="1", category="test",
        ui_mode="external", ui_url="http://{host}:8090/",
        description="Long form.\n\nSecond paragraph.",
        author="Acme", website="https://acme.example", license="MIT",
        use_cases=["Alarm on unregistered vehicles", "Gate history"],
    )
    d = m.to_dict()
    assert d["ui_mode"] == "external"
    assert d["ui_url"] == "http://{host}:8090/"
    assert d["use_cases"] == ["Alarm on unregistered vehicles", "Gate history"]
    import json
    json.dumps(d)  # listing fields must stay JSON-serializable


def test_unknown_ui_mode_rejected():
    import pytest

    with pytest.raises(ValueError, match="ui_mode"):
        AppManifest(id="x", name="X", version="1", category="test",
                    ui_mode="popup")


def test_external_without_url_rejected():
    import pytest

    with pytest.raises(ValueError, match="ui_url"):
        AppManifest(id="x", name="X", version="1", category="test",
                    ui_mode="external")
