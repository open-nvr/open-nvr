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
    assert not any("category" in w for w in _report(category="fashion").warnings)
    assert any("canonical task" in w for w in _report(requires_tasks=["mind_reading"]).warnings)
    assert any("requires_scopes" in e for e in _report(requires_scopes=["plates"]).errors)
    assert any("declared twice" in e for e in _report(emits=[AlertType("a"), AlertType("a")]).errors)
    assert any("severity" in e for e in _report(emits=[AlertType("a", severity="loud")]).errors)
    assert any("price_note" in w for w in _report(pricing="paid").warnings)
    assert any("entitlement='none'" in w for w in _report(pricing="paid", price_note="$1").warnings)
    assert any("declared twice" in e for e in _report(actions=[Action("go", "Go"), Action("go", "Go")]).errors)
    assert any("ui_url" in e for e in _report(has_ui=True, ui_mode="external", ui_url="http://{host}:1/").errors) is False


def test_tier0_labels_checks():
    """Tier-0 matches labels lowercased, so a capitalised or padded label
    would widen nothing — exactly the silent failure the field exists to
    prevent. Duplicates are harmless, so only a warning."""
    assert _report(tier0_labels=["backpack", "suitcase"]).ok
    assert any("lowercase" in e for e in _report(tier0_labels=["Backpack"]).errors)
    assert any("lowercase" in e for e in _report(tier0_labels=[" suitcase"]).errors)
    assert any("non-empty string" in e for e in _report(tier0_labels=[""]).errors)
    dup = _report(tier0_labels=["backpack", "backpack"])
    assert dup.ok and any("listed twice" in w for w in dup.warnings)


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


# ── the platform rules (docs/BUILDING_EFFICIENT_APPS.md) ────────────


def _codes(source: str) -> list[str]:
    from opennvr_app_sdk.validate import scan_source

    return [code for code, _line, _what in scan_source(source)]


def test_the_source_scan_finds_what_the_guide_forbids():
    assert _codes("import cv2\ncap = cv2.VideoCapture(url)\n") == ["own-decode"]
    assert _codes('URL = "rtsp://10.0.0.5/stream1"\n') == ["own-decode"]
    assert _codes('import subprocess\nsubprocess.run(["ffmpeg", "-i", src])\n') == ["own-decode"]
    assert _codes("from ultralytics import YOLO\n") == ["own-model"]
    assert _codes("import onnxruntime as ort\n") == ["own-model"]
    assert _codes('url = base.rstrip("/") + "/infer"\n') == ["direct-adapter"]
    assert _codes('r = httpx.post("http://yolov8:9002/infer", content=jpeg)\n') == ["direct-adapter"]
    assert _codes('SUBJECT = "opennvr.inference.>"\n') == ["wide-subject"]
    assert _codes("s = nvr.ai.stream('yolov8', camera_id='cam1')\n") == ["per-frame-model"]
    per_frame = (
        "class Watch(FrameApp):\n"
        "    def on_frame(self, camera_id, jpeg):\n"
        "        return self.nvr.ai.infer('blip', jpeg, task='image_captioning')\n")
    assert _codes(per_frame) == ["per-frame-model"]


def test_the_source_scan_leaves_the_right_way_alone():
    """KAI-C's own route, the narrow subject, a model call on an event,
    and prose that merely mentions the words."""
    assert _codes('url = f"{kaic}/api/v1/infer/{adapter}"\n') == []
    assert _codes('SUBJECT = "opennvr.inference.tier0.>"\n') == []
    assert _codes('"""Never open rtsp:// yourself; ffmpeg is the platform\'s job."""\n') == []
    assert _codes("def f():\n    '''opennvr.inference.>'''\n") == []
    on_event = (
        "class Gate(DomainEventSubscriber):\n"
        "    def on_event(self, event):\n"
        "        return self.nvr.ai.infer('blip', self.nvr.timeline.evidence(1), task='vqa')\n")
    assert _codes(on_event) == []
    assert _codes("def broken(:\n") == []          # unparsable source is not this check's finding


def _app_with(tmp_path: Path, extra_source: str) -> Path:
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    module = app_dir / "gate_watch.py"
    module.write_text(module.read_text() + "\n" + extra_source)
    return app_dir


