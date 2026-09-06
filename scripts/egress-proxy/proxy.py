# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""OpenNVR egress proxy — the only door out of the apps network.

Apps run on ``opennvr_apps``, an *internal* compose network with no
route to the LAN or the internet. This service sits on that network
and on the platform network, and is the ``HTTP_PROXY`` / ``HTTPS_PROXY``
every app container receives. For each request it asks core whether
the calling app may open the destination:

    POST {OPENNVR_URL}/api/v1/apps/egress/check
         {"client_ip": ..., "host": ..., "port": ...}
         → {"allowed": bool, "app_id": ..., "reason": ...}

Core identifies the app by its address (the one its contract URL
resolves to) and answers from the listing's declared hosts plus the
operator's allow list; a refusal is logged, counted and raised in the
inbox by core. This proxy holds no policy of its own — it only
remembers answers briefly so a chatty app does not ask core per byte.

Fail closed: no answer from core, or an unknown client, is a 403.

Wire behaviour: ``CONNECT host:port`` (every HTTPS call) becomes a
plain TCP tunnel once allowed — TLS passes through untouched, the
proxy never sees plaintext. Plain-HTTP absolute-URI requests
(``GET http://host/path``) are forwarded with the request line
rewritten to origin form. ``GET /healthz`` answers the container
health check. Nothing else is served.

Standard library only; one file; injectable ``ask`` for tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger("opennvr.egress-proxy")

MAX_HEAD = 16 * 1024
CONNECT_TIMEOUT_S = float(os.environ.get("EGRESS_CONNECT_TIMEOUT_S", "10"))
CACHE_ALLOW_S = float(os.environ.get("EGRESS_CACHE_ALLOW_S", "30"))
CACHE_DENY_S = float(os.environ.get("EGRESS_CACHE_DENY_S", "5"))
ASK_TIMEOUT_S = float(os.environ.get("EGRESS_ASK_TIMEOUT_S", "5"))

_REQUEST_LINE = re.compile(r"^([A-Z]+) (\S+) HTTP/1\.[01]$")
_HOST_CHARS = re.compile(r"^[A-Za-z0-9.\-]+$")


@dataclass
class Decision:
    allowed: bool
    app_id: str | None
    reason: str


Ask = Callable[[str, str, int], Awaitable[Decision]]


# ── asking core ─────────────────────────────────────────────────────


def _ask_core_blocking(base_url: str, key: str, client_ip: str, host: str, port: int) -> Decision:
    body = json.dumps({"client_ip": client_ip, "host": host, "port": port}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/apps/egress/check", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Internal-Api-Key": key},
    )
    # No env proxies for this call: core is on the platform network.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=ASK_TIMEOUT_S) as resp:  # noqa: S310 — fixed core URL
            data = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return Decision(False, None, f"core answered {exc.code}")
    except Exception as exc:  # noqa: BLE001 — any failure is a refusal
        return Decision(False, None, f"core unreachable: {exc.__class__.__name__}")
    return Decision(bool(data.get("allowed")), data.get("app_id"), str(data.get("reason") or ""))


def core_asker(base_url: str, key: str) -> Ask:
    async def ask(client_ip: str, host: str, port: int) -> Decision:
        return await asyncio.to_thread(_ask_core_blocking, base_url, key, client_ip, host, port)
    return ask


class Policy:
    """Cached answers, keyed by (client, host, port)."""

    def __init__(self, ask: Ask, *, allow_ttl: float = CACHE_ALLOW_S, deny_ttl: float = CACHE_DENY_S):
        self._ask = ask
        self._allow_ttl = allow_ttl
        self._deny_ttl = deny_ttl
        self._cache: dict[tuple[str, str, int], tuple[float, Decision]] = {}
        self.asked = 0

    async def decide(self, client_ip: str, host: str, port: int) -> Decision:
        key = (client_ip, host.lower(), port)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        self.asked += 1
        try:
            decision = await self._ask(client_ip, host, port)
        except Exception as exc:  # noqa: BLE001
            decision = Decision(False, None, f"ask failed: {exc.__class__.__name__}")
        ttl = self._allow_ttl if decision.allowed else self._deny_ttl
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = (now + ttl, decision)
        return decision

    def forget(self) -> None:
        self._cache.clear()


# ── request parsing ─────────────────────────────────────────────────


@dataclass
class Request:
    method: str
    host: str
    port: int
    head: bytes            # the (possibly rewritten) head to forward, for plain HTTP
    healthz: bool = False


