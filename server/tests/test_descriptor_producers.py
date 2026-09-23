"""Every descriptor kind the platform declares should have something
that writes it.

Three places name descriptor kinds, and all three read as a capability:
``enrichment_plan.TASK_DESCRIPTORS`` says which skill claims which kind,
``journey.ANCHOR_KINDS`` says which kinds are strong enough to be an
identity, and ``journey.KIND_WEIGHT`` prices each one as evidence. None
of them writes a row. A kind can therefore sit in all three, be priced
as the strongest evidence in the system, and never once exist — which is
the same defect that has already been fixed here twice under other
names: an enumerated list that quietly falls behind the thing it lists.

This file is the guard. It does not argue that every declared kind
*must* have a producer — ``face_id`` deliberately does not, and the
reason is worth more than the row would be. It argues that the set of
kinds without one is a decision somebody made on purpose, written down,
and re-approved whenever it changes.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVER))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_producers_test.db")
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


#: Kinds that nothing in this repository writes, and why that is the
#: intended state rather than an oversight. Anything added here is a
#: promise that the absence was chosen; anything removed is a promise
#: that a producer now exists.
KINDS_WITHOUT_A_PRODUCER: dict[str, str] = {
    "face_id": (
        "A name attached to a person is the most sensitive claim in the "
        "set, and CORE deliberately does not produce it: "
        "descriptor_enrichment refuses the kind outright, and "
        "descriptor_store keeps it out of the projected words so it "
        "cannot be reached by free-text search. It is left to an app the "
        "operator installed on purpose, and as of RFC-0003 one exists — "
        "smart-doorbell writes face_id through /internal/app/visits/"
        "claims, roster-scoped, with the binding recorded so a guessed "
        "subject stays distinguishable from a measured one. So this "
        "entry no longer means the kind is never written; it means core "
        "will not be the one writing it, which is the decision worth "
        "keeping. The journey branches that price face_id ARE now "
        "reachable on a deployment running that app."
    ),
}


def _kinds_core_can_write() -> set[str]:
    """The kinds the three in-tree writers can actually emit.

    ``descriptor_store.sync_plate_claim`` writes exactly one kind.
    ``descriptor_enrichment`` writes whatever ``LABEL_KINDS`` maps a
    label to. ``internal_camera_agent`` writes what the agent posts, so
    it constrains nothing and is excluded on purpose — counting it would
    make this test vacuous, since it can write any kind at all.
    """
    from services.descriptor_enrichment import LABEL_KINDS

    kinds = {"plate"}
    for mapped in LABEL_KINDS.values():
        kinds.update(mapped)
    return kinds


def _declared_kinds() -> set[str]:
    from services.enrichment_plan import TASK_DESCRIPTORS
    from services.journey import ANCHOR_KINDS, KIND_WEIGHT

    declared = set(ANCHOR_KINDS) | set(KIND_WEIGHT)
    for spec in TASK_DESCRIPTORS.values():
        declared.update(spec.get("kinds") or ())
    return declared


def test_every_declared_kind_has_a_producer_or_a_written_reason():
    orphans = _declared_kinds() - _kinds_core_can_write()
    assert orphans == set(KINDS_WITHOUT_A_PRODUCER), (
        "These descriptor kinds are declared but nothing writes them: "
        f"{sorted(orphans - set(KINDS_WITHOUT_A_PRODUCER))}. Either add a "
        "producer, or add the kind to KINDS_WITHOUT_A_PRODUCER with the "
        "reason it is meant to stay empty. A kind that is priced as "
        "evidence and never written is a feature that silently never "
        "fires."
    )


def test_the_reasons_are_reasons_and_not_placeholders():
    for kind, why in KINDS_WITHOUT_A_PRODUCER.items():
        assert len(why.split()) >= 20, (
            f"{kind} needs an actual explanation, not a note to self"
        )


@pytest.mark.parametrize("kind", sorted(KINDS_WITHOUT_A_PRODUCER))
def test_a_kind_with_no_producer_is_still_priced_honestly(kind):
    """If we keep pricing it, the branch has to stay correct for the day
    a producer arrives — so it must still be a known weight, not fall
    through to the default."""
    from services.journey import KIND_WEIGHT

    assert kind in KIND_WEIGHT


def test_apps_cannot_publish_a_claim_of_their_own():
    """The face_id reason above rests on this: there is no app-facing
    way to write a descriptor, so 'left to an app' currently means 'left
    undone'. If a descriptor write is ever added to the app platform,
    that reason becomes wrong and the decision needs retaking — this is
    what makes the test fail at that moment rather than years later.
    """
    src = (_SERVER / "routers" / "app_platform.py").read_text()
    routes = [
        line.strip()
        for line in src.splitlines()
        if line.strip().startswith("@router.")
    ]
    offenders = [r for r in routes if "descriptor" in r.lower()]
    assert offenders == [], (
        f"app_platform now exposes {offenders}; revisit the face_id entry "
        "in KINDS_WITHOUT_A_PRODUCER before shipping it"
    )


def test_face_id_is_not_reachable_through_free_text():
    """Restates the store's own rule as an assertion, because the reason
    for the empty producer leans on it: the claim is exact-match only."""
    from services.descriptor_store import _UNPROJECTED_KINDS

    assert "face_id" in _UNPROJECTED_KINDS
