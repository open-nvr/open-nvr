# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app validate`` — the manifest, config, listing and repository checks."""
from __future__ import annotations

from pathlib import Path

from opennvr_app_sdk import Action, AlertType, AppManifest, Param, scaffold
from opennvr_app_sdk.validate import Report, check_manifest, find_app_module, validate_app


def _report(**kw) -> Report:
    base = dict(id="gate-watch", name="Gate Watch", version="1.0.0", category="perimeter",
                summary="Watches gates.", requires_tasks=["object_detection"],
                emits=[AlertType("gate_watch")])
    cls = kw.pop("_cls", None)
    base.update(kw)
    r = Report()
    check_manifest(AppManifest(**base), r, cls)
    return r


def test_manifest_checks():
    assert _report().ok and _report().warnings == []
    assert any("kebab" in e for e in _report(id="Gate_Watch").errors)
    assert any("semantic" in e for e in _report(version="1.0").errors)
    assert any("category" in w for w in _report(category="misc").warnings)
    assert any("canonical task" in w for w in _report(requires_tasks=["mind_reading"]).warnings)
    assert any("requires_scopes" in e for e in _report(requires_scopes=["plates"]).errors)
    assert any("declared twice" in e for e in _report(emits=[AlertType("a"), AlertType("a")]).errors)
    assert any("severity" in e for e in _report(emits=[AlertType("a", severity="loud")]).errors)
    assert any("price_note" in w for w in _report(pricing="paid").warnings)
    assert any("entitlement='none'" in w for w in _report(pricing="paid", price_note="$1").warnings)
    assert any("declared twice" in e for e in _report(actions=[Action("go", "Go"), Action("go", "Go")]).errors)
    assert any("ui_url" in e for e in _report(has_ui=True, ui_mode="external", ui_url="http://{host}:1/").errors) is False


def test_param_checks():
    ok = _report(params=[Param("watch_labels", list, default=["person"], suggestions=["car"]),
                         Param("zone", "geometry.polygon", default=[], per_camera=True),
                         Param("dwell_s", float, default=30)])
    assert ok.ok and ok.warnings == []
    r = _report(params=[Param("Watch", str), Param("dwell_s", int, default=True),
                        Param("dwell_s", int), Param("odd", "colour"),
                        Param("must", str, required=True, default="x"),
                        Param("labels", list, suggestions=[1])])
    errs = "\n".join(r.errors)
    assert "snake_case" in errs and "not a int" in errs and "declared twice" in errs
    assert "suggestions must be strings" in errs
    warns = "\n".join(r.warnings)
    assert "'colour'" in warns and "redundant" in warns


def test_license_hook_must_be_overridden():
    from opennvr_app_sdk import Detector

    class Paid(Detector):
        pass

    class PaidRight(Detector):
        def verify_license(self, key):  # noqa: ARG002
            return None

    assert any("verify_license" in e
               for e in _report(pricing="paid", price_note="$1", entitlement="license_key", _cls=Paid).errors)
    assert _report(pricing="paid", price_note="$1", entitlement="license_key", _cls=PaidRight).ok


def test_validate_a_scaffolded_app(tmp_path: Path, capsys):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path, repo=True)
    assert find_app_module(app_dir) == "gate_watch"
    report = validate_app(app_dir)
    assert report.manifest is not None and report.manifest.id == "gate-watch"
    # The scaffold is valid except for the listing placeholders the author must fill.
    assert [e for e in report.errors if "placeholder" not in e] == [], report.errors
    assert any("listing.author" in e for e in report.errors)
    assert any("no LICENSE" in w for w in report.warnings)
    assert any("config.example.yml loads" in n for n in report.notes)
    # Fill the placeholders → clean.
    entry = (app_dir / "apps-index-entry.yml").read_text()
    entry = entry.replace('"Your Name"', '"Ada"').replace('"you@example.com"', '"ada@arkade.ai"') \
                 .replace('"What Gate Watch does, in one operator-facing sentence."', '"Watches gates."')
    (app_dir / "apps-index-entry.yml").write_text(entry)
    (app_dir / "LICENSE").write_text("Apache-2.0")
    report = validate_app(app_dir)
    assert report.ok and report.warnings == [], (report.errors, report.warnings)
    # A drifted listing is caught.
    (app_dir / "apps-index-entry.yml").write_text(entry.replace('version: "0.1.0"', 'version: "9.9.9"')
                                                  .replace("emits: [gate-watch]", "emits: [other]"))
    report = validate_app(app_dir)
    assert any("listing.version" in e for e in report.errors) and any("listing.emits" in e for e in report.errors)
    # The CLI.
    assert scaffold.main(["validate", str(app_dir)]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "listing.version" in out
    assert scaffold.main(["validate", str(tmp_path / "nowhere")]) == 1


def test_validate_without_a_module(tmp_path: Path):
    (tmp_path / "README.md").write_text("nothing here")
    report = validate_app(tmp_path)
    assert not report.ok and "no app module" in report.errors[0]
