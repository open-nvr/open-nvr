# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound mail, for the one thing the core needs it for today: a
password-reset link. Settings live in the site settings table under
``mail.smtp`` (the password encrypted at rest); nothing is sent unless
an operator configured a server. Standard library ``smtplib`` on a
worker thread — one message at a time is all this ever does.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from services import site_settings

logger = logging.getLogger("opennvr.mail")

MAIL_KEY = "mail.smtp"
SECURITY = ("none", "starttls", "tls")


def load(db) -> dict[str, Any]:
    """The stored settings, password decrypted, with defaults filled."""
    raw = site_settings.get_json(db, MAIL_KEY, {}) or {}
    out = {
        "host": str(raw.get("host") or "").strip(),
        "port": int(raw.get("port") or 587),
        "security": raw.get("security") if raw.get("security") in SECURITY else "starttls",
        "username": str(raw.get("username") or ""),
        "password": "",
        "from_address": str(raw.get("from_address") or "").strip(),
        "from_name": str(raw.get("from_name") or "OpenNVR").strip() or "OpenNVR",
    }
    enc = raw.get("password_enc")
    if enc:
        try:
            from utils.encryption import decrypt_value

            out["password"] = decrypt_value(enc)
        except Exception:
            logger.warning("mail: stored SMTP password could not be decrypted")
    return out


def save(db, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the stored settings. A blank password keeps the
    stored one; ``None`` clears it."""
    raw = site_settings.get_json(db, MAIL_KEY, {}) or {}
    for key in ("host", "username", "from_address", "from_name"):
        if key in patch and patch[key] is not None:
            raw[key] = str(patch[key]).strip()
    if "port" in patch and patch["port"] is not None:
        raw["port"] = max(1, min(65535, int(patch["port"])))
    if patch.get("security") in SECURITY:
        raw["security"] = patch["security"]
    if "password" in patch:
        if patch["password"] is None:
            raw.pop("password_enc", None)
        elif str(patch["password"]):
            from utils.encryption import encrypt_value

            raw["password_enc"] = encrypt_value(str(patch["password"]))
    site_settings.set_json(db, MAIL_KEY, raw)
    return public(db)


def public(db) -> dict[str, Any]:
    """The settings as the UI shows them: no password, only whether one is set."""
    cfg = load(db)
    return {**{k: v for k, v in cfg.items() if k != "password"},
            "has_password": bool(cfg["password"]), "configured": is_configured(cfg)}


def is_configured(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("host")) and bool(cfg.get("from_address"))


def _send_sync(cfg: dict[str, Any], to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["from_address"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    timeout = 20
    if cfg["security"] == "tls":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
    try:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        server.send_message(msg)
    finally:
        with contextlib.suppress(Exception):
            server.quit()


async def send(db, to: str, subject: str, text: str) -> None:
    """Send one message. Raises on failure (callers decide what to tell
    the user; the forgot-password route tells them nothing)."""
    cfg = load(db)
    if not is_configured(cfg):
        raise RuntimeError("mail is not configured")
    await asyncio.to_thread(_send_sync, cfg, to, subject, text)
