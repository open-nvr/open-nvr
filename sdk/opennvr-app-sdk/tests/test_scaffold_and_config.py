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


def test_a_config_without_nats_url_uses_what_the_deployment_exports(tmp_path, monkeypatch):
    """The app installer already exports NATS_URL / OPENNVR_URL /
    OPENNVR_INTERNAL_API_KEY into every app container, so a config.yml
    that names none of them is complete — and an explicit value in the
    file still wins."""
    from opennvr_app_sdk.config import DEFAULT_NATS_URL

    p = tmp_path / "c.yml"
    p.write_text("watch_labels: [x]\n")

    monkeypatch.delenv("NATS_URL", raising=False)
    monkeypatch.delenv("OPENNVR_INTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENNVR_URL", raising=False)
    bare = load_app_config(p)
    assert bare.nats_url == DEFAULT_NATS_URL
    assert bare.nats_token is None

    monkeypatch.setenv("NATS_URL", "nats://elsewhere:4222")
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "k3y")
    monkeypatch.setenv("OPENNVR_URL", "http://core:8000")
    from_env = load_app_config(p)
    assert from_env.nats_url == "nats://elsewhere:4222"
    assert from_env.nats_token == "k3y"
    assert from_env.opennvr_token == "k3y"
    assert from_env.opennvr_url == "http://core:8000"

    (tmp_path / "explicit.yml").write_text("nats_url: nats://in-the-file:4222\n")
    assert load_app_config(tmp_path / "explicit.yml").nats_url \
        == "nats://in-the-file:4222"


def test_an_empty_nats_url_is_still_an_error(tmp_path):
    """Omitting the key means "use the deployment's"; writing an empty
    one is a typo, and typos should be loud."""
    p = tmp_path / "c.yml"
    p.write_text("nats_url: '   '\n")
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
    assert 'App(\n    "gate-watch"' in f["gate_watch.py"]
    assert "@app.on_detection(" in f["gate_watch.py"]
    assert "app.param(" in f["gate_watch.py"]
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
    assert "6 passed" in proc.stdout


def test_cli_new(tmp_path, capsys):
    rc = scaffold.main(["new", "gate-watch", "--dest", str(tmp_path)])
    assert rc == 0 and (tmp_path / "gate-watch" / "gate_watch.py").exists()
    assert "opennvr-app-sdk from PyPI" in capsys.readouterr().out
    assert scaffold.main(["new", "gate-watch", "--dest", str(tmp_path)]) == 2   # exists


def test_param_suggestions_ride_the_manifest():
    from opennvr_app_sdk import DETECTION_LABELS, AppManifest, Param

    m = AppManifest(id="a", name="A", version="1.0.0", category="analytics",
                    params=[Param("watch_labels", list, default=["person"],
                                  suggestions=["person", "car"]),
                            Param("dwell_s", float, default=30.0)])
    ps = {p["name"]: p for p in m.to_dict()["params"]}
    assert ps["watch_labels"]["suggestions"] == ["person", "car"]
    assert "suggestions" not in ps["dwell_s"]          # absent when empty (wire unchanged)
    assert "person" in DETECTION_LABELS and "car" in DETECTION_LABELS


def test_repo_mode_lays_down_the_repository_files(tmp_path):
    """``--repo``: the app folder becomes a repository for open-nvr/app-<id>
    — CI, the publish workflow that builds + signs through the org's
    build-catalog-app, the listing entry — and pins the published SDK."""
    import yaml

    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path, repo=True)
    f = _files(app_dir)
    assert {".github/workflows/ci.yml", ".github/workflows/publish.yml", "apps-index-entry.yml",
            ".gitignore", ".dockerignore"} <= set(f)
    pub = yaml.safe_load(f[".github/workflows/publish.yml"])
    job = pub["jobs"]["publish"]
    assert job["uses"] == "open-nvr/open-nvr/.github/workflows/build-catalog-app.yml@main"
    assert job["with"] == {"app_id": "gate-watch", "title": "Gate Watch"}
    assert job["permissions"] == {"contents": "read", "packages": "write", "id-token": "write"}
    assert pub[True]["push"]["tags"] == ["v*"]
    entry = yaml.safe_load(f["apps-index-entry.yml"])[0]
    assert entry["id"] == "gate-watch" and entry["image"] == "ghcr.io/open-nvr/gate-watch:latest"
    assert entry["source"] == "https://github.com/open-nvr/app-gate-watch"
    assert entry["network_egress"] == [] and entry["requires_tasks"] == ["object_detection"]
    assert "${GATE_WATCH_IMAGE:-ghcr.io/open-nvr/gate-watch:latest}" in entry["install"]["compose"]
    assert "opennvr_apps" in entry["install"]["compose"]
    assert "image_digest" not in entry                      # comes from the publish summary
    assert "## This repository" in f["README.md"] and "open-nvr/app-gate-watch" in f["README.md"]
    assert f'"opennvr-app-sdk>={__version__},<1.0"' in f["pyproject.toml"]   # pypi pin
    assert "tool.uv.sources" not in f["pyproject.toml"]
    assert "app.key" in f[".gitignore"] and "config.yml" in f[".gitignore"]
    # Without --repo none of that appears, and the README has no repo section.
    plain = _files(scaffold.generate("gate-two", "object_detection", tmp_path))
    assert not any(k.startswith(".github") or k == "apps-index-entry.yml" for k in plain)
    assert "## This repository" not in plain["README.md"] and not plain["README.md"].endswith("\n\n")
    with pytest.raises(ValueError, match="standalone"):
        scaffold.generate("gate-three", "object_detection", tmp_path, sdk="path", repo=True, repo_root=tmp_path)


def test_cli_new_repo(tmp_path, capsys):
    assert scaffold.main(["new", "gate-watch", "--dest", str(tmp_path), "--repo"]) == 0
    assert (tmp_path / "gate-watch" / ".github" / "workflows" / "publish.yml").exists()
    assert "open-nvr/app-gate-watch" in capsys.readouterr().out
