# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Compatibility fallback for ONVIF cameras rejecting HTTP Digest in SOAP.

Covers ``_onvif_request``'s two authentication schemes: HTTP Digest first,
a WS-Security UsernameToken retry on an ONVIF auth fault, the per-host
memory that makes later calls to such a camera start with WS-Security,
and the guard rails — no retry without credentials, no retry for a fault
that is not about authentication, never more than two requests.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import httpx

_SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVER))

# Same catch-all logger stub the sibling suites install (test_camera_settings
# and friends): the module under test calls main_logger.info AND .warning,
# and whichever test module imports services.onvif_digest_service first
# decides which stub every later module sees.
_lm = types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from services import onvif_digest_service as ods  # noqa: E402

AUTH_FAULT = """<s:Envelope><s:Body><s:Fault>
  <s:Code><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
  <s:Reason><s:Text>Authority failure</s:Text></s:Reason>
</s:Fault></s:Body></s:Envelope>"""

OTHER_FAULT = """<s:Envelope><s:Body><s:Fault>
  <s:Code><s:Subcode><s:Value>ter:ActionNotSupported</s:Value></s:Subcode></s:Code>
</s:Fault></s:Body></s:Envelope>"""

URL = "http://camera/onvif/device_service"


class _Client:
    responses: ClassVar[list[httpx.Response]] = []
    calls: ClassVar[list[dict]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _is_wsse(call: dict) -> bool:
    return "<wsse:Security" in call["content"]


def _is_digest(call: dict) -> bool:
    return isinstance(call.get("auth"), httpx.DigestAuth) and not _is_wsse(call)


class WsseFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Client.responses = []
        _Client.calls = []
        ods._WSSE_PREFERRED_HOSTS.clear()
        self.addCleanup(ods._WSSE_PREFERRED_HOSTS.clear)
        self.client_patch = patch.object(ods.httpx, "AsyncClient", _Client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_auth_fault_retries_once_with_valid_wsse_password_digest(self):
        nonce_raw = bytes(range(20))
        _Client.responses = [
            httpx.Response(500, text=AUTH_FAULT),
            httpx.Response(200, text="ok"),
        ]

        with patch.object(secrets, "token_bytes", return_value=nonce_raw):
            result = await ods._onvif_request(
                URL, "<tds:GetCapabilities/>", "alice&bob", "secret"
            )

        self.assertEqual(result, (200, "ok"))
        self.assertEqual(len(_Client.calls), 2)
        self.assertTrue(_is_digest(_Client.calls[0]))
        self.assertNotIn("auth", _Client.calls[1])
        envelope = _Client.calls[1]["content"]
        self.assertIn("<wsse:Username>alice&amp;bob</wsse:Username>", envelope)
        self.assertIn("<tds:GetCapabilities/>", envelope)
        created = re.search(r"<wsu:Created>([^<]+)</wsu:Created>", envelope).group(1)
        digest = re.search(
            r"<wsse:Password[^>]*>([^<]+)</wsse:Password>", envelope
        ).group(1)
        expected = base64.b64encode(
            hashlib.sha1(nonce_raw + created.encode() + b"secret").digest()
        ).decode()
        self.assertEqual(digest, expected)

    async def test_fault_detection_is_decided_by_the_body_not_the_status(self):
        """SOAP 1.2 wants faults on HTTP 400/500, but some firmware sends
        them with 200 — and ``NotAuthorized`` inside a ``<Fault>`` means
        the same thing either way. The WS-Security fault codes count too."""
        cases = [
            (200, AUTH_FAULT),
            (400, AUTH_FAULT),
            (500, "<s:Fault><s:Value>wsse:FailedAuthentication</s:Value></s:Fault>"),
            (500, "<s:Fault><s:Value>wsse:InvalidSecurityToken</s:Value></s:Fault>"),
        ]
        for status, text in cases:
            with self.subTest(status=status, text=text[:40]):
                ods._WSSE_PREFERRED_HOSTS.clear()
                _Client.responses = [
                    httpx.Response(status, text=text),
                    httpx.Response(200, text="ok"),
                ]
                _Client.calls = []

                result = await ods._onvif_request(URL, "<r/>", "admin", "secret")

                self.assertEqual(result, (200, "ok"))
                self.assertEqual(len(_Client.calls), 2)
                self.assertTrue(_is_wsse(_Client.calls[1]))

    async def test_no_retry_without_credentials_or_without_an_auth_fault(self):
        cases = [
            # No <Fault> element at all — "NotAuthorized" alone is not a fault.
            (200, "<NotAuthorized/>", True),
            (500, "ordinary server failure", True),
            # A real SOAP fault that is not about authentication.
            (500, OTHER_FAULT, True),
            # An auth fault, but nothing to retry with.
            (500, AUTH_FAULT, False),
        ]
        for status, text, credentials in cases:
            with self.subTest(status=status, text=text[:40], credentials=credentials):
                _Client.responses = [httpx.Response(status, text=text)]
                _Client.calls = []
                username, password = (
                    ("admin", "secret") if credentials else (None, None)
                )

                result = await ods._onvif_request(URL, "<r/>", username, password)

                self.assertEqual(result, (status, text))
                self.assertEqual(len(_Client.calls), 1)
                self.assertEqual(ods._WSSE_PREFERRED_HOSTS, set())

    async def test_second_auth_fault_is_returned_as_is_and_host_not_remembered(self):
        """Both schemes refused: at most two requests, the second fault
        goes back to the caller, and the host is NOT marked as
        WS-Security-preferring (it accepted neither)."""
        _Client.responses = [
            httpx.Response(500, text=AUTH_FAULT),
            httpx.Response(500, text=AUTH_FAULT),
        ]

        result = await ods._onvif_request(URL, "<r/>", "admin", "wrong")

        self.assertEqual(result, (500, AUTH_FAULT))
        self.assertEqual(len(_Client.calls), 2)
        self.assertEqual(ods._WSSE_PREFERRED_HOSTS, set())

    async def test_host_that_accepted_wsse_gets_wsse_first_next_time(self):
        """The PTZ case: after one successful fallback, later calls to any
        service on the same host (device, media, PTZ share the netloc)
        are a single WS-Security request, not a rejected Digest round
        trip plus a retry."""
        _Client.responses = [
            httpx.Response(500, text=AUTH_FAULT),
            httpx.Response(200, text="profiles"),
        ]
        await ods._onvif_request(URL, "<trt:GetProfiles/>", "admin", "secret")
        self.assertEqual(ods._WSSE_PREFERRED_HOSTS, {"http://camera"})

        _Client.responses = [httpx.Response(200, text="moved")]
        _Client.calls = []
        result = await ods._onvif_request(
            "http://camera/onvif/ptz_service", "<tptz:ContinuousMove/>",
            "admin", "secret",
        )

        self.assertEqual(result, (200, "moved"))
        self.assertEqual(len(_Client.calls), 1)
        self.assertTrue(_is_wsse(_Client.calls[0]))
        self.assertNotIn("auth", _Client.calls[0])

    async def test_memory_is_per_host_and_scheme(self):
        ods._WSSE_PREFERRED_HOSTS.add("http://camera")
        _Client.responses = [httpx.Response(200, text="ok")]

        await ods._onvif_request(
            "https://camera/onvif/device_service", "<r/>", "admin", "secret"
        )

        self.assertTrue(_is_digest(_Client.calls[0]))

    async def test_remembered_host_that_rejects_wsse_falls_back_to_digest(self):
        """The mirror of the primary fallback: a remembered host whose
        firmware now wants Digest gets one Digest retry and is forgotten,
        so the memory corrects itself rather than sticking forever."""
        ods._WSSE_PREFERRED_HOSTS.add("http://camera")
        _Client.responses = [
            httpx.Response(500, text=AUTH_FAULT),
            httpx.Response(200, text="ok"),
        ]

        result = await ods._onvif_request(URL, "<r/>", "admin", "secret")

        self.assertEqual(result, (200, "ok"))
        self.assertEqual(len(_Client.calls), 2)
        self.assertTrue(_is_wsse(_Client.calls[0]))
        self.assertTrue(_is_digest(_Client.calls[1]))
        self.assertEqual(ods._WSSE_PREFERRED_HOSTS, set())

    async def test_unauthenticated_probe_ignores_the_memory(self):
        """GetSystemDateAndTime is sent without credentials (it is the
        ONVIF-presence probe); a remembered host must not turn it into a
        WS-Security request with an empty token."""
        ods._WSSE_PREFERRED_HOSTS.add("http://camera")
        _Client.responses = [httpx.Response(200, text="ok")]

        await ods._onvif_request(URL, "<tds:GetSystemDateAndTime/>", None, None)

        call = _Client.calls[0]
        self.assertFalse(_is_wsse(call))
        self.assertIsNone(call["auth"])


if __name__ == "__main__":
    unittest.main()
