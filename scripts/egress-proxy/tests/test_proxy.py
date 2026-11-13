# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The egress proxy, end to end on loopback: a fake core decides, a
fake upstream answers, real clients (http.client) go through CONNECT
and through plain absolute-URI forwarding.

Run with:
    python -m pytest scripts/egress-proxy/tests -q
"""
from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import proxy as px  # noqa: E402


# ── parsing ─────────────────────────────────────────────────────────


def test_parse_connect_and_absolute_and_health():
    r = px.parse_request(b"CONNECT api.telegram.org:443 HTTP/1.1\r\nHost: api.telegram.org:443\r\n\r\n")
    assert (r.method, r.host, r.port) == ("CONNECT", "api.telegram.org", 443)
    r = px.parse_request(b"CONNECT Example.COM. HTTP/1.1\r\n\r\n")
    assert (r.host, r.port) == ("example.com", 443)
    r = px.parse_request(b"GET http://ha.local:8123/api/states?x=1 HTTP/1.1\r\nHost: ha.local:8123\r\n"
                         b"Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic xx\r\n"
                         b"User-Agent: t\r\n\r\n")
    assert (r.method, r.host, r.port) == ("GET", "ha.local", 8123)
    assert r.head == (b"GET /api/states?x=1 HTTP/1.1\r\nHost: ha.local:8123\r\nUser-Agent: t\r\n"
                      b"Connection: close\r\n\r\n")
    r = px.parse_request(b"GET http://x.y HTTP/1.0\r\n\r\n")
    assert r.head.startswith(b"GET / HTTP/1.1\r\nHost: x.y\r\n") and r.port == 80
    assert px.parse_request(b"GET /healthz HTTP/1.1\r\n\r\n").healthz
    for bad in [b"GET /index.html HTTP/1.1\r\n\r\n",          # origin form: not a proxy request
                b"GET https://x.y/ HTTP/1.1\r\n\r\n",          # https must be CONNECT
                b"CONNECT x.y:0 HTTP/1.1\r\n\r\n", b"CONNECT :443 HTTP/1.1\r\n\r\n",
                b"garbage\r\n\r\n", b"\xff\xfe"]:
        assert px.parse_request(bad) is None, bad


# ── policy cache ────────────────────────────────────────────────────


def test_policy_caches_allows_longer_than_denies():
    async def run():
        answers = {"ok.example": True}
        pol = px.Policy(lambda ip, h, p: _decide(answers, h), allow_ttl=60, deny_ttl=0)
        assert (await pol.decide("1.1.1.1", "ok.example", 443)).allowed
        assert (await pol.decide("1.1.1.1", "OK.example", 443)).allowed
        assert pol.asked == 1                                        # cached, case-insensitive
        assert not (await pol.decide("1.1.1.1", "no.example", 443)).allowed
        assert not (await pol.decide("1.1.1.1", "no.example", 443)).allowed
        assert pol.asked == 3                                        # deny ttl 0 → re-asked
        assert (await pol.decide("2.2.2.2", "ok.example", 443)).allowed and pol.asked == 4  # per client

        async def boom(ip, h, p):
            raise RuntimeError("core down")
        pol2 = px.Policy(boom)
        d = await pol2.decide("1.1.1.1", "ok.example", 443)
        assert not d.allowed and "ask failed" in d.reason                # fail closed
    asyncio.run(run())


async def _decide(answers, host):
    return px.Decision(answers.get(host, False), "app-x" if host in answers else None,
                       "listing: " + host if host in answers else "not declared, not allowed")


# ── end to end on loopback ──────────────────────────────────────────


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"path": self.path, "host": self.headers.get("Host"),
                           "conn": self.headers.get("Connection"),
                           "proxy_auth": self.headers.get("Proxy-Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def stack():
    upstream = HTTPServer(("127.0.0.1", 0), _Upstream)
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    loop = asyncio.new_event_loop()
    seen: list[tuple[str, str, int]] = []

    async def ask(client_ip, host, port):
        seen.append((client_ip, host, port))
        allowed = host == "127.0.0.1"                 # any loopback port
        return px.Decision(allowed, "test-app" if allowed else "test-app",
                           "operator: 127.0.0.1" if allowed else "not declared, not allowed")

    proxy = px.Proxy(px.Policy(ask, allow_ttl=30, deny_ttl=30))
    server = loop.run_until_complete(proxy.serve("127.0.0.1", 0))
    proxy_port = server.sockets[0].getsockname()[1]
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        yield proxy_port, up_port, seen
    finally:
        async def _down():
            server.close()
            await server.wait_closed()
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()
            loop.stop()
        asyncio.run_coroutine_threadsafe(_down(), loop)
        upstream.shutdown()


def test_plain_http_is_forwarded_when_allowed(stack):
    proxy_port, up_port, seen = stack
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.request("GET", f"http://127.0.0.1:{up_port}/hello?x=1",
              headers={"Proxy-Authorization": "Basic secret"})
    r = c.getresponse()
    assert r.status == 200
    body = json.loads(r.read())
    assert body["path"] == "/hello?x=1" and body["host"] == f"127.0.0.1:{up_port}"
    assert body["conn"] == "close" and body["proxy_auth"] is None      # proxy headers stripped
    assert seen == [("127.0.0.1", "127.0.0.1", up_port)]


def test_connect_tunnels_bytes_when_allowed(stack):
    proxy_port, up_port, _seen = stack
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.set_tunnel("127.0.0.1", up_port)                                  # CONNECT 127.0.0.1:<up>
    c.request("GET", "/tunnelled")
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["path"] == "/tunnelled"
    c.close()


def test_denied_destinations_get_a_403_that_names_them(stack):
    proxy_port, up_port, seen = stack
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.request("GET", "http://evil.example:8080/x")
    r = c.getresponse()
    assert r.status == 403
    text = r.read().decode()
    assert "test-app may not reach evil.example:8080" in text and "not declared" in text
    # CONNECT to a denied host: the tunnel is refused before any byte flows.
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.set_tunnel("evil.example", 443)
    with pytest.raises(OSError, match="403"):
        c.request("GET", "/")
    assert ("127.0.0.1", "evil.example", 8080) in seen and ("127.0.0.1", "evil.example", 443) in seen


def test_health_and_non_proxy_requests(stack):
    proxy_port, _up, seen = stack
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.request("GET", "/healthz")
    assert c.getresponse().status == 200
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.request("GET", "/index.html")
    r = c.getresponse()
    assert r.status == 400 and b"forward proxy" in r.read()
    assert seen == []                                                   # core never asked


def test_unreachable_upstream_is_a_502(stack):
    proxy_port, _up, _seen = stack
    import socket
    with socket.socket() as probe:                    # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    c = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    c.request("GET", f"http://127.0.0.1:{closed_port}/x")
    r = c.getresponse()
    assert r.status == 502 and b"could not connect" in r.read()
