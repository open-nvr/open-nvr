from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..config import get_settings


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def now_local() -> datetime:
    """Current time in the deployment's timezone (aware)."""
    return datetime.now(local_tz())


def now_local_iso() -> str:
    return now_local().isoformat()


def to_local_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).isoformat()


# The names the AWS version used; kept so ported code reads the same.
now_ist = now_local
now_ist_iso = now_local_iso
to_ist_iso = to_local_iso
