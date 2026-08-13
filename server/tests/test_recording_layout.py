# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configurable recording layout — default must be byte-identical to before."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

from cryptography.fernet import Fernet

import datetime as _dt  # noqa: E402
if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_rl_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

import services.mediamtx_config_service as mcs  # noqa: E402
from core.config import settings  # noqa: E402

M = mcs.MediaMtxConfigService


def test_default_layout_is_unchanged():
    """The 'nested' default must render exactly the historical suffix — no
    existing deployment's on-disk layout may shift."""
    settings.recording_layout = "nested"
    settings.recording_path_template = None
    assert M.recording_path_template("%path") == "%path/%Y/%m/%d/%H/%M/%S"
    assert M.recording_path_template("cam-301") == "cam-301/%Y/%m/%d/%H/%M/%S"


def test_date_hour_layout_shape():
    settings.recording_layout = "date-hour"
    assert M.recording_path_template("%path") == "%path/%Y-%m-%d/%H/%M-%S"
    assert M.recording_path_template("301") == "301/%Y-%m-%d/%H/%M-%S"


def test_flat_layout():
    settings.recording_layout = "flat"
    assert M.recording_path_template("%path") == "%path/%Y-%m-%d_%H-%M-%S"


def test_custom_template_overrides():
    settings.recording_layout = "custom"
    settings.recording_path_template = "{camera}/%Y/%j"
    assert M.recording_path_template("%path") == "%path/%Y/%j"
    settings.recording_layout = "nested"
    settings.recording_path_template = None


def test_unknown_layout_falls_back_to_nested():
    settings.recording_layout = "banana"
    assert M.recording_path_template("%path") == "%path/%Y/%m/%d/%H/%M/%S"
    settings.recording_layout = "nested"


def test_no_preset_can_overwrite_within_its_granularity():
    """Every built-in preset's FILENAME must carry second-granularity, so two
    segments starting close together never write the same file (data loss)."""
    for layout in ("nested", "date-hour", "flat"):
        settings.recording_layout = layout
        tmpl = M.recording_path_template("%path")
        # the leaf (filename) segment must include %S
        leaf = tmpl.rsplit("/", 1)[-1]
        assert "%S" in leaf, f"{layout}: filename {leaf!r} lacks %S (collision risk)"
    settings.recording_layout = "nested"
