# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 3 fast-track: an app's required adapters ship WITH it.

The bug this guards (observed live 2026-08-29): ``--profile apps up
license-plate-recognition`` succeeded and produced an app that could never
work — nothing ran or registered its fast-plate-ocr dependency, so every
OCR call 404'd at KAI-C. These are deliberately string-level (no yaml
dependency in this suite; same style as test_tier0_metrics' compose guard).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_APPS = (REPO_ROOT / "docker-compose.apps.yml").read_text()


def test_lpr_ships_its_ocr_adapter():
    # Service-level anchor (two-space indent, own line): a depends_on entry
    # or URL mention must NOT satisfy this — the first sabotage run proved
    # a bare substring check passes with the service itself deleted.
    assert "\n  fast-plate-ocr-adapter:\n" in _APPS, (
        "the LPR app's required OCR adapter service is no longer defined — "
        "a fresh install produces an app that can never read a plate")
    assert ("ghcr.io/open-nvr/fast-plate-ocr-adapter:"
            "${ADAPTER_TAG:-latest}") in _APPS, (
        "the OCR adapter must ride the same ADAPTER_TAG pin as every "
        "other adapter (RFC-0002 decision 7: one pinned set per release)")
    assert "OPENNVR_ADAPTER_TOKEN=${INTERNAL_API_KEY}" in _APPS


def test_ocr_adapter_registers_with_kaic_under_the_name_the_platform_calls():
    # The register name becomes KAI-C's route /api/v1/infer/<name>. Since
    # the app's v2 pure-consumer conversion (RFC-0002 Phase 4), the CALLERS
    # are the platform: core's plate enrichment (PLATE_MODEL) and Tier-1
    # dispatch (PLATE_ADAPTER), and the name also keys KAI-C's
    # domain-event normaliser map — a drifted name is a 404 on every OCR
    # call AND a silent end to plate.recognized.v1.
    assert 'name="fast_plate_ocr"' in _APPS
    assert "http://fast-plate-ocr-adapter:9004" in _APPS
    assert "/api/v1/adapters/register" in _APPS
    enrichment = (REPO_ROOT / "server/services/plate_enrichment.py").read_text()
    assert 'PLATE_MODEL = "fast_plate_ocr"' in enrichment, (
        "core's enrichment no longer calls the adapter this overlay "
        "registers — update both sides together")
    dispatch = (REPO_ROOT / "detect-pipeline/detect_pipeline/dispatch.py"
                ).read_text()
    assert 'PLATE_ADAPTER = "fast_plate_ocr"' in dispatch, (
        "Tier-1 dispatch no longer routes to the adapter this overlay "
        "registers — update both sides together")
    normaliser = (REPO_ROOT / "kai-c/kai_c/domain_events.py").read_text()
    assert '"fast_plate_ocr": _normalise_fast_plate_ocr' in normaliser, (
        "KAI-C's normaliser map no longer covers the adapter this overlay "
        "registers — plate.recognized.v1 would stop flowing")


def test_lpr_waits_for_its_dependency():
    lpr = _APPS[_APPS.index("  license-plate-recognition:\n"):]
    dep = lpr[:lpr.index("networks:")]
    assert "fast-plate-ocr-adapter:" in dep, (
        "the app no longer waits for its OCR adapter to be healthy")
    assert "fast-plate-ocr-register:" in dep, (
        "the app no longer waits for registration to have been attempted")


def test_registration_failure_degrades_it_does_not_wedge():
    # The register sidecar must always exit 0: a failed registration is a
    # WARN plus the app's own KAI-C 404 handling, never a compose up that
    # hangs on service_completed_successfully.
    reg = _APPS[_APPS.index("  fast-plate-ocr-register:"):]
    reg = reg[:reg.index("  license-plate-recognition")]
    assert reg.count("exit 0") == 2, (
        "both the success path and the retries-exhausted path must exit 0")
    assert "WARN: could not register" in reg


# ── Issue #371: registrations must survive an opennvr-core restart ──
#
# The one-shot registrar above runs ONCE per `compose up`. KAI-C's
# in-memory registry therefore needs its receipts file on a volume, or
# any restart of opennvr-core silently forgets fast_plate_ocr and every
# plate read 404s with no signal. These pin the compose wiring for the
# persistence added in kai_c/persistence.py.

_BASE = (REPO_ROOT / "docker-compose.yml").read_text()
_ENTRYPOINT = (REPO_ROOT / "docker-entrypoint.sh").read_text()


def test_kai_c_state_dir_is_wired_to_a_volume():
    assert "KAI_C_STATE_DIR=/app/kai-c-state" in _BASE, (
        "opennvr-core no longer tells KAI-C where to persist adapter "
        "registrations — a core restart would silently kill LPR again "
        "(issue #371)")
    assert "- opennvr_kai_c_state:/app/kai-c-state" in _BASE, (
        "the KAI-C state dir is not on a volume — receipts die with the "
        "container, which is the #371 amnesia with extra steps")
    assert "\n  opennvr_kai_c_state:" in _BASE, (
        "the opennvr_kai_c_state volume is mounted but never declared — "
        "compose config would fail")


def test_kai_c_state_dir_is_writable_by_the_service_user():
    # First mount of a named volume is root-owned; KAI-C runs as the
    # opennvr user under supervisord. Without the chown, persistence
    # degrades (with a WARN) back to restart amnesia.
    assert "chown -R opennvr:opennvr /app/kai-c-state" in _ENTRYPOINT
