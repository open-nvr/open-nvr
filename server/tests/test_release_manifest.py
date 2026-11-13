# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The release manifest generator (scripts/release/make_manifest.py).

* the Alembic head it finds is the one Alembic itself finds;
* every pulled image of the release is in the manifest, resolved to a
  digest and image IDs; images compose builds are not; only *_TAG pins the
  compose files use are recorded, never a choice like CAPTION_ADAPTER;
* the result passes the updater's own checks, a signed one verifies with
  the key that signed it, and an unknown --extra field is refused.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater import manifest as man

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mk():
    spec = importlib.util.spec_from_file_location(
        "make_manifest", REPO / "scripts/release/make_manifest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRegistry:
    def __init__(self):
        self.asked = []

    def resolve(self, ref):
        self.asked.append(ref)
        h = format(abs(hash(ref)) % 16**60, "060x")
        return (
            "sha256:d" + h + "abc",
            {
                "linux/amd64": "sha256:a" + h + "abc",
                "linux/arm64": "sha256:b" + h + "abc",
            },
            {
                "linux/amd64": "sha256:e" + h + "abc",
                "linux/arm64": "sha256:f" + h + "abc",
            },
        )


def test_the_alembic_head_is_alembics(mk):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO / "server" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "server" / "migrations"))
    assert (
        mk.alembic_head(REPO / "server" / "migrations" / "versions")
        == ScriptDirectory.from_config(cfg).get_current_head()
    )


def test_two_heads_stop_the_release(mk, tmp_path):
    for rev in ("aaa", "bbb"):
        (tmp_path / f"{rev}.py").write_text(
            f'revision = "{rev}"\ndown_revision = "root"\n'
        )
    (tmp_path / "root.py").write_text('revision = "root"\ndown_revision = None\n')
    with pytest.raises(SystemExit, match="one Alembic head"):
        mk.alembic_head(tmp_path)


def test_the_manifest_covers_every_pulled_image(mk):
    env = mk.pins_lib.load_env_pins(mk.pins_lib.ENV_EXAMPLE)
    reg = FakeRegistry()
    doc = mk.build(env["CORE_TAG"], "0" * 40, {}, reg)
    refs = {i["ref"] for i in doc["images"]}
    assert f"ghcr.io/open-nvr/core:{env['CORE_TAG']}" in refs
    assert "postgres:15-alpine" in refs  # third-party images are pinned too
    assert not any(r.startswith("opennvr/") for r in refs)  # built locally, not pulled
    assert set(doc["pins"]) >= {"CORE_TAG", "ADAPTER_TAG"}
    assert all(k.endswith("_TAG") for k in doc["pins"])
    assert (
        doc["git_tag"] == f"v{env['CORE_TAG']}"
        and man.parse(doc).version == env["CORE_TAG"]
    )


def test_a_release_whose_pin_is_another_version_is_stopped(mk):
    with pytest.raises(SystemExit, match="pins CORE_TAG"):
        mk.build("99.0.0", "0" * 40, {}, FakeRegistry())


def test_extra_fields_are_merged_and_unknown_ones_refused(mk):
    env = mk.pins_lib.load_env_pins(mk.pins_lib.ENV_EXAMPLE)
    extra = {
        "min_upgrade_from": "0.1.0",
        "urgent": True,
        "manual_steps": [{"id": "new-secret", "text": "Run generate-secrets"}],
    }
    doc = mk.build(env["CORE_TAG"], "0" * 40, extra, FakeRegistry())
    m = man.parse(doc)
    assert (
        m.urgent
        and m.min_upgrade_from == "0.1.0"
        and m.manual_steps[0].id == "new-secret"
    )
    with pytest.raises(SystemExit, match="may only set"):
        mk.build(env["CORE_TAG"], "0" * 40, {"pins": {}}, FakeRegistry())


