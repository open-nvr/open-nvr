# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The less-boilerplate trio: ``BaseAppConfig`` / ``load_app_config``,
the dispatcher on ``DomainEventSubscriber``, and the in-wheel
scaffolder (``opennvr-app new``) in both SDK modes — including that a
freshly scaffolded app's own smoke test passes."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from opennvr_app_sdk import (
    Alert, AlertDispatcher, AppManifest, BaseAppConfig, DomainEvent,
    DomainEventSubscriber, load_app_config,
)
from opennvr_app_sdk import scaffold
from opennvr_app_sdk._version import __version__

# ── config ─────────────────────────────────────────────────────────────


@dataclass
class _Cfg(BaseAppConfig):
    watch_labels: list[str] = field(default_factory=lambda: ["person"])
    dwell_s: float = 30.0
    zone: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.dwell_s <= 0:
            raise ValueError("'dwell_s' must be positive")


def test_load_app_config_fills_base_and_extra_fields(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("nats_url: nats://x:4222\nnats_token: tok\ncontract_port: '9210'\n"
                 "opennvr_url: http://core\nwatch_labels: [Car, TRUCK]\ndwell_s: 12\n")
    cfg = load_app_config(p, _Cfg)
    assert isinstance(cfg, _Cfg)
    assert cfg.nats_url == "nats://x:4222" and cfg.nats_token == "tok"
    assert cfg.contract_port == 9210 and cfg.opennvr_url == "http://core"
    assert cfg.subject_pattern is None and cfg.webhook_url is None
    assert cfg.nats_alerts_subject_prefix == "opennvr.alerts"
    assert cfg.watch_labels == ["Car", "TRUCK"] and cfg.dwell_s == 12 and cfg.zone == {}


def test_load_app_config_errors_are_operator_readable(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("watch_labels: [x]\n")
    with pytest.raises(ValueError, match="'nats_url' is required"):
        load_app_config(p, _Cfg)
    p.write_text("nats_url: nats://x\nsubject_pattern: '  '\n")
    with pytest.raises(ValueError, match="subject_pattern"):
        load_app_config(p, _Cfg)
    p.write_text("nats_url: nats://x\ncontract_port: eighty\n")
    with pytest.raises(ValueError, match="contract_port"):
        load_app_config(p, _Cfg)
    p.write_text("nats_url: nats://x\ndwell_s: -1\n")
    with pytest.raises(ValueError, match="dwell_s"):          # __post_init__ surfaces
        load_app_config(p, _Cfg)

    @dataclass
    class _Needs(BaseAppConfig):
        api_token: str | None = None

    @dataclass(kw_only=True)            # a required extra after the base defaults
    class _Required(BaseAppConfig):
        api_token: str

    p.write_text("nats_url: nats://x\n")
    assert load_app_config(p, _Needs).api_token is None
    with pytest.raises(ValueError, match="'api_token' is required"):
        load_app_config(p, _Required)
    assert load_app_config(p).__class__ is BaseAppConfig


# ── dispatcher on the event archetype ──────────────────────────────────


class _Rec:
    name = "rec"

    def __init__(self):
        self.alerts = []

    def send(self, a):
        self.alerts.append(a)
        return True


class _Gate(DomainEventSubscriber):
    manifest = AppManifest(id="gate", name="Gate", version="2.0.0", category="vehicle")
    subscriptions = ["plate.recognized.v1"]

    def on_event(self, event):
        self.fire(Alert(title="hi", description="", camera_id=event.camera_id))


def _cfg(**over):
    base = dict(nats_url="nats://x", nats_token=None, webhook_url=None,
                nats_alerts_url=None, nats_alerts_token=None, contract_port=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_event_subscriber_fires_as_the_app():
    rec = _Rec()
    app = _Gate(_cfg(), dispatcher=AlertDispatcher([rec]))
    ok = app._handle_raw(b'{"id":"e","schema":"plate.recognized.v1","camera_id":"cam2",'
                         b'"ts":"t","producer":"lpr","payload":{"plate_text":"X"}}', subject="s")
    assert ok and len(rec.alerts) == 1
    # Identity from the manifest, scoped around on_event like Detector.
    assert rec.alerts[0].source.name == "gate" and rec.alerts[0].source.version == "2.0.0"
    assert app.health_snapshot()["alerts_fired"] == 1
    # Built lazily from cfg when none is injected: stdout only here.
    lazy = _Gate(_cfg())
    assert [type(c).__name__ for c in lazy.dispatcher._channels] == ["StdoutChannel"]
    lazy2 = _Gate(_cfg(webhook_url="http://hook"))
    assert [type(c).__name__ for c in lazy2.dispatcher._channels] == ["StdoutChannel", "WebhookChannel"]


# ── scaffold ───────────────────────────────────────────────────────────


def _files(app_dir: Path) -> dict[str, str]:
    return {p.relative_to(app_dir).as_posix(): p.read_text()
            for p in app_dir.rglob("*") if p.is_file()}


def test_pypi_mode_is_the_default_and_self_contained(tmp_path):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    f = _files(app_dir)
    assert set(f) >= {"gate_watch.py", "pyproject.toml", "Dockerfile", "README.md",
                      "config.example.yml", "tests/test_smoke.py"}
    assert f'"opennvr-app-sdk>={__version__},<1.0"' in f["pyproject.toml"]
    assert "tool.uv.sources" not in f["pyproject.toml"]
    assert f'pip install --no-cache-dir "opennvr-app-sdk>={__version__},<1.0"' in f["Dockerfile"]
    assert "COPY gate_watch.py config.example.yml ./" in f["Dockerfile"]
    assert "COPY sdk/" not in f["Dockerfile"]
    assert scaffold.DOCS_URL + "FIRST_DETECTOR.md" in f["README.md"]
    import re
    leftovers = {k for k, v in f.items() if re.search(r"__[A-Z_]+__", v)}   # no token left
    assert not leftovers, leftovers
    assert "class GateWatch(Detector)" in f["gate_watch.py"]
    assert "load_app_config(path, AppConfig)" in f["gate_watch.py"]
    assert "__APP" not in f["gate_watch.py"]


def test_path_mode_inside_a_checkout(tmp_path):
    repo = tmp_path / "open-nvr"
    (repo / "sdk" / "opennvr-app-sdk").mkdir(parents=True)
    app_dir = scaffold.generate("gate-watch", "object_detection", repo / "examples",
                                repo_root=repo)                        # auto → path
    f = _files(app_dir)
    assert 'opennvr-app-sdk = { path = "../../sdk/opennvr-app-sdk", editable = true }' in f["pyproject.toml"]
    assert "COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk" in f["Dockerfile"]
    assert "COPY examples/gate-watch/gate_watch.py" in f["Dockerfile"]
    assert "](../../docs/FIRST_DETECTOR.md)" in f["README.md"]
    # Explicit overrides, and the guard rails.
    out = scaffold.generate("a-one", "object_detection", tmp_path / "elsewhere",
                            sdk="path", repo_root=repo)
    assert "editable = true" in _files(out)["pyproject.toml"]
    with pytest.raises(ValueError, match="repo_root"):
        scaffold.generate("a-two", "object_detection", tmp_path, sdk="path")
    with pytest.raises(ValueError):
        scaffold.generate("Bad_Id", "object_detection", tmp_path)
    with pytest.raises(FileExistsError):
        scaffold.generate("gate-watch", "object_detection", repo / "examples", repo_root=repo)


def test_scaffolded_app_smoke_test_passes(tmp_path):
    """The generated app must be green out of the box, against THIS
    SDK (the app's own tests import it; we run them in-process)."""
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(app_dir / "tests")],
        cwd=app_dir, capture_output=True, text=True,
        env={"PYTHONPATH": f"{app_dir}{__import__('os').pathsep}{Path(scaffold.__file__).parents[1]}",
             "PATH": __import__('os').environ.get("PATH", "")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "5 passed" in proc.stdout


def test_cli_new(tmp_path, capsys):
    rc = scaffold.main(["new", "gate-watch", "--dest", str(tmp_path)])
    assert rc == 0 and (tmp_path / "gate-watch" / "gate_watch.py").exists()
    assert "opennvr-app-sdk from PyPI" in capsys.readouterr().out
    assert scaffold.main(["new", "gate-watch", "--dest", str(tmp_path)]) == 2   # exists
