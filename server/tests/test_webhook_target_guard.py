# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Integration webhooks: metadata denied, allowlist honoured.

Reported by Kamal Sentassi (S9S Security Research), coordinated
disclosure, 2026 (item 2c): the webhook sender had no host allowlist and
no gate, so a superuser could aim it at any address.

Unlike the camera/ONVIF probes, a webhook is MEANT to leave the network,
so this is not the same guard inverted. Cloud metadata is refused
outright — no webhook targets it, and it returns cloud credentials — and
an operator who wants a tighter posture sets an explicit allowlist.
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.config import settings  # noqa: E402
from services.integration_service import _webhook_target_refusal  # noqa: E402


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "", raising=False)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/",
    "http://169.254.170.2/v2/credentials",
])
def test_cloud_metadata_is_refused(url):
    assert _webhook_target_refusal(url) is not None


@pytest.mark.parametrize("url", [
    "https://hooks.slack.com/services/T/B/x",
    "https://example.com/webhook",
    "http://ntfy.local/topic",
    "http://192.168.1.50:8123/api/webhook/abc",
])
def test_ordinary_targets_are_allowed_when_no_allowlist_is_set(url):
    """Default posture must not break working integrations — including
    on-prem ones, which this product explicitly supports."""
    assert _webhook_target_refusal(url) is None


@pytest.mark.parametrize("url", [
    "ftp://example.com/x", "file:///etc/passwd", "gopher://x/", "not-a-url",
])
def test_non_http_schemes_are_refused(url):
    assert _webhook_target_refusal(url) is not None


def test_allowlist_is_enforced_when_set(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allowed_hosts",
                        "hooks.slack.com, ntfy.local", raising=False)
    assert _webhook_target_refusal("https://hooks.slack.com/x") is None
    assert _webhook_target_refusal("http://ntfy.local/t") is None
    assert _webhook_target_refusal("https://evil.example/x") is not None


def test_allowlist_does_not_re_admit_metadata(monkeypatch):
    """Metadata is denied before the allowlist is consulted, so an
    operator cannot allow it back by accident."""
    monkeypatch.setattr(settings, "webhook_allowed_hosts",
                        "169.254.169.254", raising=False)
    assert _webhook_target_refusal("http://169.254.169.254/") is not None
