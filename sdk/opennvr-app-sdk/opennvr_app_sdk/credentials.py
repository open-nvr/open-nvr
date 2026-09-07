# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The credential an app presents to OpenNVR core — and where it lives.

An app has, in order of preference:

1. **its own app key** (``oak_<app-id>_…``), minted by core the first
   time the app registers and handed back once. It authenticates the
   app *as itself*: its own config and live state, the cameras the
   operator assigned to it, nothing else. Persisted to a small file
   (``OPENNVR_APP_KEY_FILE``, default ``.opennvr/app.key`` under the
   working directory — mount a volume there to survive restarts) or
   supplied outright by the operator in ``OPENNVR_APP_KEY``;
2. the deployment's **site key** (``OPENNVR_INTERNAL_API_KEY`` or the
   config's ``opennvr_token``) — the bootstrap credential: an app that
   holds no key of its own registers with it and asks core for one
   (``wants_key``), after which every call switches to the app key.

Every SDK client that talks to core (registration, the config poll,
camera discovery, the events store) takes its headers from
:func:`auth_headers` so a rotation lands everywhere at once.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from typing import Any

logger = logging.getLogger("opennvr.app.credentials")

APP_KEY_PREFIX = "oak_"
DEFAULT_KEY_FILE = ".opennvr/app.key"


def key_file() -> Path:
    return Path(os.environ.get("OPENNVR_APP_KEY_FILE") or DEFAULT_KEY_FILE)


def stored_app_key() -> str | None:
    """The persisted app key, if any (env ``OPENNVR_APP_KEY`` wins)."""
    env = (os.environ.get("OPENNVR_APP_KEY") or "").strip()
    if env:
        return env
    try:
        text = key_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def store_app_key(key: str) -> bool:
    """Persist a freshly issued key (0600). ``False`` when the location
    is not writable — the app keeps working from memory for this run
    and asks for a new key next boot."""
    path = key_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning(
            "could not persist the app key to %s (%s) — set "
            "OPENNVR_APP_KEY_FILE to a writable path or mount a volume; "
            "the app will request a fresh key on every start", path, exc)
        return False
    return True


# ── The apps bus ───────────────────────────────────────────────────────
#
# Since api_version 1.3 core tells a registering app where to join the
# event bus with ITS OWN key (``registry.bus = {url, auth: "app_key"}``):
# user = the app id, password = the app key, on the ``nats-apps`` leaf
# server whose permissions are derived from the manifest. Remembered next
# to the key so a restart joins the right bus before re-registering.


def bus_file() -> Path:
    return key_file().with_name(key_file().name + ".bus")


def stored_bus_url() -> str | None:
    env = (os.environ.get("OPENNVR_APP_BUS_URL") or "").strip()
    if env:
        return env
    try:
        text = bus_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def store_bus_url(url: str) -> None:
    path = bus_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("could not persist the apps-bus URL to %s (%s)", path, exc)


def forget_bus_url() -> None:
    try:
        bus_file().unlink()
    except OSError:
        pass


def app_id_from_key(key: str | None) -> str | None:
    """``oak_<app-id>_<hex>`` → ``<app-id>``; the bus user name."""
    if not key or not is_app_key(key):
        return None
    body = key[len(APP_KEY_PREFIX):]
    app_id, sep, tail = body.rpartition("_")
    return app_id if sep and app_id else None


