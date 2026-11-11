# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""LOGS_DIR resolution: source-tree default, OPENNVR_LOGS_DIR override.

``core.logging_config`` resolves and creates its logs directory at import
time, so both are pinned by re-importing the module under a controlled
environment:

* unset (or empty) OPENNVR_LOGS_DIR resolves to ``<repo>/logs`` — the
  layout the Docker image and the bare-metal dev shell rely on;
* a set OPENNVR_LOGS_DIR wins, and the directory is created at import —
  an install on a read-only source tree (NixOS) points it at a writable
  directory and starts instead of dying on ``mkdir``.

``importlib.reload`` mutates the shared module object in place, so every
test restores the default resolution afterwards: later suites must not
inherit a ``LOGS_DIR`` pointing into a deleted ``tmp_path``.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.logging_config  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_default_resolution():
    yield
    os.environ.pop("OPENNVR_LOGS_DIR", None)
    importlib.reload(core.logging_config)


def test_logs_dir_defaults_to_source_tree(monkeypatch):
    monkeypatch.delenv("OPENNVR_LOGS_DIR", raising=False)
    module = importlib.reload(core.logging_config)
    assert module.LOGS_DIR == Path(__file__).resolve().parents[2] / "logs"
    assert module.LOGS_DIR.is_dir()


def test_logs_dir_empty_env_behaves_as_unset(monkeypatch):
    monkeypatch.setenv("OPENNVR_LOGS_DIR", "")
    module = importlib.reload(core.logging_config)
    assert module.LOGS_DIR == Path(__file__).resolve().parents[2] / "logs"


def test_logs_dir_follows_opennvr_logs_dir(monkeypatch, tmp_path):
    override = tmp_path / "nested" / "logs"
    monkeypatch.setenv("OPENNVR_LOGS_DIR", str(override))
    module = importlib.reload(core.logging_config)
    assert module.LOGS_DIR == override
    assert module.LOGS_DIR.is_dir()
