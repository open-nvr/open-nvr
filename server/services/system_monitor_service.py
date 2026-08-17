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

"""Host resource monitor: CPU / memory / recordings-volume disk.

Samples every SAMPLE_INTERVAL_SECONDS in a worker thread (disk_usage on a NAS
mount can block), keeps short history in RAM only (no DB writes per sample —
the DB usually shares the volume being monitored), and raises edge-triggered
SystemEvent alerts with sustained-duration + hysteresis so boundary-hovering
metrics don't flap.

Container caveat: psutil reports host-wide /proc values, not cgroup limits —
the payload is labeled ``scope: "host"``; cgroup awareness is a follow-up.
"""

from __future__ import annotations

import json
import shutil
from collections import deque
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.logging_config import main_logger
from models import SecuritySetting
from schemas import SystemMonitoringSettings
from services.storage_service import get_effective_recordings_base_path
from services.system_events import latest_event_state, record_system_event

try:  # degrade to disk-only metrics on stripped deployments
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

MONITORING_SETTINGS_KEY = "system_monitoring"

SAMPLE_INTERVAL_SECONDS = 15
STARTUP_DELAY_SECONDS = 30

# Ring buffers: 15s grain for 1h, 5-min averages for 24h. RAM-only by design.
_FINE_MAXLEN = 240
_COARSE_MAXLEN = 288
_COARSE_EVERY = 20  # fine samples per coarse point (20 * 15s = 5 min)

ALERT_CPU_HIGH = "cpu_high"
ALERT_MEMORY_HIGH = "memory_high"
ALERT_DISK_LOW = "disk_low"
ALERT_DISK_STAT_ERROR = "disk_stat_error"


def load_monitoring_settings(db: Session) -> SystemMonitoringSettings:
    """Load monitoring settings (same JSON-in-security_settings pattern as
    recordings_retention)."""
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == MONITORING_SETTINGS_KEY)
        .first()
    )
    if not row or not row.json_value:
        return SystemMonitoringSettings()
    try:
        data = json.loads(row.json_value)
        return SystemMonitoringSettings(
            **{**SystemMonitoringSettings().model_dump(), **data}
        )
    except Exception:
        return SystemMonitoringSettings()


