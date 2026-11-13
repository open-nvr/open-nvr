# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Plate scan policy — how hard the sweep works for one camera's reads.

The consensus sweep (plate_enrichment) trades time for accuracy: it OCRs
up to four looks per visit, at most two calls in flight on the whole
box, and writes a plate only when two looks agree. At an entry gate
that is right — the barrier opens on the number. On a road camera at
one car every few seconds, on a weak CPU, it is a backlog measured in
minutes, while the ANPR app has already alarmed on the first read.

So the policy is per camera and chosen by the operator in the ANPR
app's config form (``scan_mode`` + ``scan_mode_overrides``), which
core reads from the installed app's registry row — the same way the
overlay switch reaches the track consumer. Three modes:

* ``accurate`` — the sweep as shipped: the environment defaults.
* ``fast``     — first accepted read wins, at most two looks, a short
                 dedup window. The table shows the plate as soon as
                 the first look passes the guards; the bus consumer
                 writes it instead of deferring to the sweep.
* ``auto``     — ``accurate`` while the sweep keeps up, ``fast`` while
                 it is backlogged (more visits waiting for OCR than
                 the box can have in flight). Measured here, in core,
                 from the sweep's own in-flight ledger.

Everything is read live and cached briefly, so a change in the form
reaches the next visit within the cache window. A missing app, a DB
error or a junk value all mean ``accurate`` — the policy can never
make the platform read worse than it did before it existed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LPR_APP_ID = "license-plate-recognition"
MODES: tuple[str, ...] = ("accurate", "fast", "auto")
DEFAULT_MODE = "accurate"

#: ``fast``: one accepted read is final, two looks at most, and a short
#: rolling dedup window — on a road the next car follows in seconds.
FAST_MIN_AGREEING = 1
FAST_MAX_LOOKS = 2
FAST_DEDUP_WINDOW_DEFAULT_S = 8.0

#: ``auto`` flips to fast when this many sweeps are waiting or running.
#: The sweep allows two OCR calls in flight, so three pending visits
#: means at least one is queued behind the others.
AUTO_BACKLOG_DEFAULT = 3

#: How long a read of the app's config is trusted. Visits arrive many
#: times a minute on a busy site; the form changes a few times a year.
_CONFIG_TTL_S = 15.0


@dataclass(frozen=True)
class PlatePolicy:
    """The sweep's dials for one visit."""

    mode: str                 # the EFFECTIVE mode: accurate | fast
    configured: str           # what the operator chose: accurate | fast | auto
    min_agreeing: int
    max_looks: int
    dedup_window_s: float
    reason: str

    @property
    def first_read_wins(self) -> bool:
        return self.min_agreeing <= 1


def fast_dedup_window_s() -> float:
    raw = os.environ.get("OPENNVR_PLATE_FAST_DEDUP_WINDOW_S", "")
    try:
        value = float(raw)
    except ValueError:
        return FAST_DEDUP_WINDOW_DEFAULT_S
    return max(0.0, value)


def auto_backlog_threshold() -> int:
    raw = os.environ.get("OPENNVR_PLATE_AUTO_BACKLOG", "")
    try:
        value = int(raw)
    except ValueError:
        return AUTO_BACKLOG_DEFAULT
    return max(1, value)


# ── the operator's choice, from the app's registry row ──────────────

_cache_lock = threading.Lock()
_cache: tuple[float, dict] | None = None   # (fetched_at, config)


def _load_app_config() -> dict:
    """The ANPR app's effective config, or ``{}``. Imported lazily so
    this module stays importable without the ORM (tests, tools)."""
    from core.database import SessionLocal
    from models import InstalledApp

    db = SessionLocal()
    try:
        row = db.query(InstalledApp).filter(InstalledApp.id == LPR_APP_ID).first()
        if row is None:
            return {}
        cfg = row.config_json
        return dict(cfg) if isinstance(cfg, dict) else {}
    finally:
        db.close()


