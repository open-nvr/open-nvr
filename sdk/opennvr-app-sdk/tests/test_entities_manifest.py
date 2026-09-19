# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``entities:`` in the manifest (HA-114): serialisation and validation."""
from __future__ import annotations

from opennvr_app_sdk import Action, AlertType, AppManifest, Entity, Param
from opennvr_app_sdk.validate import Report, check_manifest


def _report(entities, actions=None) -> Report:
    m = AppManifest(id="gate-watch", name="Gate Watch", version="1.0.0", category="perimeter",
                    summary="Watches gates.", requires_tasks=["object_detection"],
                    emits=[AlertType("gate_watch")],
                    actions=actions if actions is not None else [
                        Action("resolve", "Resolve", params=[Param("camera", str, default="")])],
                    entities=entities)
    r = Report()
    check_manifest(m, r)
    return r


def test_to_dict_is_compact_and_in_the_manifest():
    e = Entity("unattended", "sensor", "Unattended now", state_path="unattended_now",
               state_class="measurement")
    assert e.to_dict() == {"key": "unattended", "platform": "sensor", "name": "Unattended now",
                           "per_camera": False, "enabled_default": True,
                           "state_path": "unattended_now", "state_class": "measurement"}
    m = AppManifest(id="a", name="A", version="1.0.0", category="perimeter", entities=[e])
    assert m.to_dict()["entities"] == [e.to_dict()]
    assert AppManifest(id="a", name="A", version="1.0.0",
                       category="perimeter").to_dict()["entities"] == []


def test_valid_entities_pass():
    r = _report([
        Entity("unattended", "sensor", "Unattended", state_path="unattended_now"),
        Entity("cam_unattended", "sensor", "Unattended here", per_camera=True,
               state_path="per_camera[camera={camera}].unattended"),
        Entity("resolve", "button", "Resolve", per_camera=True, action="resolve"),
        Entity("abandoned", "event", "Abandoned", event_types=["abandoned"]),
    ])
    assert r.ok, r.errors


def test_bad_entities_are_errors():
    def errs(*entities, actions=None):
        return _report(list(entities), actions).errors

    assert any("snake_case" in e for e in errs(Entity("Bad-Key", "sensor", "x", state_path="a")))
    assert any("declared twice" in e for e in errs(
        Entity("a", "sensor", "x", state_path="a"), Entity("a", "sensor", "y", state_path="b")))
    assert any("unknown platform" in e for e in errs(Entity("a", "lock", "x")))
    assert any("needs action" in e for e in errs(Entity("a", "button", "x")))
    assert any("not one of the app's actions" in e
               for e in errs(Entity("a", "button", "x", action="launch")))
    assert any("needs state_path" in e for e in errs(Entity("a", "sensor", "x")))
    assert any("per_camera=True" in e
               for e in errs(Entity("a", "sensor", "x", state_path="rows[camera={camera}].n")))
