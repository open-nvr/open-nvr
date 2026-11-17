# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What the app knows about OpenNVR updates, and how it asks for one.

The updater runs on the host (scripts/update-host.sh / .ps1); core never
holds Docker. The two talk through two folders:

* ``data/updates`` (host → core, mounted read-only at ``/app/update-state``):
  the run state, the last check's result, the verified candidate manifest,
  the run history and the runner's heartbeat.
* ``data/update-requests`` (core → host, mounted at ``/app/update-requests``):
  an "update to X" request and the update policy, which the host runner
  picks up within a minute.

Core cannot touch the run state, the manifests or the backups. A request
only names a version; the host's updater still verifies a signed manifest
for it, so the most a compromised core can do is ask for a genuine release.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from updater import envfile
from updater.manifest import ManifestError, is_newer, parse

STATE_DIR = Path(os.environ.get("OPENNVR_UPDATE_STATE_DIR", "/app/update-state"))
REQUEST_DIR = Path(os.environ.get("OPENNVR_UPDATE_REQUEST_DIR", "/app/update-requests"))

#: Steps after which a run is over (scripts/update-host.*: upd_state STEP=…).
FINISHED_STEPS = {"succeeded", "rolled_back", "failed"}
#: A runner that has not checked in for this long is reported as not running.
RUNNER_STALE_S = 10 * 60

MODES = ("off", "notify", "manual", "auto")
DEFAULT_POLICY = {"mode": "auto", "channel": "stable", "auto_scope": "minor",
                  "window_days": "mon,tue,wed,thu,fri,sat,sun", "window_start": "02:00", "window_end": "04:00"}


def _kv_last(path: Path) -> dict[str, str]:
    """KEY=value file where a later line wins (state.env is appended to)."""
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip():
                out[key.strip()] = value.strip()
    return out


def _manifest(path: Path) -> dict | None:
    """A manifest the host already verified; parsed again only for its shape."""
    try:
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        parse(doc)
        return doc
    except (OSError, ValueError, ManifestError):
        return None