def split_host_port(text: str, default_port: int) -> tuple[str, int] | None:
    """``host[:port]`` → (host, port); IPv4 / names only (the apps network
    is IPv4), ``None`` for anything else."""
    host, sep, port_text = text.strip().rpartition(":")
    if not sep:
        host, port_text = port_text, ""
    port = int(port_text) if port_text.isdigit() else (default_port if not port_text else 0)
    host = host.lower().rstrip(".")
    if not host or not _HOST_CHARS.match(host) or not 0 < port < 65536:
        return None
    return host, port


def parse_request(head: bytes) -> Request | None:
    """The target of one proxy request head, or ``None`` when it is not
    something this proxy serves."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n")
    m = _REQUEST_LINE.match(lines[0])
    if not m:
        return None
    method, target = m.group(1), m.group(2)
    if method == "CONNECT":
        hp = split_host_port(target, 443)
        return Request(method, hp[0], hp[1], b"") if hp else None
    if target == "/healthz":
        return Request(method, "", 0, b"", healthz=True)
    if "://" not in target:
        return None                       # origin-form: not a proxy request
    parts = urlsplit(target)
    if parts.scheme != "http" or not parts.hostname:
        return None                       # https:// must arrive as CONNECT
    port = parts.port or 80
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    out = [f"{method} {path} HTTP/1.1"]
    saw_host = False
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name in ("proxy-connection", "proxy-authorization", "connection", "keep-alive"):
            continue
        if name == "host":
            saw_host = True
        out.append(line)
    if not saw_host:
        out.append(f"Host: {parts.netloc}")
    out.append("Connection: close")
    return Request(method, parts.hostname.lower(), port, ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))


# ── the server ──────────────────────────────────────────────────────


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            pass


def _reply(writer: asyncio.StreamWriter, status: str, body: str = "") -> None:
    data = body.encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
    )


class Proxy:
    def __init__(self, policy: Policy):
        self.policy = policy

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        client_ip = str(peer[0])
        upstream_w: asyncio.StreamWriter | None = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                _reply(writer, "400 Bad Request", "OpenNVR egress: malformed request")
                return
            if len(head) > MAX_HEAD:
                _reply(writer, "431 Request Header Fields Too Large")
                return
            req = parse_request(head)
            if req is None:
                _reply(writer, "400 Bad Request",
                       "OpenNVR egress: this is a forward proxy — send CONNECT host:port "
                       "or an absolute http:// URL")
                return
            if req.healthz:
                _reply(writer, "200 OK", "ok")
                return
            decision = await self.policy.decide(client_ip, req.host, req.port)
            if not decision.allowed:
                logger.warning("DENY %s (%s) → %s:%d — %s", client_ip, decision.app_id or "unknown app",
                               req.host, req.port, decision.reason)
                _reply(writer, "403 Forbidden",
                       f"OpenNVR egress: {decision.app_id or 'this app'} may not reach "
                       f"{req.host}:{req.port} ({decision.reason}). Declared hosts come from the "
                       "app's catalog listing; an operator can allow more from the App Catalog.")
                return
            logger.info("ALLOW %s (%s) → %s:%d — %s", client_ip, decision.app_id, req.host, req.port,
                        decision.reason)
            try:
                upstream_r, upstream_w = await asyncio.wait_for(
                    asyncio.open_connection(req.host, req.port), timeout=CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as exc:
                _reply(writer, "502 Bad Gateway",
                       f"OpenNVR egress: could not connect to {req.host}:{req.port} ({exc.__class__.__name__})")
                return
            if req.method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                upstream_w.write(req.head)
                await upstream_w.drain()
            await asyncio.gather(_pipe(reader, upstream_w), _pipe(upstream_r, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            for w in (upstream_w, writer):
                if w is None:
                    continue
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

    async def serve(self, host: str, port: int) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD)


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    core_url = os.environ.get("OPENNVR_URL", "http://opennvr-core:8000")
    key = os.environ.get("INTERNAL_API_KEY", "")
    if not key:
        raise SystemExit("INTERNAL_API_KEY is required (the proxy asks core with it)")
    port = int(os.environ.get("LISTEN_PORT", "3128"))
    proxy = Proxy(Policy(core_asker(core_url, key)))
    server = await proxy.serve("0.0.0.0", port)  # noqa: S104 — internal network only
    logger.info("egress proxy listening on :%d, asking %s", port, core_url)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
