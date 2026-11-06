"""Attendly's platform face: the SDK's ``ContractApp``.

The App SDK's bases own the operator contract — ``/health``, ``/manifest``,
``/state`` and their counters — and RFC-0002 asks every first-party app
to ride one of them so those answers never drift from what core expects.
Attendly is a full web application served by FastAPI, so it does not run
``ContractApp``'s own HTTP server or loop; it keeps one instance and lets
the FastAPI routes answer with the base's snapshots.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, Optional

from opennvr_app_sdk import ContractApp

from . import APP_ID, APP_VERSION
from .config import Settings, get_settings
from .manifest import app_ui_url, build_manifest

logger = logging.getLogger(__name__)


class AttendlyApp(ContractApp):
    """The contract surface; the work happens in the engines."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self.settings = s
        self.manifest = build_manifest(s)
        cfg = SimpleNamespace(
            contract_port=s.port, opennvr_url=s.opennvr_url, internal_api_key=s.internal_api_key,
            kaic_url=s.kaic_url, app_id=APP_ID, version=APP_VERSION,
        )
        super().__init__(cfg)

    # ── ContractMixin hooks ────────────────────────────────────────

    def state_snapshot(self) -> Dict[str, Any]:
        from .services.access import GateEngine
        from .services.engine import StreamEngine
        from .services.staff_attendance import StaffEngine
        from .storage import repo

        try:
            c = repo.counts()
        except Exception:  # noqa: BLE001 — a cold database is not a crash
            c = {}
        workers = _safe(lambda: StreamEngine.get_shared().status(), [])
        gate_workers = _safe(lambda: GateEngine.get_shared().status(), [])
        staff_workers = _safe(lambda: StaffEngine.get_shared().status(), [])
        return {
            "app": APP_ID, "version": APP_VERSION, "ui_url": app_ui_url(self.settings),
            "students": c.get("students", 0), "rooms": c.get("rooms", 0), "courses": c.get("courses", 0),
            "finalized_classes": c.get("finalized_classes", 0),
            "active_classes": sum(1 for w in workers if w.get("running")), "workers": workers,
            "hostlers": c.get("hostlers", 0), "staff": c.get("staff", 0), "departments": c.get("departments", 0),
            "gates": c.get("gates", 0), "active_gates": sum(1 for w in gate_workers if w.get("running")),
            "gate_workers": gate_workers, "staff_workers": staff_workers,
        }

    def not_ready_reason(self) -> Optional[str]:
        """Up, but unable to do its job? The database must open; the face
        model is asked lazily, so a missing adapter shows on first use
        rather than here (probing it on every /health would be a cost)."""
        try:
            from .storage import repo

            repo.counts()
        except Exception as exc:  # noqa: BLE001
            return f"database unavailable: {exc}"
        return None

    def health(self) -> Dict[str, Any]:
        """``/health`` — the base's payload plus the fields attendly's own
        callers and tests have always read."""
        payload = self.health_snapshot()
        payload.update({"status": "ok" if payload.get("ready") else "degraded", "app": APP_ID, "version": APP_VERSION,
                        "uptime_seconds": int(payload.get("uptime_s") or 0)})
        return payload


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


_shared: Optional[AttendlyApp] = None


def get_platform_app(settings: Optional[Settings] = None) -> AttendlyApp:
    global _shared
    if _shared is None or (settings is not None and _shared.settings is not settings):
        _shared = AttendlyApp(settings)
    return _shared


def reset_platform_app() -> None:
    global _shared
    _shared = None
