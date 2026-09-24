# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every enrichment knob the server reads must reach the container.

THE SAME DEFECT, IN THE OTHER HALF OF THE SYSTEM.

``test_detect_pipeline_wiring.py`` exists because ``docker-compose.yml``
has no ``env_file:`` — each service enumerates its environment by hand,
so a variable added to the code and to ``.env.example`` stays inert
until somebody remembers the third list. It caught
``DETECT_MOTION_MAX_FORCED_EXITS``.

Nobody wrote the equivalent for the server, and the same thing had
happened there, to every single one of them. None of

    EVENTS_PLATE_ENRICHMENT      (default true)
    EVENTS_CAPTION_ENRICHMENT    (default true)
    EVENTS_DESCRIPTOR_ENRICHMENT (default true)
    EVENTS_DESCRIPTOR_PEOPLE     (default false)
    EVENTS_ENRICHMENT_BACKFILL   (default false)

reached ``opennvr-core``. So on a compose install — which is every
install — the three defaulting true could not be turned OFF by an
operator trying to cut an inference bill, and the two defaulting false
could not be turned ON at all. ``EVENTS_DESCRIPTOR_PEOPLE`` shipped as
a documented opt-in that nobody was able to opt into.

It would have happened a sixth time immediately:
``EVENTS_EMBED_ENRICHMENT`` is the switch for semantic search, it
defaults off, and without plumbing it an operator could set it in
``.env``, restart, and get no embeddings — with nothing in any log
saying why. A feature reachable only by editing compose by hand is not
shipped.

The failure mode is what makes it worth a test rather than a review
habit: the code falls back to its own default, the service starts
clean, and nothing anywhere reports that the setting was ignored.

Deliberately string-level, matching the sibling test's style — no yaml
dependency in this suite.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = REPO_ROOT / "server" / "core" / "config.py"
_COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
_ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

#: The service that runs the server.
_SERVICE = "  opennvr-core:"

#: Settings whose env name starts with this are operator-facing
#: enrichment switches — the family that was entirely unplumbed.
#:
#: Scoped to a PREFIX rather than "every setting on the class" on
#: purpose. Most of the server's settings are wired by compose under a
#: different name (DATABASE_URL is composed from POSTGRES_*), derived,
#: or genuinely internal, and a test that demanded all of them would be
#: a wall of exceptions nobody maintains — which is how a guard stops
#: being read. This family is small, operator-facing, documented, and
#: was broken; widen it when another family earns the same treatment.
_PREFIX = "EVENTS_"

#: Read by the code but intentionally NOT passed to the container.
#: Every entry needs a reason — "we forgot" is the bug this test exists
#: to catch, so an unexplained addition here defeats the guard.
DELIBERATELY_UNPLUMBED: dict[str, str] = {}


def _settings_fields() -> set[str]:
    """``EVENTS_*`` env names the Settings class defines.

    pydantic-settings maps ``events_embed_enrichment`` to
    ``EVENTS_EMBED_ENRICHMENT``, so the field names ARE the env names
    upper-cased. Read from source rather than by importing, so this
    needs no app configuration to run.
    """
    text = _CONFIG.read_text(encoding="utf-8")
    fields = re.findall(r"(?m)^    (events_[a-z0-9_]+)\s*:", text)
    return {f.upper() for f in fields}


def _passed_to_container() -> set[str]:
    lines = _COMPOSE.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(_SERVICE))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^  [a-z0-9_-]+:", lines[i])), len(lines))
    block = "\n".join(lines[start:end])
    return set(re.findall(r"^\s+- ([A-Z0-9_]+)=", block, re.M))


def test_the_service_block_is_actually_found():
    """Guard the guard. A compose refactor that renames the service must
    fail loudly here rather than silently reducing every assertion below
    to a comparison of two empty sets."""
    passed = _passed_to_container()

    assert len(passed) > 20, (
        f"found only {len(passed)} env vars on {_SERVICE.strip()} — the "
        f"block parser no longer matches docker-compose.yml, so the "
        f"assertions below are vacuous")
    assert "DATABASE_URL" in passed


def test_the_settings_were_actually_parsed():
    """The other vacuous-pass guard: if the field regex stopped
    matching, "every knob is plumbed" would hold over an empty set."""
    fields = _settings_fields()

    assert len(fields) >= 5, (
        f"parsed only {len(fields)} EVENTS_* settings out of config.py — "
        f"the field regex has probably drifted")
    assert "EVENTS_EMBED_ENRICHMENT" in fields


def test_every_enrichment_knob_the_code_reads_reaches_the_container():
    """The rule, and the thing that was broken for all of them."""
    missing = sorted(_settings_fields() - _passed_to_container()
                     - set(DELIBERATELY_UNPLUMBED))

    assert not missing, (
        "read by the server but never passed to opennvr-core — setting "
        "these in .env does nothing and the code silently uses its own "
        "default: " + ", ".join(missing) + ". Add them to the "
        "opennvr-core service's environment: block in "
        "docker-compose.yml, or to DELIBERATELY_UNPLUMBED with a reason.")


def test_every_documented_enrichment_knob_reaches_the_container():
    """Documenting a knob in .env.example promises an operator it works.

    Plumbed-but-undocumented is harmless; documented-but-unplumbed is a
    lie told in writing.
    """
    documented = set(re.findall(r"(?m)^(EVENTS_[A-Z0-9_]+)=", _ENV_EXAMPLE))
    broken = sorted(documented - _passed_to_container()
                    - set(DELIBERATELY_UNPLUMBED))

    assert not broken, (
        ".env.example documents these but docker-compose.yml never "
        "passes them to opennvr-core, so following the documentation "
        "has no effect: " + ", ".join(broken))


def test_semantic_search_can_be_switched_on_by_an_operator():
    """The specific regression, pinned by name.

    EVENTS_EMBED_ENRICHMENT defaults OFF, which makes an unplumbed
    variable indistinguishable from a working one that is simply off —
    the operator sets it, restarts, sees no embeddings, and has nothing
    to go on. A default-off feature needs its switch plumbed MORE than a
    default-on one, not less.
    """
    assert "EVENTS_EMBED_ENRICHMENT" in _passed_to_container(), (
        "semantic search cannot be turned on from .env on a compose "
        "install — the feature ships unreachable")


def test_the_people_opt_in_can_actually_be_opted_into():
    """The one that was already shipped broken: a documented opt-in with
    no way to opt in."""
    assert "EVENTS_DESCRIPTOR_PEOPLE" in _passed_to_container()
