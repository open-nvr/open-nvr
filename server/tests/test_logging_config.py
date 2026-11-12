# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""LOGS_DIR resolution: source-tree default, OPENNVR_LOGS_DIR override.

``core.logging_config`` resolves and creates its logs directory at import
time, so both are pinned by importing the module under a controlled
environment:

* unset (or empty) OPENNVR_LOGS_DIR resolves to ``<repo>/logs`` — the
  layout the Docker image and the bare-metal dev shell rely on;
* a set OPENNVR_LOGS_DIR wins, and the directory is created at import —
  an install on a read-only source tree (NixOS) points it at a writable
  directory and starts instead of dying on ``mkdir``.

Many suites in this directory replace ``sys.modules["core.logging_config"]``
with a stub at collection time (they do not want log files from a unit
test), so a module object imported here at collection is no longer the one
in ``sys.modules`` by the time the tests run, and ``importlib.reload``
refuses it. Each test therefore imports a FRESH copy under its environment,
and the fixture puts back exactly what was registered before — the stub,
or the real module — so later suites see what they expect.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NAME = "core.logging_config"


@pytest.fixture()
def fresh_logging_config():
    """A loader that imports ``core.logging_config`` anew under the current
    environment; afterwards the module registered before the test is put
    back (and OPENNVR_LOGS_DIR cleared), so nothing downstream inherits a
    LOGS_DIR pointing into a deleted ``tmp_path``."""
    saved = sys.modules.get(NAME)

    def load():
        sys.modules.pop(NAME, None)
        return importlib.import_module(NAME)

    yield load
    os.environ.pop("OPENNVR_LOGS_DIR", None)
    sys.modules.pop(NAME, None)
    if saved is not None:
        sys.modules[NAME] = saved


def test_logs_dir_defaults_to_source_tree(monkeypatch, fresh_logging_config):
    monkeypatch.delenv("OPENNVR_LOGS_DIR", raising=False)
    module = fresh_logging_config()
    assert module.LOGS_DIR == Path(__file__).resolve().parents[2] / "logs"
    assert module.LOGS_DIR.is_dir()


def test_logs_dir_empty_env_behaves_as_unset(monkeypatch, fresh_logging_config):
    monkeypatch.setenv("OPENNVR_LOGS_DIR", "")
    module = fresh_logging_config()
    assert module.LOGS_DIR == Path(__file__).resolve().parents[2] / "logs"


def test_logs_dir_follows_opennvr_logs_dir(monkeypatch, tmp_path, fresh_logging_config):
    override = tmp_path / "nested" / "logs"
    monkeypatch.setenv("OPENNVR_LOGS_DIR", str(override))
    module = fresh_logging_config()
    assert module.LOGS_DIR == override
    assert module.LOGS_DIR.is_dir()

