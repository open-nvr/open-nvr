# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app dev`` — the app running against a simulated camera.

The point of the command is that a rule can be seen firing before any
broker, adapter or container exists, so these tests assert on what the
author actually reads: the events, the zone annotations, and the alerts.
"""
from __future__ import annotations

from pathlib import Path

from opennvr_app_sdk import scaffold
from opennvr_app_sdk.dev import run_dev

FACADE_APP = '''
from opennvr_app_sdk import App

app = App("zone-test", name="Zone Test", category="perimeter", summary="s")


@app.on_detection("person", zone="driveway", dwell=3, severity="high")
def loitering(event):
    event.alert(f"Person loitering on {event.camera}")
'''

FACADE_CONFIG = """
nats_url: "nats://dev:4222"
driveway:
  cam-1: [[0.3, 0.0], [0.8, 0.0], [0.8, 1.0], [0.3, 1.0]]
"""

LEGACY_APP = '''
from opennvr_app_sdk import Alert, AppManifest, BaseAppConfig, Detector

MANIFEST = AppManifest(id="legacy-app", name="Legacy App", version="1.0.0",
                       category="analytics", summary="s",
                       subscribes="opennvr.inference.>")


class AppConfig(BaseAppConfig):
    pass


class LegacyApp(Detector):
    manifest = MANIFEST

    def on_detections(self, camera_id, detections, event):
        return [Alert(title="legacy fired", description="d", camera_id=camera_id)]
'''

#: The shape `validate` has always accepted and `dev` used to reject:
#: the manifest lives ONLY as a class attribute.
CLASS_ONLY_APP = '''
from opennvr_app_sdk import Alert, AppManifest, Detector


class ClassOnly(Detector):
    manifest = AppManifest(id="class-only", name="Class Only", version="1.0.0",
                           category="analytics", summary="s",
                           subscribes="opennvr.inference.>")

    def on_detections(self, camera_id, detections, event):
        return [Alert(title="class-only fired", description="d",
                      camera_id=camera_id)]
'''


def write_app(tmp_path: Path, module: str, source: str,
              config: str | None = None) -> Path:
    app_dir = tmp_path / module
    app_dir.mkdir()
    (app_dir / f"{module}.py").write_text(source)
    if config is not None:
        (app_dir / "config.example.yml").write_text(config)
    return app_dir


# ── The happy path ──────────────────────────────────────────────────


def test_dev_run_reports_zone_entry_and_fires_once(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP, FACADE_CONFIG)
    assert run_dev(app_dir, fast=True, count=12) == 0
    out = capsys.readouterr().out

    assert "opennvr-app dev — zone-test 0.1.0 (perimeter)" in out
    assert "zones: driveway" in out
    # The walk crosses into the zone partway through, not at the start.
    assert "t=   0.0s  person  conf 0.80  at (0.05, 0.50)\n" in out
    assert "in driveway" in out
    # dwell=3 fires once per presence episode, at HIGH — the rule's severity.
    assert out.count("ALERT [HIGH] Person loitering on cam-1") == 1
    assert "1 alert(s) fired over 12 event(s)." in out
    # Evidence the facade filled in for free. The walk enters the zone at
    # t=4, so dwell is measured from THERE, not from when the person
    # appeared on camera at t=0 — "loitering in the driveway for 3s".
    assert "zone=driveway" in out and "dwell_s=3.0" in out
    assert "t=   7.0s  ALERT" in out


def test_still_parks_the_object_in_the_centre(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP, FACADE_CONFIG)
    run_dev(app_dir, fast=True, count=5, still=True)
    out = capsys.readouterr().out
    assert out.count("at (0.50, 0.50)") == 5


def test_label_and_camera_are_overridable(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP, FACADE_CONFIG)
    run_dev(app_dir, fast=True, count=3, label="car", camera="cam-gate")
    out = capsys.readouterr().out
    assert "camera cam-gate · car walking" in out
    # The rule watches people, so a car fires nothing.
    assert "0 alert(s) fired" in out


def test_runs_without_a_config_file(tmp_path, capsys):
    """No config.example.yml ⇒ generated defaults, not a crash — and a
    stand-in polygon, so the zone rule the app is built around can
    actually be seen firing."""
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP)
    assert run_dev(app_dir, fast=True, count=12) == 0
    out = capsys.readouterr().out
    assert "drew a stand-in polygon" in out
    assert "zones: driveway" in out
    assert "1 alert(s) fired over 12 event(s)." in out


def test_no_zones_leaves_an_undrawn_zone_undrawn(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP)
    assert run_dev(app_dir, fast=True, count=12, no_zones=True) == 0
    out = capsys.readouterr().out
    assert "drew a stand-in polygon" not in out
    assert "0 alert(s) fired over 12 event(s)." in out


def test_a_scaffolded_app_runs_out_of_the_box(tmp_path, capsys):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    assert run_dev(app_dir, fast=True, count=3) == 0
    out = capsys.readouterr().out
    assert "opennvr-app dev — gate-watch 0.1.0" in out
    assert out.count("ALERT [MEDIUM] Person seen on cam-1") == 3


# ── The pre-facade path still works ─────────────────────────────────


def test_dev_runs_a_plain_detector_app(tmp_path, capsys):
    app_dir = write_app(tmp_path, "legacy_app", LEGACY_APP)
    assert run_dev(app_dir, fast=True, count=2) == 0
    out = capsys.readouterr().out
    assert "opennvr-app dev — legacy-app 1.0.0 (analytics)" in out
    assert out.count("ALERT [HIGH] legacy fired") == 2


# ── Failure modes ───────────────────────────────────────────────────


def test_missing_app_is_an_error_not_a_traceback(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert run_dev(tmp_path / "empty", fast=True) == 2
    assert "no app module found" in capsys.readouterr().err


def test_import_failure_is_reported(tmp_path, capsys):
    app_dir = write_app(
        tmp_path, "broken",
        'from opennvr_app_sdk import App\napp = App("broken")\n1 / 0\n')
    assert run_dev(app_dir, fast=True) == 2
    assert "importing 'broken' failed: ZeroDivisionError" in capsys.readouterr().err


def test_dev_runs_an_app_whose_manifest_is_only_a_class_attribute(tmp_path, capsys):
    """`validate` accepts this shape, so `dev` must too — the two
    commands disagreeing about the same app is its own bug."""
    app_dir = write_app(tmp_path, "class_only", CLASS_ONLY_APP)
    assert run_dev(app_dir, fast=True, count=2) == 0
    out = capsys.readouterr().out
    assert "opennvr-app dev — class-only 1.0.0" in out
    assert out.count("ALERT [HIGH] class-only fired") == 2


def test_bad_config_is_reported(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP, "nats_url: '   '\n")
    assert run_dev(app_dir, fast=True) == 2
    assert "nats_url" in capsys.readouterr().err


# ── CLI wiring ──────────────────────────────────────────────────────


def test_cli_exposes_dev(tmp_path, capsys):
    app_dir = write_app(tmp_path, "zone_test", FACADE_APP, FACADE_CONFIG)
    code = scaffold.main(["dev", str(app_dir), "--fast", "--count", "2"])
    assert code == 0
    assert "opennvr-app dev — zone-test" in capsys.readouterr().out
