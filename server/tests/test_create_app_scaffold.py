# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""scripts/create_opennvr_app.py — the two shapes it must produce.

In-tree (``examples/<id>``) the app tracks this checkout's SDK through
an editable path, so the examples never drift from main. Out of tree —
a third-party developer's own repository — there is no checkout to
point at: the app must pin the published ``opennvr-app-sdk`` and its
Dockerfile must build from PyPI alone. The outside-the-repo walk
(docs/EXTERNAL_APP_WALKTHROUGH.md) found the second shape missing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "create_opennvr_app", REPO_ROOT / "scripts" / "create_opennvr_app.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules["create_opennvr_app"] = gen
_spec.loader.exec_module(gen)


def _files(app_dir: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in app_dir.iterdir() if p.is_file()}


def test_out_of_tree_pins_pypi_and_builds_without_a_checkout(tmp_path):
    app_dir = gen.generate("gate-watch", "object_detection", tmp_path)   # auto → pypi
    f = _files(app_dir)
    version = gen.sdk_version()
    assert f'"opennvr-app-sdk>={version},<1.0"' in f["pyproject.toml"]
    assert "tool.uv.sources" not in f["pyproject.toml"]
    assert "editable" not in f["pyproject.toml"]
    assert f'pip install --no-cache-dir "opennvr-app-sdk>={version},<1.0"' in f["Dockerfile"]
    assert "COPY sdk/" not in f["Dockerfile"] and "examples/" not in f["Dockerfile"]
    assert "COPY gate_watch.py config.example.yml ./" in f["Dockerfile"]
    # The app itself is the same either way.
    assert "class GateWatch(Detector)" in f["gate_watch.py"]


def test_in_tree_keeps_the_editable_path(tmp_path, monkeypatch):
    # Simulate examples/<id> by pointing the generator's notion of the
    # repo root at tmp_path and scaffolding under it.
    monkeypatch.setattr(gen, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "_SDK_DIR", tmp_path / "sdk" / "opennvr-app-sdk")
    app_dir = gen.generate("gate-watch", "object_detection", tmp_path / "examples")
    f = _files(app_dir)
    assert 'opennvr-app-sdk = { path = "../../sdk/opennvr-app-sdk", editable = true }' \
        in f["pyproject.toml"]
    assert "COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk" in f["Dockerfile"]


def test_explicit_mode_overrides_auto(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    monkeypatch.setattr(gen, "_REPO_ROOT", repo)
    monkeypatch.setattr(gen, "_SDK_DIR", repo / "sdk" / "opennvr-app-sdk")
    in_tree_but_pypi = gen.generate("a-one", "object_detection",
                                    repo / "examples", sdk="pypi")
    assert "tool.uv.sources" not in _files(in_tree_but_pypi)["pyproject.toml"]
    out_but_path = gen.generate("a-two", "object_detection",
                                tmp_path / "outside", sdk="path")
    assert "editable = true" in _files(out_but_path)["pyproject.toml"]
    with pytest.raises(ValueError):
        gen.generate("a-three", "object_detection", tmp_path, sdk="conda")