def _age_s(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def status(installed_version: str, installed_commit: str | None, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    available = None
    candidate = _manifest(STATE_DIR / "candidate" / "release-manifest.json")
    if candidate and is_newer(candidate["version"], installed_version):
        available = {
            "version": candidate["version"],
            "notes_url": candidate.get("notes_url"),
            "urgent": bool(candidate.get("urgent")),
            "manual_steps": [s.get("text", "") for s in candidate.get("manual_steps") or []],
            "state_migrations": bool(candidate.get("state_migrations")),
        }

    check = _kv_last(STATE_DIR / "check.env")
    message_file = STATE_DIR / "check.log"
    last_check = None
    if check:
        last_check = {
            "at": check.get("CHECKED_AT"),
            # 0 ready, 10 up to date, 20 refused, 21 needs the operator, 1 error
            "result": {"0": "available", "10": "up_to_date", "20": "refused",
                       "21": "needs_operator"}.get(check.get("CODE", ""), "error"),
            "message": message_file.read_text(encoding="utf-8", errors="replace")[-4000:]
            if message_file.is_file() else "",
        }

    run = None
    state = _kv_last(STATE_DIR / "state.env")
    if state.get("STEP"):
        run = {"from": state.get("FROM"), "to": state.get("TO"), "step": state["STEP"],
               "started": state.get("STARTED"), "finished": state.get("FINISHED"),
               "in_progress": state["STEP"] not in FINISHED_STEPS}
        # Why a run rolled back: what the release's own check said about the
        # candidate (the host keeps it in verify-candidate.log). A run that
        # never reached the check has none.
        verdict = STATE_DIR / "verify-candidate.log"
        if run["step"] in FINISHED_STEPS - {"succeeded"} and verdict.is_file():
            run["message"] = verdict.read_text(encoding="utf-8", errors="replace")[-4000:]

    history = []
    log = STATE_DIR / "history.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
            parts = line.split()
            if len(parts) >= 4:
                history.append({"finished": parts[0], "from": parts[1], "to": parts[2], "result": parts[3]})
    history.reverse()

    seen = _kv_last(STATE_DIR / "runner.env").get("LAST_SEEN")
    age = _age_s(seen, now)
    pending = _kv_last(REQUEST_DIR / "request.env")
    done = (STATE_DIR / "last-request").read_text(encoding="utf-8").strip() \
        if (STATE_DIR / "last-request").is_file() else ""
    return {
        "installed": {"version": installed_version, "commit": installed_commit},
        "available": available,
        "last_check": last_check,
        "run": run,
        "history": history,
        "runner": {"last_seen": seen, "running": age is not None and age < RUNNER_STALE_S},
        "pending_request": {"to": pending.get("TO"), "requested_by": pending.get("REQUESTED_BY"),
                            "requested_at": pending.get("REQUESTED_AT")}
        if pending.get("ID") and pending.get("ID") != done else None,
        "connected": STATE_DIR.is_dir() and REQUEST_DIR.is_dir(),
        "apps": apps_status(),
    }


def _json(path: Path) -> dict:
    import json

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _tail(path: Path, chars: int = 4000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-chars:] if path.is_file() else ""


def app_reports() -> dict[str, dict]:
    """The host's last app check (``apps.json``, updater/apps.py), by app id.
    Empty before the first check, on a site without the runner, or when the
    file cannot be read."""
    out = {}
    for report in _json(STATE_DIR / "apps.json").get("apps") or []:
        if isinstance(report, dict) and isinstance(report.get("id"), str):
            out[report["id"]] = report
    return out


def app_offer(report: dict) -> dict | None:
    """The newest release *report* offers, as the app shows it: with what the
    host's cosign check said, and whether it was rolled back here before."""
    offers = [o for o in report.get("offers") or [] if isinstance(o, dict)]
    if not offers:
        return None
    newest = offers[0]  # updater/apps.py sorts them newest first
    verified = _json(STATE_DIR / "apps-verified.json").get(f"{report.get('id')}@{newest.get('digest')}")
    failed = STATE_DIR / "apps-failed.txt"
    failed_before = failed.is_file() and f"{report.get('id')} {newest.get('version')}" in {
        line.strip() for line in failed.read_text(encoding="utf-8", errors="replace").splitlines()}
    return {
        "version": newest.get("version"),
        "kind": newest.get("kind"),
        "rollback": newest.get("rollback", "safe"),
        "signature": "unchecked" if not isinstance(verified, dict) else "ok" if verified.get("ok") else "failed",
        "signature_message": verified.get("message", "") if isinstance(verified, dict) else "",
        "failed_before": failed_before,
    }


def apps_status() -> dict:
    """App releases from the registry, as the host runner left them
    (scripts/update-host.*, "apps from the registry")."""
    doc = _json(STATE_DIR / "apps.json")
    apps = []
    for report in app_reports().values():
        apps.append({
            "id": report["id"],
            "state": report.get("state"),
            "reason": report.get("reason", ""),
            "installed_version": report.get("installed_version") or None,
            "installed_revoked": report.get("installed_revoked") or None,
            "available": app_offer(report),
            "skipped": [s for s in report.get("skipped") or [] if isinstance(s, str)],
        })

    check = _kv_last(STATE_DIR / "apps-check.env")
    last_check = None
    if check:
        ok = check.get("CODE") == "0"
        last_check = {"at": doc.get("checked_at"), "ok": ok,
                      "message": "" if ok else _tail(STATE_DIR / "apps-check.log")}

    run = None
    state = _kv_last(STATE_DIR / "apps-state.env")
    if state.get("STEP"):
        run = {"app": state.get("APP_ID"), "from": state.get("FROM_VERSION"), "to": state.get("VERSION"),
               "step": state["STEP"], "started": state.get("STARTED"), "finished": state.get("FINISHED"),
               "in_progress": state["STEP"] not in FINISHED_STEPS}
        if run["step"] in FINISHED_STEPS - {"succeeded"}:
            run["message"] = _tail(STATE_DIR / "apps-up.log")

    history = []
    log = STATE_DIR / "apps-history.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
            parts = line.split()
            if len(parts) >= 5:
                history.append({"finished": parts[0], "app": parts[1], "from": parts[2], "to": parts[3],
                                "result": parts[4]})
    history.reverse()

    pending = _kv_last(REQUEST_DIR / "app-request.env")
    done = (STATE_DIR / "last-app-request").read_text(encoding="utf-8").strip() \
        if (STATE_DIR / "last-app-request").is_file() else ""
    return {
        "apps": sorted(apps, key=lambda a: a["id"]),
        "problems": [p for p in doc.get("problems") or [] if isinstance(p, str)],
        "last_check": last_check,
        "run": run,
        "history": history,
        "pending_request": {"app": pending.get("APP"), "requested_by": pending.get("REQUESTED_BY"),
                            "requested_at": pending.get("REQUESTED_AT")}
        if pending.get("ID") and pending.get("ID") != done else None,
    }


def request_app_update(app_id: str, username: str) -> str:
    """Ask the host runner to install *app_id*'s newest release now (it checks
    again, and still verifies the signature); returns the request id."""
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    tmp = REQUEST_DIR / "app-request.env.tmp"
    envfile.write_kv(tmp, {"ID": rid, "ACTION": "app-update", "APP": app_id,
                           "REQUESTED_BY": _token(username),
                           "REQUESTED_AT": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    tmp.replace(REQUEST_DIR / "app-request.env")
    return rid


def cancel_app_request() -> bool:
    try:
        (REQUEST_DIR / "app-request.env").unlink()
        return True
    except FileNotFoundError:
        return False


def request_update(version: str, username: str) -> str:
    """Ask the host runner to update to *version*; returns the request id."""
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex
    tmp = REQUEST_DIR / "request.env.tmp"
    envfile.write_kv(tmp, {"ID": rid, "ACTION": "update", "TO": version,
                           "REQUESTED_BY": _token(username),
                           "REQUESTED_AT": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    tmp.replace(REQUEST_DIR / "request.env")  # the runner never sees half a file
    return rid


def cancel_request() -> bool:
    try:
        (REQUEST_DIR / "request.env").unlink()
        return True
    except FileNotFoundError:
        return False


def write_policy(policy: dict) -> None:
    """Mirror the policy for the host runner (the database holds the original)."""
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REQUEST_DIR / "policy.env.tmp"
    values = {}
    for key, value in policy.items():
        if key == "app_modes":
            # One line per app, as updater/policy.py reads them: APP_<ID>_MODE.
            for app_id, mode in (value or {}).items():
                values[f"APP_{app_id.upper().replace('-', '_')}_MODE"] = _token(str(mode))
        elif value != "":  # "" = follow the release setting: left out
            values[key.upper()] = _token(str(value))
    envfile.write_kv(tmp, values)
    tmp.replace(REQUEST_DIR / "policy.env")


def _token(value: str) -> str:
    """A value the host scripts can read as one plain word."""
    return "".join(c if c.isalnum() or c in "._:,@+-" else "_" for c in value)[:100] or "_"
