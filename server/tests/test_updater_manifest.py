# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The signed release manifest (updater/manifest.py).

* only bytes signed by a trusted key are accepted, and only exactly those
  bytes: one changed character, another key, or no trusted key at all is
  refused with a message an operator can act on;
* the schema is checked before anything uses it;
* versions order like releases do (pre-releases before their release).
"""

from __future__ import annotations

import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater import UPDATER_PROTOCOL, manifest as m

D = "sha256:" + "a" * 64
I_AMD = "sha256:" + "b" * 64
I_ARM = "sha256:" + "c" * 64


def doc(**over) -> dict:
    d = {
        "schema": 1, "product": "opennvr", "version": "0.1.9", "channel": "stable",
        "git_commit": "0123456789abcdef0123456789abcdef01234567", "git_tag": "v0.1.9",
        "alembic_head": "ff88aa99bb00", "min_upgrade_from": "0.1.8", "min_updater": 1,
        "pins": {"CORE_TAG": "0.1.9", "ADAPTER_TAG": "0.1.9"},
        "images": [{"ref": "ghcr.io/open-nvr/core:0.1.9", "digest": D,
                    "ids": {"linux/amd64": I_AMD, "linux/arm64": I_ARM}}],
        "manual_steps": [], "urgent": False, "state_migrations": False,
    }
    d.update(over)
    return d


@pytest.fixture
def key():
    k = Ed25519PrivateKey.generate()
    return k, {"rel-1": k.public_key()}


def signed(d: dict, k, key_id="rel-1"):
    raw = json.dumps(d, indent=2).encode()
    return raw, m.sign(raw, k, key_id)


def test_a_signed_manifest_is_accepted(key):
    k, trusted = key
    raw, sig = signed(doc(), k)
    man = m.verify(raw, sig, trusted)
    assert man.version == "0.1.9" and man.pins["CORE_TAG"] == "0.1.9"
    assert man.image_for("ghcr.io/open-nvr/core").ids["linux/arm64"] == I_ARM


def test_one_changed_byte_is_refused(key):
    k, trusted = key
    raw, sig = signed(doc(), k)
    with pytest.raises(m.ManifestError, match="changed or corrupted"):
        m.verify(raw.replace(b"0.1.9", b"0.1.7", 1), sig, trusted)


def test_another_key_is_refused(key):
    _, trusted = key
    raw, sig = signed(doc(), Ed25519PrivateKey.generate())
    with pytest.raises(m.ManifestError, match="changed or corrupted"):
        m.verify(raw, sig, trusted)
    raw, sig = signed(doc(), Ed25519PrivateKey.generate(), key_id="stranger")
    with pytest.raises(m.ManifestError, match="does not trust"):
        m.verify(raw, sig, trusted)


def test_no_trusted_key_refuses_everything(key):
    k, _ = key
    raw, sig = signed(doc(), k)
    with pytest.raises(m.ManifestError, match="trusts no release key"):
        m.verify(raw, sig, {})


def test_a_garbled_signature_file_is_refused(key):
    k, trusted = key
    raw, _ = signed(doc(), k)
    for bad in (b"", b"{}", b'{"key_id": "rel-1", "signature": "not base64!"}'):
        with pytest.raises(m.ManifestError, match="unreadable"):
            m.verify(raw, bad, trusted)


def test_key_rotation_trusts_both_keys(key):
    k, trusted = key
    new = Ed25519PrivateKey.generate()
    trusted = {**trusted, "rel-2": new.public_key()}
    for priv, kid in ((k, "rel-1"), (new, "rel-2")):
        raw, sig = signed(doc(), priv, kid)
        assert m.verify(raw, sig, trusted).version == "0.1.9"


def test_extra_keys_file_is_trusted_for_testing(tmp_path, monkeypatch, key):
    k, _ = key
    f = tmp_path / "keys.json"
    f.write_text(json.dumps({"keys": [m.public_key_entry(k, "mine")]}))
    monkeypatch.setenv(m.EXTRA_KEYS_ENV, str(f))
    raw, sig = signed(doc(), k, "mine")
    assert m.verify(raw, sig).version == "0.1.9"


@pytest.mark.parametrize("change, message", [
    ({"schema": 2}, "schema"),
    ({"product": "other"}, "not for OpenNVR"),
    ({"min_updater": UPDATER_PROTOCOL + 1}, "newer updater"),
    ({"version": "latest"}, "not a release version"),
    ({"channel": "nightly"}, "unknown channel"),
    ({"git_commit": "abc123"}, "full commit hash"),
    ({"pins": {"core_tag": "1"}}, "plain KEY=value"),
    ({"pins": {"CORE_TAG": "1; rm -rf /"}}, "plain KEY=value"),
    ({"pins": {"CORE_TAG": "$(reboot)"}}, "plain KEY=value"),
    ({"images": []}, "no images"),
    ({"images": [{"ref": "x:1", "digest": "sha256:short", "ids": {"linux/amd64": I_AMD}}]}, "digest"),
    ({"images": [{"ref": "x:1", "digest": D, "ids": {}}]}, "image IDs"),
    ({"manual_steps": [{"id": 1}]}, "manual step"),
])
def test_bad_manifests_are_refused(change, message):
    with pytest.raises(m.ManifestError, match=message):
        m.parse(doc(**change))


def test_missing_and_mistyped_fields_are_refused():
    for field in ("version", "git_commit", "pins", "images", "alembic_head"):
        d = doc()
        del d[field]
        with pytest.raises(m.ManifestError, match=field):
            m.parse(d)
    with pytest.raises(m.ManifestError, match="wrong type"):
        m.parse(doc(min_updater=True))


def test_optional_fields_default_safely():
    d = doc()
    for k in ("manual_steps", "urgent", "state_migrations", "min_upgrade_from"):
        d.pop(k)
    man = m.parse(d)
    assert (man.manual_steps, man.urgent, man.state_migrations, man.min_upgrade_from) == ((), False, False, None)
    assert man.raw == d and m.parse(copy.deepcopy(d)) == man


def test_versions_order_like_releases():
    order = ["0.1.8", "0.1.9-rc.1", "0.1.9-rc.2", "0.1.9-rc.10", "0.1.9", "0.1.10", "0.2.0", "1.0.0"]
    assert sorted(order, key=m.parse_version) == order
    assert m.is_newer("0.1.10", "0.1.9") and not m.is_newer("0.1.9", "0.1.9")
    assert m.parse_version("v0.1.9") == m.parse_version("0.1.9")
    assert [m.upgrade_kind("0.1.8", v) for v in ("0.1.9", "0.2.0", "1.0.0")] == ["patch", "minor", "major"]


def test_the_shipped_keys_file_is_readable():
    assert isinstance(m.trusted_keys(), dict)
