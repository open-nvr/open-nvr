# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Signing the withdrawn-release list (scripts/release/sign_revocations.py)."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater import manifest, revocations

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("sign_revocations", REPO / "scripts/release/sign_revocations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_shipped_list_is_readable():
    assert revocations.parse((REPO / "release" / "revocations.json").read_bytes()) == {}


def test_signed_list_verifies_and_a_bad_one_never_ships(tool, tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [manifest.public_key_entry(key, "k")]}))
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    monkeypatch.setenv(manifest.EXTRA_KEYS_ENV, str(keys))
    monkeypatch.setenv("RELEASE_SIGNING_KEY", base64.b64encode(seed).decode())
    monkeypatch.setenv("RELEASE_SIGNING_KEY_ID", "k")
    src = tmp_path / "revocations.json"
    src.write_text(json.dumps({"schema": 1, "revoked": [{"version": "0.2.1", "reason": "bad"}]}))
    assert tool.main(["--source", str(src), "--out", str(tmp_path / "dist")]) == 0
    assert revocations.from_dir(tmp_path / "dist") == {"0.2.1": "bad"}
    src.write_text(json.dumps({"schema": 1, "revoked": [{"version": "soon"}]}))
    with pytest.raises(manifest.ManifestError):
        tool.main(["--source", str(src), "--out", str(tmp_path / "dist2")])
