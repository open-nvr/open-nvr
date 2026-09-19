# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The running server's version, for code outside ``main``."""

from __future__ import annotations


def server_version() -> str:
    try:
        from main import __version__  # noqa: PLC0415 - main is loaded by then
    except Exception:  # noqa: BLE001 - tests import services without main
        return "unknown"
    return str(__version__)
