# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr_app_sdk.egress`` — where the platform's egress proxy is,
for clients that do not read the proxy environment themselves."""
from __future__ import annotations

from opennvr_app_sdk import connect_via_proxy, proxy_address
from opennvr_app_sdk.egress import bypasses, proxy_url


def test_no_proxy_configured(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert proxy_url() is None and proxy_address() is None
    assert connect_via_proxy("broker.local", 1883) is None
    assert bypasses("anything") is False


def test_platform_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "opennvr-core,nats,nats-apps,egress-proxy,localhost,127.0.0.1")
    assert proxy_address() == ("egress-proxy", 3128)
    assert proxy_address("http") == ("egress-proxy", 3128)
    assert connect_via_proxy("192.168.1.20", 1883) == ("egress-proxy", 3128)
    assert connect_via_proxy("opennvr-core", 8000) is None            # NO_PROXY: direct
    assert connect_via_proxy("NATS.", 4222) is None


def test_https_falls_back_to_http_and_empty_means_off(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "egress-proxy:3129")           # scheme-less is accepted
    assert proxy_address() == ("egress-proxy", 3129)
    monkeypatch.setenv("HTTP_PROXY", "")                             # operator switched it off
    assert proxy_address() is None


def test_no_proxy_suffix_matching(monkeypatch):
    monkeypatch.setenv("NO_PROXY", ".internal, example.com:80 ")
    assert bypasses("svc.internal") and bypasses("internal") and not bypasses("xinternal")
    assert bypasses("example.com") and bypasses("a.example.com") and not bypasses("notexample.com")
    assert not bypasses("whatever")
    monkeypatch.setenv("NO_PROXY", "*")
    assert bypasses("whatever")
    monkeypatch.setenv("NO_PROXY", "")
    assert not bypasses("whatever")
