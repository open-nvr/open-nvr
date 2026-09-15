# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Compatibility fallback for ONVIF cameras rejecting HTTP Digest in SOAP."""

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

_logging = types.ModuleType("core.logging_config")
_logging.main_logger = types.SimpleNamespace(info=lambda *args, **kwargs: None)
sys.modules.setdefault("core.logging_config", _logging)

from services import onvif_digest_service as ods  # noqa: E402


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


class WsseFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Client.responses = []
        _Client.calls = []
        self.client_patch = patch.object(ods.httpx, "AsyncClient", _Client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_auth_fault_retries_once_with_valid_wsse_password_digest(self):
        nonce_raw = bytes(range(20))
        fault = """<s:Envelope><s:Body><s:Fault>
          <s:Code><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
          <s:Reason><s:Text>Authority failure</s:Text></s:Reason>
        </s:Fault></s:Body></s:Envelope>"""
        _Client.responses = [
            httpx.Response(500, text=fault),
            httpx.Response(200, text="ok"),
        ]

        with patch.object(secrets, "token_bytes", return_value=nonce_raw):
            result = await ods._onvif_request(
                "http://camera/onvif/device_service",
                "<tds:GetCapabilities/>",
                "alice&bob",
                "secret",
            )

        self.assertEqual(result, (200, "ok"))
        self.assertEqual(len(_Client.calls), 2)
        self.assertIsInstance(_Client.calls[0]["auth"], httpx.DigestAuth)
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

    async def test_fallback_requires_an_authenticated_soap_fault(self):
        cases = [
            (200, "<NotAuthorized/>", True),
            (500, "ordinary server failure", True),
            (500, "<s:Fault><s:Text>NotAuthorized</s:Text></s:Fault>", False),
        ]
        for status, text, credentials in cases:
            with self.subTest(status=status, text=text, credentials=credentials):
                _Client.responses = [httpx.Response(status, text=text)]
                _Client.calls = []
                username, password = (
                    ("admin", "secret") if credentials else (None, None)
                )

                result = await ods._onvif_request(
                    "http://camera/onvif", "<request/>", username, password
                )

                self.assertEqual(result, (status, text))
                self.assertEqual(len(_Client.calls), 1)


if __name__ == "__main__":
    unittest.main()
