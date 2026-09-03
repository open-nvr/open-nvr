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

"""Alarm ACTIONS — what happens beyond the browser when an alarm fires.

The inbox (services/alerts_inbox.py) makes alarms visible and audible in
open browsers. This module is for the moments nobody has a browser open:
when a stored alert reaches the configured severity, it can

* place a PHONE CALL and/or send an SMS through Twilio's REST API to a
  list of numbers (the guard's phone rings even at 3am), and
* hit an external WEBHOOK/RELAY — a hooter or speaker behind a Shelly/
  Tasmota/Node-RED style HTTP relay, a SIEM, anything with a URL.

Configuration lives in SecuritySetting under ``alert_action_config``;
the Twilio auth token is Fernet-encrypted at rest via the platform's
credential vault key and NEVER returned by the API (masked as set/unset).
Dispatch is best-effort and bounded: every failure is a logged result,
never an exception into the alert path — a broken phone number cannot
break the inbox.

SIP trunk calling is deliberately not implemented here: it needs a real
SIP stack and a trunk account, while Twilio Voice covers "the phone
rings" with one authenticated HTTP call. The config shape leaves room
for a ``sip`` block when that lands.
"""

from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger(__name__)

ACTION_CONFIG_KEY = "alert_action_config"

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

DEFAULT_ACTION_CONFIG: dict = {
    # Act on alerts AT OR ABOVE this severity.
    "min_severity": "high",
    "twilio": {
        "enabled": False,
        "account_sid": "",
        # Stored encrypted as auth_token_enc; plaintext never persisted.
        "auth_token_enc": "",
        "from_number": "",
        "to_numbers": [],
        # call | sms | both
        "mode": "call",
    },
    "webhook": {
        "enabled": False,
        "url": "",
        # POST sends a JSON alarm payload; GET hits the URL bare (dumb
        # relays that just switch on whatever request arrives).
        "method": "POST",
    },
}

_TWILIO_TIMEOUT_S = 10.0
_WEBHOOK_TIMEOUT_S = 5.0


# ── config storage ─────────────────────────────────────────────────


def _vault():
    from core.config import settings
    from services.credential_vault_service import CredentialVaultService

    return CredentialVaultService(settings)


def load_action_config(db) -> dict:
    """Stored config overlaid on defaults. Unknown keys dropped, modes
    sanitized — corrupt state degrades to 'actions off', never to a
    surprise phone call."""
    from models import SecuritySetting

    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == ACTION_CONFIG_KEY)
        .first()
    )
    raw = None
    if row is not None:
        try:
            raw = json.loads(row.json_value)
        except ValueError:
            raw = None
    return normalize_action_config(raw)


def normalize_action_config(raw: object) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_ACTION_CONFIG))  # deep copy
    if not isinstance(raw, dict):
        return cfg
    if raw.get("min_severity") in _SEVERITY_RANK:
        cfg["min_severity"] = raw["min_severity"]
    tw = raw.get("twilio")
    if isinstance(tw, dict):
        t = cfg["twilio"]
        t["enabled"] = bool(tw.get("enabled", False))
        for key in ("account_sid", "auth_token_enc", "from_number"):
            if isinstance(tw.get(key), str):
                t[key] = tw[key].strip()
        if isinstance(tw.get("to_numbers"), list):
            t["to_numbers"] = [str(n).strip() for n in tw["to_numbers"]
                               if str(n).strip()][:10]
        if tw.get("mode") in ("call", "sms", "both"):
            t["mode"] = tw["mode"]
    wh = raw.get("webhook")
    if isinstance(wh, dict):
        w = cfg["webhook"]
        w["enabled"] = bool(wh.get("enabled", False))
        if isinstance(wh.get("url"), str):
            w["url"] = wh["url"].strip()
        if wh.get("method") in ("POST", "GET"):
            w["method"] = wh["method"]
    return cfg


def save_action_config(db, incoming: dict) -> dict:
    """Merge an API payload into the stored config.

    The Twilio auth token arrives as plaintext ``auth_token`` and is
    encrypted here; an EMPTY/absent token keeps the stored one, so the
    UI never needs to (and never can) round-trip the secret."""
    from models import SecuritySetting

    current = load_action_config(db)
    merged = normalize_action_config(
        {**current, **incoming,
         "twilio": {**current["twilio"], **(incoming.get("twilio") or {})},
         "webhook": {**current["webhook"], **(incoming.get("webhook") or {})}}
    )
    token_plain = (incoming.get("twilio") or {}).get("auth_token")
    if isinstance(token_plain, str) and token_plain.strip():
        merged["twilio"]["auth_token_enc"] = _vault().encrypt_token(
            token_plain.strip())
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == ACTION_CONFIG_KEY)
        .first()
    )
    if row is None:
        row = SecuritySetting(key=ACTION_CONFIG_KEY,
                              json_value=json.dumps(merged))
        db.add(row)
    else:
        row.json_value = json.dumps(merged)
    db.commit()
    return merged


