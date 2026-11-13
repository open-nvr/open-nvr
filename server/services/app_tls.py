# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""How core verifies an app's TLS certificate when it calls the app.

Apps normally serve their contract (``/health``, ``/state``, ``/actions``,
``/ui``) over plain HTTP on the compose network. An app that terminates
TLS itself — the OpenNVR Agent does, so the operator's browser can use
the microphone from a LAN device — presents a self-signed certificate
that no CA bundle knows, and every probe from core failed verification:
the catalog showed it "unreachable" while it answered fine.

Trust is explicit and file-based: the compose overlay that runs such an
app mounts its certificate directory read-only into core under
``settings.app_trusted_certs_dir``; every ``*.crt`` / ``*.pem`` found
there (one level of subdirectories, one per app) is added to the
system trust store for app calls only. Nothing else is bypassed — the
hostname must still match the certificate's SAN (``DNS:camera-agent``),
which is why the agent registers with its service name.

Cheap to call per request: the bundle is rebuilt only when the set of
files or their mtimes changes.
"""
from __future__ import annotations

import logging
import os
import ssl
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "ctx": None}


def trusted_cert_files(directory: str | os.PathLike | None) -> list[Path]:
    if not directory:
        return []
    root = Path(directory)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for pattern in ("*.crt", "*.pem", "*/*.crt", "*/*.pem"):
        found.extend(p for p in root.glob(pattern) if p.is_file())
    return sorted(set(found))


def _fingerprint(files: list[Path]) -> tuple:
    out = []
    for p in files:
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(out)


def app_verify(directory: str | os.PathLike | None = None) -> Any:
    """The ``verify=`` argument for an httpx client that talks to apps:
    ``True`` (the default trust store) when no app certificates are
    mounted, otherwise an SSL context that trusts them as well."""
    if directory is None:
        from core.config import settings

        directory = getattr(settings, "app_trusted_certs_dir", "") or ""
    files = trusted_cert_files(directory)
    if not files:
        return True
    key = _fingerprint(files)
    with _lock:
        if _cache["key"] == key and _cache["ctx"] is not None:
            return _cache["ctx"]
        ctx = ssl.create_default_context()
        loaded = 0
        for p in files:
            try:
                ctx.load_verify_locations(cafile=str(p))
                loaded += 1
            except (ssl.SSLError, OSError) as exc:
                logger.warning("app TLS: ignoring %s: %s", p, exc)
        if loaded:
            logger.info("app TLS: trusting %d app certificate(s) from %s", loaded, directory)
        _cache["key"] = key
        _cache["ctx"] = ctx
        return ctx


def reset_for_tests() -> None:
    with _lock:
        _cache["key"] = None
        _cache["ctx"] = None