def app_config(now: float | None = None) -> dict:
    """Cached read of the app's config; a failure answers the last good
    value, or ``{}``."""
    global _cache
    now = time.monotonic() if now is None else now
    with _cache_lock:
        if _cache is not None and now - _cache[0] < _CONFIG_TTL_S:
            return _cache[1]
    try:
        cfg = _load_app_config()
    except Exception:  # noqa: BLE001 - policy must never break the sweep
        logger.debug("plate policy: could not read the ANPR app config", exc_info=True)
        with _cache_lock:
            return _cache[1] if _cache is not None else {}
    with _cache_lock:
        _cache = (now, cfg)
    return cfg


def forget_cached_config() -> None:
    """Drop the cache (tests, and the config PUT so a form save applies
    to the very next visit)."""
    global _cache
    with _cache_lock:
        _cache = None


def _normalize_mode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if text in MODES else None


def configured_mode(camera_id: int | str | None, cfg: dict | None = None) -> str:
    """What the operator asked for on this camera: the per-camera
    override if one names it, else the site-wide ``scan_mode``, else
    ``accurate``. Camera keys are accepted as ``3``, ``"3"`` or
    ``"cam3"`` — the form and the bus both spell them."""
    from services.camera_scope import camera_id_from_handle

    cfg = app_config() if cfg is None else cfg
    cam = camera_id_from_handle(camera_id)
    overrides = cfg.get("scan_mode_overrides")
    if cam is not None and isinstance(overrides, dict):
        for key, value in overrides.items():
            if camera_id_from_handle(key) == cam:
                mode = _normalize_mode(value)
                if mode is not None:
                    return mode
                break
    return _normalize_mode(cfg.get("scan_mode")) or DEFAULT_MODE


# ── auto: the sweep's own backlog ───────────────────────────────────

_auto_lock = threading.Lock()
_auto_last_mode: str | None = None
_auto_last_logged: float = 0.0


def sweep_backlog() -> int:
    """Visits whose sweep is queued or running right now."""
    from services.plate_enrichment import sweeps_in_flight

    return sweeps_in_flight()


def auto_mode(backlog: int | None = None, now: float | None = None) -> tuple[str, str]:
    """``(mode, reason)`` for ``auto``: fast while backlogged. Mode flips
    are logged once a minute at most — a busy road would otherwise
    write a line per visit."""
    global _auto_last_mode, _auto_last_logged
    backlog = sweep_backlog() if backlog is None else backlog
    threshold = auto_backlog_threshold()
    mode = "fast" if backlog >= threshold else "accurate"
    reason = f"auto: {backlog} sweep(s) pending, threshold {threshold}"
    now = time.monotonic() if now is None else now
    with _auto_lock:
        if mode != _auto_last_mode and now - _auto_last_logged >= 60.0:
            logger.info("plate policy: auto → %s (%s)", mode, reason)
            _auto_last_logged = now
        _auto_last_mode = mode
    return mode, reason


# ── the answer the sweep asks for ───────────────────────────────────


def accurate_policy(configured: str = "accurate", reason: str = "accurate: environment defaults") -> PlatePolicy:
    from services.plate_enrichment import (
        MAX_INGEST_ATTEMPTS, dedup_window_s, min_agreeing_reads,
    )

    return PlatePolicy(
        mode="accurate", configured=configured,
        min_agreeing=min_agreeing_reads(), max_looks=MAX_INGEST_ATTEMPTS,
        dedup_window_s=dedup_window_s(), reason=reason,
    )


def fast_policy(configured: str = "fast", reason: str = "fast: first accepted read wins") -> PlatePolicy:
    return PlatePolicy(
        mode="fast", configured=configured,
        min_agreeing=FAST_MIN_AGREEING, max_looks=FAST_MAX_LOOKS,
        dedup_window_s=fast_dedup_window_s(), reason=reason,
    )


def policy_for(camera_id: int | str | None, *, cfg: dict | None = None,
               backlog: int | None = None) -> PlatePolicy:
    """The sweep's dials for a visit on ``camera_id``. Never raises."""
    try:
        configured = configured_mode(camera_id, cfg)
    except Exception:  # noqa: BLE001
        logger.debug("plate policy: falling back to accurate", exc_info=True)
        configured = DEFAULT_MODE
    if configured == "fast":
        return fast_policy()
    if configured == "auto":
        mode, reason = auto_mode(backlog)
        return fast_policy("auto", reason) if mode == "fast" else accurate_policy("auto", reason)
    return accurate_policy()