class SystemMonitorService:
    """Sampler + edge-triggered threshold state machine."""

    def __init__(self) -> None:
        self._fine: deque[dict[str, Any]] = deque(maxlen=_FINE_MAXLEN)
        self._coarse: deque[dict[str, Any]] = deque(maxlen=_COARSE_MAXLEN)
        self._coarse_acc: list[dict[str, Any]] = []
        self._last_sample: dict[str, Any] | None = None
        # Sustained-window bookkeeping: metric -> first breach timestamp.
        self._breach_since: dict[str, float] = {}
        self._clear_since: dict[str, float] = {}
        # Alarm states seed lazily from the DB (authoritative across restarts).
        self._active: dict[str, bool] | None = None
        self._active_info: dict[str, dict[str, Any]] = {}
        self._last_notified: dict[str, float] = {}

    # ------------------------------------------------------------------ sample

    def sample(self, db: Session) -> dict[str, Any]:
        """One sync sample. Runs in a worker thread."""
        now = datetime.now(UTC)
        out: dict[str, Any] = {
            "sampled_at": now.isoformat(),
            "ts": now.timestamp(),
            "scope": "host",
            "cpu_percent": None,
            "load_avg": None,
            "memory": None,
            "disk": None,
            "disk_error": None,
        }
        if psutil is not None:
            try:
                # interval=None = average since previous call, i.e. a true
                # 15s window at our cadence (first call returns 0.0).
                out["cpu_percent"] = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                out["memory"] = {
                    "total": mem.total,
                    "used": mem.total - mem.available,
                    "percent": mem.percent,
                }
                try:
                    out["load_avg"] = list(psutil.getloadavg())
                except (AttributeError, OSError):
                    pass
            except Exception as e:
                main_logger.warning(f"psutil sampling failed: {e}")

        try:
            base_path = get_effective_recordings_base_path(db)
            stat = shutil.disk_usage(base_path)
            out["disk"] = {
                "path": base_path,
                "total": stat.total,
                "used": stat.used,
                "free": stat.free,
                "percent": round(stat.used / stat.total * 100, 1)
                if stat.total
                else 0.0,
            }
        except Exception as e:
            out["disk_error"] = str(e)
        return out

    # ----------------------------------------------------------------- history

    def _record_sample(self, sample: dict[str, Any]) -> None:
        self._last_sample = sample
        point = {
            "ts": sample["ts"],
            "cpu_percent": sample.get("cpu_percent"),
            "memory_percent": (sample.get("memory") or {}).get("percent"),
            "disk_percent": (sample.get("disk") or {}).get("percent"),
            "disk_free": (sample.get("disk") or {}).get("free"),
        }
        self._fine.append(point)
        self._coarse_acc.append(point)
        if len(self._coarse_acc) >= _COARSE_EVERY:
            acc = self._coarse_acc
            self._coarse_acc = []

            def avg(key: str) -> float | None:
                vals = [p[key] for p in acc if p[key] is not None]
                return round(sum(vals) / len(vals), 1) if vals else None

            self._coarse.append({
                "ts": acc[-1]["ts"],
                "cpu_percent": avg("cpu_percent"),
                "memory_percent": avg("memory_percent"),
                "disk_percent": avg("disk_percent"),
                "disk_free": acc[-1]["disk_free"],
            })

    def history(self, minutes: int) -> list[dict[str, Any]]:
        if minutes <= _FINE_MAXLEN * SAMPLE_INTERVAL_SECONDS // 60:
            cutoff = datetime.now(UTC).timestamp() - minutes * 60
            return [p for p in self._fine if p["ts"] >= cutoff]
        cutoff = datetime.now(UTC).timestamp() - minutes * 60
        return [p for p in self._coarse if p["ts"] >= cutoff]

    def snapshot(self, settings: SystemMonitoringSettings) -> dict[str, Any]:
        """Current state for GET /system/resources."""
        base = self._last_sample or {}
        return {
            **base,
            "monitoring_available": psutil is not None,
            "active_alerts": [
                {"alert_type": k, **v}
                for k, v in sorted(self._active_info.items())
                if (self._active or {}).get(k)
            ],
            "thresholds": settings.model_dump(),
        }

    # ------------------------------------------------------------ state machine

    def _seed_active_states(self, db: Session) -> None:
        """Lazily seed alarm states from the DB (survives restarts)."""
        if self._active is not None:
            return
        self._active = {}
        for alert_type in (
            ALERT_CPU_HIGH,
            ALERT_MEMORY_HIGH,
            ALERT_DISK_LOW,
            ALERT_DISK_STAT_ERROR,
        ):
            try:
                self._active[alert_type] = (
                    latest_event_state(db, alert_type) == "active"
                )
            except Exception:
                self._active[alert_type] = False

    def _edge(
        self,
        *,
        alert_type: str,
        breach: bool,
        clear: bool,
        sustained_seconds: int,
        now_ts: float,
        severity: str,
        description: str,
        data: dict[str, Any],
        transitions: list[dict[str, Any]],
    ) -> None:
        """Sustained-window edge trigger: raise after `breach` held for
        sustained_seconds, resolve after `clear` (hysteresis band) held as
        long. In the dead band between the two, timers reset and state holds."""
        active = self._active.get(alert_type, False)
        if breach:
            self._clear_since.pop(alert_type, None)
            start = self._breach_since.setdefault(alert_type, now_ts)
            if not active and now_ts - start >= sustained_seconds:
                self._active[alert_type] = True
                self._active_info[alert_type] = {
                    "severity": severity,
                    "description": description,
                    "since": datetime.now(UTC).isoformat(),
                }
                transitions.append({
                    "event_type": alert_type,
                    "state": "active",
                    "severity": severity,
                    "description": description,
                    "data": data,
                })
        elif clear:
            self._breach_since.pop(alert_type, None)
            start = self._clear_since.setdefault(alert_type, now_ts)
            if active and now_ts - start >= sustained_seconds:
                self._active[alert_type] = False
                self._active_info.pop(alert_type, None)
                transitions.append({
                    "event_type": alert_type,
                    "state": "inactive",
                    "severity": "info",
                    "description": f"{description} — resolved",
                    "data": data,
                })
        else:
            # Dead band: neither raising nor clearing.
            self._breach_since.pop(alert_type, None)
            self._clear_since.pop(alert_type, None)

    def evaluate(
        self, sample: dict[str, Any], settings: SystemMonitoringSettings
    ) -> list[dict[str, Any]]:
        """Pure-ish threshold evaluation -> list of state transitions."""
        transitions: list[dict[str, Any]] = []
        now_ts = sample["ts"]
        hyst = settings.resolve_hysteresis_percent

        cpu = sample.get("cpu_percent")
        thr = settings.cpu_percent_threshold
        if thr is not None and cpu is not None:
            self._edge(
                alert_type=ALERT_CPU_HIGH,
                breach=cpu >= thr,
                clear=cpu < thr - hyst,
                sustained_seconds=settings.sustained_seconds,
                now_ts=now_ts,
                severity="warning",
                description=f"CPU usage {cpu:.0f}% (threshold {thr}%)",
                data={"cpu_percent": cpu, "threshold": thr},
                transitions=transitions,
            )

        mem = sample.get("memory")
        thr = settings.memory_percent_threshold
        if thr is not None and mem is not None:
            pct = mem["percent"]
            self._edge(
                alert_type=ALERT_MEMORY_HIGH,
                breach=pct >= thr,
                clear=pct < thr - hyst,
                sustained_seconds=settings.sustained_seconds,
                now_ts=now_ts,
                severity="warning",
                description=f"Memory usage {pct:.0f}% (threshold {thr}%)",
                data={"memory_percent": pct, "threshold": thr},
                transitions=transitions,
            )

        disk = sample.get("disk")
        disk_error = sample.get("disk_error")
        # Disk stat failure is its own alert: with no reading, neither the
        # monitor nor the retention purge can protect the volume (fail-safe
        # replaces the old silent float("inf") fail-open).
        self._edge(
            alert_type=ALERT_DISK_STAT_ERROR,
            breach=disk is None and disk_error is not None,
            clear=disk is not None,
            sustained_seconds=SAMPLE_INTERVAL_SECONDS,  # 2 consecutive samples
            now_ts=now_ts,
            severity="warning",
            description=f"Cannot read recordings disk usage: {disk_error}",
            data={"error": disk_error},
            transitions=transitions,
        )

        if disk is not None:
            free_gb = disk["free"] / (1024**3)
            used_pct = disk["percent"]
            pct_thr = settings.disk_used_percent_threshold
            gb_thr = settings.disk_min_free_gb
            breach = False
            clear = True
            if pct_thr is not None:
                breach = breach or used_pct >= pct_thr
                clear = clear and used_pct < pct_thr - hyst
            if gb_thr is not None:
                breach = breach or free_gb < gb_thr
                clear = clear and free_gb > gb_thr + 2.0
            if pct_thr is not None or gb_thr is not None:
                severity = (
                    "critical" if used_pct >= 98 or free_gb < 2.0 else "warning"
                )
                self._edge(
                    alert_type=ALERT_DISK_LOW,
                    # Disk moves slowly; one confirming sample is enough.
                    breach=breach,
                    clear=clear,
                    sustained_seconds=SAMPLE_INTERVAL_SECONDS,
                    now_ts=now_ts,
                    severity=severity,
                    description=(
                        f"Recordings disk low: {free_gb:.1f} GB free "
                        f"({used_pct:.0f}% used)"
                    ),
                    data={
                        "free_gb": round(free_gb, 1),
                        "used_percent": used_pct,
                        "path": disk["path"],
                    },
                    transitions=transitions,
                )
        return transitions

    # --------------------------------------------------------------- main pass

    def _run_sync(self) -> tuple[SystemMonitoringSettings, list[dict[str, Any]]]:
        """Sample + evaluate + persist transitions. Runs in a worker thread."""
        db = SessionLocal()
        try:
            settings = load_monitoring_settings(db)
            if not settings.enabled:
                return settings, []
            self._seed_active_states(db)
            sample = self.sample(db)
            self._record_sample(sample)
            transitions = self.evaluate(sample, settings)
            for t in transitions:
                record_system_event(
                    db,
                    event_type=t["event_type"],
                    state=t["state"],
                    severity=t["severity"],
                    description=t["description"],
                    data=t["data"],
                )
            return settings, transitions
        finally:
            db.close()

    async def run_once(self) -> None:
        import asyncio

        from services.event_bus_service import publish_system_alert

        settings, transitions = await asyncio.to_thread(self._run_sync)
        for t in transitions:
            log = main_logger.warning if t["state"] == "active" else main_logger.info
            log(f"System alert {t['event_type']} -> {t['state']}: {t['description']}")
            await publish_system_alert(
                alert_type=t["event_type"],
                state=t["state"],
                severity=t["severity"],
                payload={"description": t["description"], **t["data"]},
            )
            await self._maybe_notify(settings, t)

    async def _maybe_notify(
        self, settings: SystemMonitoringSettings, t: dict[str, Any]
    ) -> None:
        """Send raise transitions to enabled integrations, cooldown-gated."""
        if not settings.notify_integrations or t["state"] != "active":
            return
        now_ts = datetime.now(UTC).timestamp()
        last = self._last_notified.get(t["event_type"], 0.0)
        if now_ts - last < settings.renotify_cooldown_minutes * 60:
            return
        self._last_notified[t["event_type"]] = now_ts
        try:
            from services.integration_service import IntegrationService

            await IntegrationService.send_alert(
                subject=f"OpenNVR system alert: {t['event_type']}",
                message=t["description"],
                payload={
                    "event": f"system.{t['event_type']}",
                    "severity": t["severity"],
                    **t["data"],
                },
            )
        except Exception as e:
            main_logger.warning(f"System alert integration notify failed: {e}")


_monitor_instance: SystemMonitorService | None = None


def get_system_monitor() -> SystemMonitorService:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = SystemMonitorService()
    return _monitor_instance
