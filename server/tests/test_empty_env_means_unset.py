# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An unset variable must not crash the server. It did. (#547)

WHAT ``test_server_env_wiring.py`` CHECKED, AND WHAT IT MISSED.

That test exists because ``docker-compose.yml`` has no ``env_file:`` —
each service enumerates its environment by hand, so a variable added to
the code and to ``.env.example`` stays inert until somebody remembers
the third list. It asserts every ``EVENTS_*`` knob the server reads is
passed through to ``opennvr-core``, and that is true.

It says nothing about the VALUE. Compose writes a passthrough as

    - EVENTS_CAPTION_ENRICHMENT=${EVENTS_CAPTION_ENRICHMENT:-}

and when the variable is absent from ``.env``, compose does not omit
it. It substitutes the empty string and passes the variable anyway. So
"plumbed" and "parseable" turned out to be different properties, and
the test asserted the first while the second was broken for four of the
six knobs it had just finished plumbing — every knob ``.env.example``
does not happen to set.

WHY IT WAS WORSE THAN A BAD DEFAULT.

``core/config.py`` instantiates ``Settings()`` at module scope, so the
``ValidationError`` fires during import, before uvicorn has a logger.
supervisord restarts the backend, it fails the same way, forever;
``opennvr-core`` never reports healthy; every dependent container exits
with ``dependency failed to start``. ``docker logs opennvr_core`` shows
only the restart loop. The actual error is in a file INSIDE the
container. The operator's first experience of the release is a compose
stack that will not come up and a log that does not say why.

THE SHAPE OF THE GUARD.

Deriving the variable list from ``docker-compose.yml`` rather than
listing the six known ones is the whole point: the next passthrough
added to a non-``str`` field is the next occurrence, and it should fail
here on the commit that adds it rather than on somebody's install.
Today that list is 17 and only the six ``EVENTS_*`` map to typed
Settings fields; the other eleven are read elsewhere and tolerate ``''``
by accident, which is exactly the kind of accident that stops holding.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
_CONFIG_SRC = (REPO_ROOT / "server" / "core" / "config.py").read_text(encoding="utf-8")

_SERVICE = "  opennvr-core:"


def _core_block() -> str:
    lines = _COMPOSE.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(_SERVICE))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^  [a-z0-9_-]+:", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def _vars_that_arrive_empty() -> list[str]:
    """Passthroughs compose turns into ``NAME=`` when ``.env`` is silent.

    Deliberately the ``${VAR:-}`` form ONLY. The bare ``${VAR}`` form
    substitutes an empty string just the same, but compose uses it here
    for the things that have no default and must be supplied —
    ``SECRET_KEY``, ``CREDENTIAL_ENCRYPTION_KEY`` — and compose warns
    about those itself. Sweeping them in would assert that a stack with
    no secrets at all should start, which is the opposite of what
    ``validate_strong_secrets`` is for. The two compose spellings turn
    out to mark exactly the two cases the fix separates: optional with a
    default, and required.
    """
    return sorted(set(re.findall(
        r"^\s+- ([A-Z0-9_]+)=\$\{[A-Z0-9_]+:-\}\s*$", _core_block(), re.M)))


def _declared_defaults() -> dict[str, str]:
    """``EVENTS_*`` field name -> the default written in config.py.

    Read from source so that changing a documented default forces this
    test to be updated deliberately, instead of the test following the
    code wherever it goes and asserting nothing.
    """
    return {
        f"EVENTS_{name.upper()}": default.strip()
        for name, default in re.findall(
            r"(?m)^    events_([a-z0-9_]+)\s*:\s*bool\s*=\s*(True|False)\s*$",
            _CONFIG_SRC)
    }


