"""The scopes a token may hold are written down in four places.

``api_tokens.ALLOWED_SCOPES`` is the one with authority — it refuses
anything else at issue time. But the checkbox list the operator actually
sees lives in ``app/src/views/settings/ApiTokens.tsx``, hand-maintained,
under a comment that says it mirrors the server; each scope needs a
label in both translation catalogues to render as anything but its own
key; and ``entity_descriptors.DESCRIPTOR_SCOPES`` names the subset a
descriptor may demand.

Nothing joined them up. ``apps.view`` was added to the server for
app-declared entities and never reached the form, so the scope existed,
gated real reads, and could not be granted through the UI at all — you
could only get it by calling the API by hand. That is a worse failure
than a missing label, because it is silent on both sides: the server
never sees a request for it and the operator never sees the box.

The rule these tests encode: a scope is not shipped until every one of
the four lists knows about it.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_SERVER = Path(__file__).resolve().parents[1]
_REPO = _SERVER.parent
sys.path.insert(0, str(_SERVER))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_scopes_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

_TOKENS_VIEW = _REPO / "app" / "src" / "views" / "settings" / "ApiTokens.tsx"
_LOCALES = _REPO / "app" / "src" / "locales"

_SCOPES_BLOCK = re.compile(r"const SCOPES = \[(.*?)\] as const", re.S)
_QUOTED = re.compile(r"'([a-z_]+\.[a-z_]+)'")
_KEY = re.compile(r"'([A-Za-z0-9_.\-]+)'\s*:")

_LANGUAGES = ("en", "fr")


def _form_scopes() -> list[str]:
    block = _SCOPES_BLOCK.search(_TOKENS_VIEW.read_text())
    assert block, "could not find the SCOPES list in ApiTokens.tsx"
    return _QUOTED.findall(block.group(1))


def _allowed_scopes() -> set[str]:
    from services.api_tokens import ALLOWED_SCOPES

    return set(ALLOWED_SCOPES)


def test_the_form_offers_every_scope_the_server_allows():
    """A scope missing here cannot be granted through the UI at all, and
    nothing anywhere says so — this is how apps.view went missing."""
    missing = sorted(_allowed_scopes() - set(_form_scopes()))
    assert missing == [], (
        f"the server allows {missing} but the token form does not offer "
        "them, so an operator cannot grant them")


def test_the_form_offers_nothing_the_server_will_refuse():
    """The opposite drift: a checkbox that produces a token the server
    silently strips, which looks to the operator like it worked."""
    extra = sorted(set(_form_scopes()) - _allowed_scopes())
    assert extra == [], (
        f"the token form offers {extra}, which ALLOWED_SCOPES will refuse")


def test_the_form_lists_each_scope_once():
    seen = _form_scopes()
    dupes = sorted({s for s in seen if seen.count(s) > 1})
    assert dupes == [], f"the token form lists {dupes} twice"


@pytest.mark.parametrize("language", _LANGUAGES)
def test_every_scope_has_a_label(language):
    """The label is looked up as t(`apiTokens.scope.${s}`) — a computed
    key, which the catalogue guard cannot check. Without a label the
    checkbox is captioned with the key itself."""
    catalogue = set(_KEY.findall((_LOCALES / f"{language}.ts").read_text()))
    unlabelled = sorted(
        s for s in _allowed_scopes()
        if f"apiTokens.scope.{s}" not in catalogue
    )
    assert unlabelled == [], (
        f"{language}.ts has no apiTokens.scope.* label for {unlabelled}")


def test_descriptor_scopes_are_real_scopes():
    """DESCRIPTOR_SCOPES is a subset by design — a descriptor has no use
    for live.view — but a typo in it would demand a scope no token can
    ever hold, and the entity would simply never be readable."""
    from services.entity_descriptors import DESCRIPTOR_SCOPES

    unknown = sorted(set(DESCRIPTOR_SCOPES) - _allowed_scopes())
    assert unknown == [], (
        f"DESCRIPTOR_SCOPES names {unknown}, which no token can hold")


def test_the_scope_a_read_only_app_entity_needs_is_grantable():
    """The specific thing that was broken, pinned as its own case:
    entity_descriptors gives every non-command app entity
    required_scope='apps.view', so a Home Assistant install reading an
    app's sensors needs it, and needs to be able to tick it."""
    assert "apps.view" in _allowed_scopes()
    assert "apps.view" in _form_scopes()