def test_a_signed_release_verifies_with_its_key(mk, tmp_path, monkeypatch):
    import base64

    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [man.public_key_entry(key, "test-key")]}))
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv(man.EXTRA_KEYS_ENV, str(keys))
    monkeypatch.setenv("RELEASE_SIGNING_KEY", base64.b64encode(seed).decode())
    monkeypatch.setenv("RELEASE_SIGNING_KEY_ID", "test-key")
    monkeypatch.setattr(mk, "Registry", FakeRegistry)
    env = mk.pins_lib.load_env_pins(mk.pins_lib.ENV_EXAMPLE)
    assert (
        mk.main(
            ["--version", env["CORE_TAG"], "--commit", "1" * 40, "--out", str(tmp_path)]
        )
        == 0
    )
    raw = (tmp_path / man.MANIFEST_NAME).read_bytes()
    assert (
        man.verify(raw, (tmp_path / man.SIGNATURE_NAME).read_bytes()).git_commit
        == "1" * 40
    )


def test_registry_refs_split_like_docker(mk):
    s = mk.Registry.split
    assert s("ghcr.io/open-nvr/core:0.1.9") == ("ghcr.io", "open-nvr/core", "0.1.9")
    assert s("postgres:15-alpine") == (
        "registry-1.docker.io",
        "library/postgres",
        "15-alpine",
    )
    assert s("amir20/dozzle:v8.9.1") == (
        "registry-1.docker.io",
        "amir20/dozzle",
        "v8.9.1",
    )
    assert s("nginx") == ("registry-1.docker.io", "library/nginx", "latest")


class FlavourlessRegistry(FakeRegistry):
    """ghcr.io without the hardware flavours: 404 on every ``-intel`` /
    ``-qualcomm`` tag, like adapter release 0.1.8, which predates them."""

    def resolve(self, ref):
        import urllib.error

        tag = ref.rsplit(":", 1)[-1]
        if any(tag.endswith(s) for s in ("-intel", "-qualcomm", "-nvidia", "-rocm")):
            self.asked.append(ref)
            raise urllib.error.HTTPError(ref, 404, "Not Found", {}, None)
        return super().resolve(ref)


def test_a_missing_hardware_flavour_is_left_out_not_fatal(mk):
    """The edge manifest for every main push failed with a 404 once the
    flavour overlays pinned adapter images at ``0.1.8-intel`` that were
    never built. A flavour the registry lacks is skipped (start.sh falls
    back to cpu without it); the cpu image stays required."""
    env = mk.pins_lib.load_env_pins(mk.pins_lib.ENV_EXAMPLE)
    reg = FlavourlessRegistry()
    doc = mk.build(env["CORE_TAG"], "0" * 40, {}, reg)
    refs = [i["ref"] for i in doc["images"]]
    assert refs, "no images resolved at all"
    assert not any(mk.is_flavour_ref(r) for r in refs)
    assert any(mk.is_flavour_ref(r) for r in reg.asked), (
        "the flavour refs were never even asked for"
    )
    assert any(not mk.is_flavour_ref(r) for r in reg.asked)


def test_a_missing_cpu_image_is_still_fatal(mk):
    import urllib.error

    class Broken(FakeRegistry):
        def resolve(self, ref):
            raise urllib.error.HTTPError(ref, 404, "Not Found", {}, None)

    env = mk.pins_lib.load_env_pins(mk.pins_lib.ENV_EXAMPLE)
    with pytest.raises(urllib.error.HTTPError):
        mk.build(env["CORE_TAG"], "0" * 40, {}, Broken())


def test_flavour_refs_are_recognised_by_their_tag(mk):
    assert mk.is_flavour_ref("ghcr.io/open-nvr/detect-pipeline:0.1.9-qualcomm")
    assert mk.is_flavour_ref("ghcr.io/open-nvr/yolov8-adapter:latest-intel")
    assert not mk.is_flavour_ref("ghcr.io/open-nvr/yolov8-adapter:0.1.9")
    assert not mk.is_flavour_ref("bluenviron/mediamtx:1.15.4-ffmpeg")