def masked_action_config(cfg: dict) -> dict:
    """API-safe view: the encrypted token is replaced by a set/unset
    flag. The secret leaves the server exactly never."""
    out = json.loads(json.dumps(cfg))
    tw = out.get("twilio", {})
    tw["auth_token_set"] = bool(tw.pop("auth_token_enc", ""))
    return out


# ── dispatch ───────────────────────────────────────────────────────


def dispatch_alarm_actions(alert: dict, *, force: bool = False) -> list[dict]:
    """Run every enabled action for one stored alert.

    ``alert``: severity/title/description/camera_id/alert_id (the wire
    fields). ``force`` skips the severity gate (the test button — it
    must exercise the actions regardless of policy). Returns one result
    dict per attempted action; every failure is a result, never a
    raise."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        cfg = load_action_config(db)
    finally:
        db.close()

    results: list[dict] = []
    severity = str(alert.get("severity") or "high")
    if not force:
        if (_SEVERITY_RANK.get(severity, 2)
                < _SEVERITY_RANK[cfg["min_severity"]]):
            return results

    tw = cfg["twilio"]
    if tw["enabled"]:
        results.extend(_dispatch_twilio(tw, alert))
    wh = cfg["webhook"]
    if wh["enabled"]:
        results.append(_dispatch_webhook(wh, alert))

    for r in results:
        level = logging.INFO if r.get("ok") else logging.WARNING
        logger.log(level, "alarm action %s → %s (%s)",
                   r.get("action"), "ok" if r.get("ok") else "FAILED",
                   r.get("detail", ""))
    return results


def dispatch_in_background(alert: dict) -> None:
    """Fire-and-forget dispatch for the alert-ingest path — the inbox
    write must never wait on a phone network."""
    t = threading.Thread(target=dispatch_alarm_actions, args=(dict(alert),),
                         daemon=True, name="alarm-actions")
    t.start()


def _alarm_text(alert: dict) -> str:
    bits = [f"OpenNVR {alert.get('severity', 'high')} alarm:",
            str(alert.get("title") or "alarm")]
    if alert.get("camera_id"):
        bits.append(f"on camera {alert['camera_id']}")
    return " ".join(bits)


def _dispatch_twilio(tw: dict, alert: dict) -> list[dict]:
    import httpx

    sid = tw["account_sid"]
    token = ""
    try:
        if tw["auth_token_enc"]:
            token = _vault().decrypt_token(tw["auth_token_enc"])
    except Exception:  # noqa: BLE001 — wrong key/corrupt → clear result
        return [{"action": "twilio", "ok": False,
                 "detail": "stored auth token cannot be decrypted — re-enter it"}]
    if not sid or not token or not tw["from_number"] or not tw["to_numbers"]:
        return [{"action": "twilio", "ok": False,
                 "detail": "incomplete config (sid/token/from/to required)"}]

    text = _alarm_text(alert)
    say = text.replace("&", "and").replace("<", "").replace(">", "")
    results = []
    for to in tw["to_numbers"]:
        if tw["mode"] in ("call", "both"):
            results.append(_twilio_post(
                httpx, sid, token,
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                {"To": to, "From": tw["from_number"],
                 # Inline TwiML: say it three times, no webhook needed.
                 "Twiml": f"<Response><Say loop='3'>{say}</Say></Response>"},
                f"call {to}"))
        if tw["mode"] in ("sms", "both"):
            results.append(_twilio_post(
                httpx, sid, token,
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                {"To": to, "From": tw["from_number"], "Body": text},
                f"sms {to}"))
    return results


def _twilio_post(httpx, sid: str, token: str, url: str, data: dict,
                 label: str) -> dict:
    try:
        resp = httpx.post(url, data=data, auth=(sid, token),
                          timeout=_TWILIO_TIMEOUT_S)
        if resp.status_code in (200, 201):
            return {"action": f"twilio {label}", "ok": True,
                    "detail": f"accepted ({resp.status_code})"}
        detail = f"HTTP {resp.status_code}"
        try:
            detail += f": {resp.json().get('message', '')}"
        except ValueError:
            pass
        return {"action": f"twilio {label}", "ok": False, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"action": f"twilio {label}", "ok": False, "detail": str(exc)}


def _dispatch_webhook(wh: dict, alert: dict) -> dict:
    import httpx

    url = wh["url"]
    if not url:
        return {"action": "webhook", "ok": False, "detail": "no URL configured"}
    try:
        if wh["method"] == "GET":
            resp = httpx.get(url, timeout=_WEBHOOK_TIMEOUT_S)
        else:
            resp = httpx.post(url, json={
                "event": "opennvr.alarm",
                "severity": alert.get("severity"),
                "title": alert.get("title"),
                "description": alert.get("description"),
                "camera_id": alert.get("camera_id"),
                "alert_id": alert.get("alert_id"),
                "fired_at": alert.get("fired_at"),
            }, timeout=_WEBHOOK_TIMEOUT_S)
        ok = 200 <= resp.status_code < 300
        return {"action": "webhook", "ok": ok,
                "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"action": "webhook", "ok": False, "detail": str(exc)}