_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def _with_userinfo(url: str, userinfo: str) -> str | None:
    """``nats://host:port`` → ``nats://<userinfo>@host:port`` (nats-py reads
    per-server credentials from the URI); ``None`` when the URL already
    carries userinfo or the credential is not URL-safe verbatim."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname or parts.username is not None:
        return None
    return urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def bus_connection(credentials: "AppCredentials | None", fallback_url: str | None,
                   fallback_token: str | None) -> dict[str, Any]:
    """nats.connect() kwargs for this app.

    With an app key AND a bus URL from core: the apps bus as
    user=<app id> / password=<key> — and, when the configured
    ``nats_url`` (+ ``nats_token``) is also known, that platform bus as
    the **second server in the pool** with the site token in its URI, so
    a bus that cannot be resolved or reached (``nats-apps`` down, an
    older compose file, a stack half-upgraded) fails over instead of
    leaving the app reconnecting forever with no alerts. nats-py tries
    the pool in order (``dont_randomize``) and takes each server's
    credentials from its URI. Otherwise the configured URL (+ token),
    as before — an older core, or a bare dev run.
    """
    creds = credentials or AppCredentials()
    bus = creds.bus_url
    user = app_id_from_key(creds.app_key)
    if bus and user and creds.app_key:
        primary = _with_userinfo(bus, f"{user}:{creds.app_key}") if _URL_SAFE_RE.match(creds.app_key) else None
        secondary = None
        if fallback_url and fallback_url != bus and fallback_token and _URL_SAFE_RE.match(fallback_token):
            secondary = _with_userinfo(fallback_url, fallback_token)
        if primary and secondary:
            return {"servers": [primary, secondary], "dont_randomize": True}
        return {"servers": [bus], "user": user, "password": creds.app_key}
    out: dict[str, Any] = {"servers": [fallback_url] if fallback_url else []}
    if fallback_token:
        out["token"] = fallback_token
    return out


def describe_connection(kwargs: dict[str, Any]) -> str:
    """One line for the log: where and as whom (never the secret)."""
    servers = kwargs.get("servers") or []
    shown = [redact_url(s) for s in servers]
    if kwargs.get("user"):
        who = f"as {kwargs['user']}"
    elif kwargs.get("token"):
        who = "with the site token"
    elif len(servers) == 2:
        who = "as the app, site-token fallback second"
    else:
        who = "anonymous"
    return f"{shown} {who}"


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    who = parts.username if parts.password is not None else "<token>"
    return urlunsplit((parts.scheme, f"{who}:***@{host}" if parts.password is not None else f"{who}@{host}",
                       parts.path, parts.query, parts.fragment))


def connected_via_fallback(nc: Any, kwargs: dict[str, Any]) -> bool:
    """After ``nats.connect``: True when the pool had a fallback and the
    client landed on it rather than on the apps bus."""
    servers = kwargs.get("servers") or []
    if len(servers) < 2:
        return False
    try:
        current = nc.connected_url
    except Exception:  # noqa: BLE001
        return False
    if current is None:
        return False
    return (current.hostname, current.port) == (urlsplit(servers[1]).hostname, urlsplit(servers[1]).port)


def forget_app_key() -> None:
    """Drop the persisted key (after a 401 — it was rotated or revoked)."""
    try:
        key_file().unlink()
    except OSError:
        pass


def site_key(explicit: str | None = None) -> str | None:
    """The deployment's ``INTERNAL_API_KEY`` as this app knows it."""
    if explicit:
        return str(explicit)
    key = (os.environ.get("OPENNVR_INTERNAL_API_KEY") or "").strip()
    return key or None


def is_app_key(value: str | None) -> bool:
    return bool(value) and str(value).startswith(APP_KEY_PREFIX)


class AppCredentials:
    """Resolve, remember and rotate the app's credential.

    ``explicit`` is the config's ``opennvr_token`` (either kind).
    """

    def __init__(self, explicit: str | None = None) -> None:
        self._explicit = str(explicit) if explicit else None
        self._app_key: str | None = None
        if is_app_key(self._explicit):
            self._app_key = self._explicit
        else:
            self._app_key = stored_app_key()

    @property
    def app_key(self) -> str | None:
        return self._app_key

    @property
    def has_app_key(self) -> bool:
        return bool(self._app_key)

    @property
    def bus_url(self) -> str | None:
        """Where to join the event bus with the app key (core told us)."""
        return stored_bus_url()

    @property
    def app_id(self) -> str | None:
        return app_id_from_key(self._app_key)

    def adopt_bus(self, url: str) -> None:
        if url and url != stored_bus_url():
            store_bus_url(url)
            logger.info("apps bus at %s — joining NATS as this app, not with the site key", url)

    def token(self) -> str | None:
        """What to send: the app key when we have one, else the site key."""
        return self._app_key or site_key(
            None if is_app_key(self._explicit) else self._explicit)

    def headers(self) -> dict[str, str]:
        """Both header shapes core accepts, so one value works whether
        it is an app key, the site key or a user JWT."""
        tok = self.token()
        if not tok:
            return {}
        return {"Authorization": f"Bearer {tok}", "X-Internal-Api-Key": tok}

    def adopt(self, key: str) -> None:
        """A key just issued by core: use it from now on and persist it."""
        self._app_key = key.strip()
        store_app_key(self._app_key)
        logger.info("app key issued by OpenNVR — using it for every core call")

    def invalidate(self) -> None:
        """Core refused our app key (rotated/revoked): fall back to the
        site key so the next registration can ask for a new one."""
        if self._app_key:
            logger.warning("OpenNVR rejected the app key — discarding it; "
                           "will request a new one at next registration")
        self._app_key = None
        forget_app_key()
        forget_bus_url()


def auth_headers(explicit: str | None = None) -> dict[str, str]:
    """One-shot helper for clients that don't hold an AppCredentials."""
    return AppCredentials(explicit).headers()