def test_a_finding_is_a_warning_and_strict_fails_on_it(tmp_path: Path, capsys):
    app_dir = _app_with(tmp_path, 'def grab(url):\n    import cv2\n    return cv2.VideoCapture(url)\n')
    report = validate_app(app_dir)
    assert report.ok, report.errors
    assert len(report.rule_findings) == 1 and "[own-decode]" in report.rule_findings[0]
    assert "gate_watch.py:" in report.rule_findings[0] and "rule 1" in report.rule_findings[0]
    assert report.rule_findings[0] in report.warnings
    assert scaffold.main(["validate", str(app_dir)]) == 0
    assert scaffold.main(["validate", "--strict", str(app_dir)]) == 1
    assert "--strict: 1 platform-rule finding" in capsys.readouterr().out


def test_a_waiver_needs_its_reason(tmp_path: Path):
    waived = _app_with(
        tmp_path / "a",
        "def grab(path):\n    import cv2\n"
        "    # opennvr: allow[own-decode] reads a file the operator uploaded, not a camera\n"
        "    return cv2.VideoCapture(path)\n")
    report = validate_app(waived)
    assert report.rule_findings == []
    assert any("waived" in n and "uploaded" in n for n in report.notes)
    assert scaffold.main(["validate", "--strict", str(waived)]) == 0

    bare = _app_with(
        tmp_path / "b",
        "def grab(path):\n    import cv2\n"
        "    return cv2.VideoCapture(path)  # opennvr: allow[own-decode]\n")
    report = validate_app(bare)
    assert len(report.rule_findings) == 1 and "gives no reason" in report.rule_findings[0]

    wrong_rule = _app_with(
        tmp_path / "c",
        "def grab(path):\n    import cv2\n"
        "    return cv2.VideoCapture(path)  # opennvr: allow[own-model] not this rule\n")
    assert len(validate_app(wrong_rule).rule_findings) == 1


def test_tests_and_tooling_are_not_the_app(tmp_path: Path):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    (app_dir / "tests" / "test_video.py").write_text("import cv2\ncv2.VideoCapture('x.mp4')\n")
    (app_dir / "tools").mkdir()
    (app_dir / "tools" / "eval.py").write_text("import torch\n")
    assert validate_app(app_dir).rule_findings == []


def test_the_wide_subject_in_the_example_config(tmp_path: Path):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    cfg = app_dir / "config.example.yml"
    cfg.write_text(cfg.read_text() + '\nsubject_pattern: "opennvr.inference.>"\n')
    report = validate_app(app_dir)
    assert any("config.example.yml: [wide-subject]" in f for f in report.rule_findings)
    # An app that does not consume Tier-0 sees each object once on the wide subject.
    cfg.write_text(cfg.read_text() + "consume_tier0: false\n")
    assert validate_app(app_dir).rule_findings == []


def test_every_rule_code_points_at_a_rule_in_the_guide():
    """The validator cites rule numbers; the guide must have them."""
    import re

    from opennvr_app_sdk.validate import PLATFORM_RULES

    guide = Path(__file__).resolve().parents[3] / "docs" / "BUILDING_EFFICIENT_APPS.md"
    if not guide.exists():          # an sdist has no docs/
        import pytest

        pytest.skip("docs/ is not part of this checkout")
    numbers = set(re.findall(r"^## Rule (\d+)\b", guide.read_text(encoding="utf-8"), flags=re.M))
    for code, (number, _advice) in PLATFORM_RULES.items():
        assert number in numbers, f"{code} cites rule {number}, which the guide does not have"
        assert f"`{code}`" in guide.read_text(encoding="utf-8"), f"the guide never names the {code} check"


def test_an_app_that_cannot_be_imported_is_still_read(tmp_path: Path):
    """The rules are read from the source, so a missing dependency in
    the validating environment does not hide them."""
    app_dir = _app_with(tmp_path, "import a_module_nobody_has\nfrom ultralytics import YOLO\n")
    report = validate_app(app_dir)
    assert any("importing" in e for e in report.errors)
    assert any("[own-model]" in f for f in report.rule_findings)