@pytest.fixture
def env(monkeypatch):
    """A hermetic environment: real secrets, no ``.env``, nothing else.

    Generated rather than hard-coded because ``validate_strong_secrets``
    rejects short values and anything matching a placeholder fragment.
    """
    for key in list(os.environ):
        if key.startswith(("EVENTS_", "OPENNVR_", "KAI_C_", "DETECT_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test_empty_env.db")
    for key in ("SECRET_KEY", "MEDIAMTX_SECRET", "INTERNAL_API_KEY"):
        monkeypatch.setenv(key, secrets.token_hex(32))
    monkeypatch.setenv(
        "CREDENTIAL_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(os.urandom(32)).decode())
    return monkeypatch


def _settings():
    """Build Settings the way the app does, minus the ``.env`` file.

    ``_env_file=None`` keeps a developer's own ``.env`` from supplying
    the very values this test needs to be missing — the reason the bug
    reached a release in the first place is that the people who ran it
    all had one.
    """
    from core.config import Settings
    return Settings(_env_file=None)


def test_compose_really_does_pass_empty_strings():
    """Guard the premise.

    Every assertion below is about a situation compose creates. If
    compose ever grows an ``env_file:`` for this service, or stops using
    the ``${VAR:-}`` form, the situation is gone and this test should
    say so loudly rather than keep passing over an empty list.
    """
    empties = _vars_that_arrive_empty()

    assert len(empties) >= 10, (
        f"only {len(empties)} interpolated passthroughs found on "
        f"{_SERVICE.strip()} — either compose changed shape or the "
        f"parser drifted, and the tests below are now vacuous")
    assert "EVENTS_CAPTION_ENRICHMENT" in empties


def test_the_server_starts_with_nothing_set_in_env(env):
    """The bug, reproduced: a stock ``.env`` that sets none of them.

    This is the install described in #547 and it is also the install of
    anyone who copies ``.env.example`` and comments a line out.
    """
    for name in _vars_that_arrive_empty():
        env.setenv(name, "")

    try:
        _settings()
    except ValidationError as exc:
        broken = sorted({e["loc"][0] for e in exc.errors()})
        pytest.fail(
            "Settings() refuses to build when compose passes unset "
            "variables through as empty strings, which is what compose "
            "does on every install whose .env does not set all of them. "
            "This raises at module import, so uvicorn dies before it has "
            "a logger and the container never goes healthy. Offending "
            "settings: " + ", ".join(map(str, broken)))


def test_unset_means_the_default_the_documentation_promises(env):
    """Not crashing is not enough — it has to mean the right thing.

    An empty variable has to land on the value ``.env.example`` and the
    docstrings say you get when you leave it alone. Coercing ``''`` to
    ``False`` would satisfy the test above and silently turn off caption
    and descriptor enrichment for every existing install.
    """
    for name in _vars_that_arrive_empty():
        env.setenv(name, "")
    settings = _settings()

    declared = _declared_defaults()
    assert len(declared) >= 6, "the default-parsing regex has drifted"

    wrong = {
        name: (getattr(settings, name.lower()), default == "True")
        for name, default in declared.items()
        if getattr(settings, name.lower()) is not (default == "True")
    }
    assert not wrong, (
        "an unset variable did not fall back to the documented default: "
        + "; ".join(f"{k} is {got!r}, config.py promises {want!r}"
                    for k, (got, want) in wrong.items()))


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False),
                                          ("1", True), ("0", False)])
def test_a_value_the_operator_actually_set_still_wins(env, raw, expected):
    """The fix must not over-reach.

    "Empty means unset" is only correct if a non-empty value is still
    parsed normally — a validator that dropped too much would leave
    every knob pinned at its default, which is the original unplumbed
    bug wearing a different hat.
    """
    for name in _vars_that_arrive_empty():
        env.setenv(name, "")
    env.setenv("EVENTS_CAPTION_ENRICHMENT", raw)
    env.setenv("EVENTS_EMBED_ENRICHMENT", raw)

    settings = _settings()

    assert settings.events_caption_enrichment is expected
    assert settings.events_embed_enrichment is expected


def test_an_empty_required_secret_still_gets_its_own_error(env):
    """The line between "unset" and "misconfigured".

    ``secret_key`` has no default and an error message written for a
    human — run ``make secrets``. Treating its empty value as "unset"
    too would replace that with pydantic's bare "Field required" and
    make a bad ``.env`` harder to diagnose, not easier. Empty means
    unset only where the field knows how to be unset.
    """
    env.setenv("SECRET_KEY", "")

    with pytest.raises(ValidationError) as caught:
        _settings()

    message = str(caught.value)
    assert "make secrets" in message, (
        "an empty SECRET_KEY no longer produces its own guidance:\n" + message)
