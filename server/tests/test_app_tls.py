# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Core trusts an app's self-signed certificate only when it is mounted
under the app-certs directory — and still checks the hostname."""
from __future__ import annotations

import datetime as _dt
import ipaddress
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from services import app_tls


@pytest.fixture(autouse=True)
def _fresh():
    app_tls.reset_for_tests()
    yield
    app_tls.reset_for_tests()


def _self_signed(tmp_path, san_names):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "opennvr-camera-agent")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(n) for n in san_names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    d = tmp_path / "camera-agent"
    d.mkdir()
    (d / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "server.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return d


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","ready":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def tls_app(tmp_path):
    certdir = _self_signed(tmp_path, ["camera-agent", "localhost"])
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certdir / "server.crt"), str(certdir / "server.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield tmp_path, f"https://localhost:{srv.server_address[1]}"
    srv.shutdown()


def test_no_trust_dir_means_default_verification(tmp_path):
    assert app_tls.app_verify(tmp_path / "missing") is True
    (tmp_path / "empty").mkdir()
    assert app_tls.app_verify(tmp_path / "empty") is True


def test_self_signed_app_fails_by_default_and_verifies_with_the_mounted_cert(tls_app):
    trust_dir, url = tls_app
    # default trust store: the catalog's old behaviour — "unreachable"
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{url}/health", verify=True, timeout=3, trust_env=False)
    # with the app's certificate directory mounted: verified
    verify = app_tls.app_verify(trust_dir)
    assert isinstance(verify, ssl.SSLContext)
    r = httpx.get(f"{url}/health", verify=verify, timeout=3, trust_env=False)
    assert r.status_code == 200 and r.json()["ready"] is True


def test_hostname_must_still_match_the_san(tls_app):
    trust_dir, url = tls_app
    port = url.rsplit(":", 1)[1]
    verify = app_tls.app_verify(trust_dir)
    # 127.0.0.1 is in the SAN, "127.0.0.1" ok; a name that is not in the SAN is refused
    assert httpx.get(f"https://127.0.0.1:{port}/health", verify=verify, timeout=3, trust_env=False).status_code == 200
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"https://localhost.localdomain:{port}/health", verify=verify, timeout=3, trust_env=False)


def test_context_is_rebuilt_when_the_directory_changes(tmp_path):
    d = _self_signed(tmp_path, ["a"])
    first = app_tls.app_verify(tmp_path)
    assert app_tls.app_verify(tmp_path) is first          # cached
    (d / "server.crt").write_bytes((d / "server.crt").read_bytes() + b"\n")
    assert app_tls.app_verify(tmp_path) is not first      # mtime changed → rebuilt


def test_unparseable_file_is_ignored_not_fatal(tmp_path):
    (tmp_path / "junk.crt").write_text("not a certificate")
    verify = app_tls.app_verify(tmp_path)
    assert isinstance(verify, ssl.SSLContext)
