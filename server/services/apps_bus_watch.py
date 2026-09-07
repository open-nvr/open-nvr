# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Is the apps bus actually joined to the platform bus?

The apps bus (``nats-apps``) is a leaf of the platform server: every
app's alert crosses that one link to reach the inbox, and every
platform detection crosses it the other way to reach the app. When the
link is down the failure is SILENT — both servers are healthy, every
app logs "connected", the operator's alerts simply stop. (The first
time it happened the leaf link's password was never rendered into the
URL and the platform server refused it for days.)

So core watches the link itself: ``nats-apps`` publishes its leaf
connections at ``/leafz`` on its monitoring port; if that list is
empty for longer than a grace period, core logs an error, raises an
inbox alert (once an hour while it lasts) and shows it under
``/health``'s detail. Recovery clears the condition and is logged.
Nothing here can raise — the watchdog is best-effort.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 30          # how often the background loop asks
GRACE_S = 120                  # unlinked this long before it counts
ALERT_EVERY_S = 3600           # one inbox alert per hour while it lasts
MONITOR_PORT = 8222


def monitor_url(nats_apps_url: str, explicit: str = "") -> str:
    """``nats://nats-apps:4222`` → ``http://nats-apps:8222/leafz``; an
    explicit monitor URL (``NATS_APPS_MONITOR_URL``) wins; "" when the
    apps bus is off."""
    if explicit:
        return explicit.rstrip("/") + ("" if explicit.endswith("/leafz") else "/leafz")
    if not nats_apps_url:
        return ""
    host = urlsplit(nats_apps_url).hostname
    return f"http://{host}:{MONITOR_PORT}/leafz" if host else ""


def parse_leafz(payload: Any) -> dict[str, Any]:
    """The bit of ``/leafz`` the watchdog cares about."""
    leafs = payload.get("leafs") if isinstance(payload, dict) else None
    leafs = leafs if isinstance(leafs, list) else []
    names = [str(leaf.get("name") or "?") for leaf in leafs if isinstance(leaf, dict)]
    return {"linked": bool(leafs), "leafs": len(leafs), "remotes": names}


@dataclass
class LinkState:
    """What the last checks said (module-level, read by ``/health``)."""
    checked_at: float = 0.0
    reachable: bool | None = None       # could /leafz be fetched at all
    linked: bool | None = None          # a leaf connection exists
    remotes: list[str] = field(default_factory=list)
    unlinked_since: float | None = None
    last_alert_at: float = 0.0
    error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "checked_at": (datetime.fromtimestamp(self.checked_at, UTC).isoformat()
                           if self.checked_at else None),
            "reachable": self.reachable,
            "linked": self.linked,
            "remotes": list(self.remotes),
            "unlinked_for_s": (int(time.time() - self.unlinked_since)
                               if self.unlinked_since else 0),
            "error": self.error,
        }


_state = LinkState()
_lock = threading.Lock()


def state() -> dict[str, Any]:
    with _lock:
        return _state.snapshot()


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = LinkState()


def fetch_leafz(url: str, timeout: float = 3.0) -> dict[str, Any]:
    """GET the monitoring endpoint; ``{"error": ...}`` instead of raising."""
    import httpx

    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return parse_leafz(r.json())
    except Exception as exc:  # anything means "unreachable"
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def observe(result: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Fold one check into the state. Returns ``{"transition": ..., "alert": bool}``
    — ``transition`` is ``"lost"`` / ``"restored"`` / ``None`` and ``alert``
    says whether an inbox alert is due now (the caller writes it)."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"transition": None, "alert": False}
    with _lock:
        st = _state
        st.checked_at = now
        if "error" in result:
            st.reachable = False
            st.linked = None
            st.remotes = []
            st.error = str(result["error"])
            # An unreachable monitor is its own problem (compose healthcheck
            # covers a dead container); it does not count as "unlinked".
            return out
        st.reachable = True
        st.error = ""
        st.linked = bool(result.get("linked"))
        st.remotes = list(result.get("remotes") or [])
        if st.linked:
            if st.unlinked_since is not None and now - st.unlinked_since >= GRACE_S:
                out["transition"] = "restored"
            st.unlinked_since = None
            st.last_alert_at = 0.0
            return out
        if st.unlinked_since is None:
            st.unlinked_since = now
            return out
        if now - st.unlinked_since < GRACE_S:
            return out
        if st.last_alert_at == 0.0:
            out["transition"] = "lost"
        if now - st.last_alert_at >= ALERT_EVERY_S:
            st.last_alert_at = now
            out["alert"] = True
    return out


def alert_envelope(now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    snap = state()
    return {
        "alert_id": f"apps-bus-unlinked-{int(now // ALERT_EVERY_S)}",
        "severity": "high",
        "title": "Apps bus is not linked to the platform bus — app alerts cannot reach the inbox",
        "description": (
            "The apps bus (nats-apps) has had no leaf connection to the platform "
            f"bus for {snap['unlinked_for_s'] // 60} minute(s). Installed apps are "
            "connected and running, but nothing they publish crosses to core: no "
            "app alerts, no domain events, and the platform's detections do not "
            "reach them. Check `docker logs opennvr_nats_apps` for "
            "'Leafnode Error' / 'Authorization Violation' (the leaf link "
            "authenticates with INTERNAL_API_KEY — nats-apps and nats must "
            "see the same value) and `docker logs opennvr_nats` for "
            "'authentication error' on port 7422. docs/APP_CREDENTIALS.md → "
            "\"When alerts stop\"."
        ),
        "source": {"kind": "platform", "name": "nats-apps"},
        "tags": ["apps-bus", "leaf-link", "infrastructure"],
        "evidence": snap,
        "fired_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }


def check_once(url: str, db=None) -> dict[str, Any]:
    """One full cycle: fetch, fold, log, alert. Never raises."""
    result = fetch_leafz(url)
    verdict = observe(result)
    try:
        if verdict["transition"] == "lost":
            logger.error("apps bus: NO leaf connection to the platform bus for %ds — app alerts "
                         "and platform detections are not crossing (%s)", GRACE_S, url)
        elif verdict["transition"] == "restored":
            logger.info("apps bus: leaf link to the platform bus restored (%s)",
                        ", ".join(state()["remotes"]) or "?")
        elif "error" in result:
            logger.debug("apps bus: monitor %s unreachable: %s", url, result["error"])
        if verdict["alert"]:
            from services.alerts_inbox import apply_alert
            apply_alert(alert_envelope(), db=db)
    except Exception as exc:
        logger.warning("apps bus watch: could not record the condition: %s", exc)
    return verdict
