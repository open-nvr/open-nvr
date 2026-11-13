# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Withdrawn releases (updater/revocations.py, UPD-402).

A withdrawn release is never offered, so never installed by itself either;
a site running one is told; the list is only believed with a trusted
signature, and a list that fails its check stops the update; no list
means nothing is withdrawn.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_updater_check import NEW, OLD_C, release, run_check, site
from updater import check, manifest, revocations, source


def listed(*versions: str, reason: str = "breaks recordings on ARM") -> bytes:
    return json.dumps({"schema": 1, "revoked": [{"version": v, "reason": reason} for v in versions]}).encode()


@pytest.fixture
def key():
    k = Ed25519PrivateKey.generate()
    return k, {"rel": k.public_key()}


def test_a_signed_list_is_read(key):
    k, trusted = key
    raw = listed("0.1.9", "v0.2.0")
    assert revocations.verify(raw, manifest.sign(raw, k, "rel"), trusted) == {
        "0.1.9": "breaks recordings on ARM", "0.2.0": "breaks recordings on ARM"}


def test_a_changed_or_foreign_list_is_refused(key):
    k, trusted = key
    raw = listed("0.1.9")
    with pytest.raises(manifest.ManifestError, match="revocation list signature does not match"):
        revocations.verify(listed("0.1.8"), manifest.sign(raw, k, "rel"), trusted)
    with pytest.raises(manifest.ManifestError, match="does not trust"):
        revocations.verify(raw, manifest.sign(raw, Ed25519PrivateKey.generate(), "other"), trusted)


@pytest.mark.parametrize("raw", [b"not json", b'{"schema": 2, "revoked": []}', b'{"schema": 1, "revoked": [{"v": 1}]}',
                                 b'{"schema": 1, "revoked": [{"version": "latest"}]}'])
def test_malformed_lists_are_refused(raw):
    with pytest.raises(manifest.ManifestError):
        revocations.parse(raw)


def test_no_list_in_a_bundle_means_none_withdrawn(tmp_path):
    assert revocations.from_dir(tmp_path) == {}
    (tmp_path / revocations.NAME).write_bytes(listed("0.1.9"))
    with pytest.raises(manifest.ManifestError, match="without its signature"):
        revocations.from_dir(tmp_path)


def test_a_withdrawn_release_is_not_offered():
    r = check.decide(site(revoked={"0.1.9": "breaks recordings on ARM"}), NEW)
    assert isinstance(r, check.Refusal) and r.code == check.REFUSED
    assert "withdrawn" in r.reason and "breaks recordings on ARM" in r.reason


def test_a_site_on_a_withdrawn_release_is_told():
    plan = check.decide(site(revoked={"0.1.8": "loses alerts"}), NEW)
    assert isinstance(plan, check.Plan) and any("withdrawn" in w and "loses alerts" in w for w in plan.warnings)
    r = check.decide(site(installed_version="0.1.9", revoked={"0.1.9": "loses alerts"}), NEW)
    assert r.code == check.UP_TO_DATE and "withdrawn" in r.reason


def test_cli_refuses_a_withdrawn_release_found_online(tmp_path, monkeypatch, capsys):
    k = Ed25519PrivateKey.generate()
    keys = tmp_path / "trusted.json"
    keys.write_text(json.dumps({"keys": [manifest.public_key_entry(k, "t")]}))
    monkeypatch.setenv(manifest.EXTRA_KEYS_ENV, str(keys))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def signed(doc):
        raw = json.dumps(doc).encode()
        return raw, manifest.sign(raw, k, "t")
    old, new = signed(release("0.1.8", OLD_C, "aaa")), signed(release("0.1.9", "2" * 40, "bbb"))
    code, work = run_check(tmp_path, monkeypatch, {None: new, "0.1.8": old, "revoked": {"0.1.9": "bad"}})
    assert code == check.REFUSED and "withdrawn" in capsys.readouterr().out
    assert not (work / "plan.env").exists()


def test_cli_reads_the_list_a_bundle_carries_and_stops_on_a_bad_one(tmp_path, monkeypatch, capsys):
    k = Ed25519PrivateKey.generate()
    keys = tmp_path / "trusted.json"
    keys.write_text(json.dumps({"keys": [manifest.public_key_entry(k, "t")]}))
    monkeypatch.setenv(manifest.EXTRA_KEYS_ENV, str(keys))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def signed(doc):
        raw = json.dumps(doc).encode()
        return raw, manifest.sign(raw, k, "t")
    bundle = tmp_path / "bundle"
    source.save(bundle, *signed(release("0.1.9", "2" * 40, "bbb")))
    source.save(bundle / "history" / "v0.1.8", *signed(release("0.1.8", OLD_C, "aaa")))
    raw = listed("0.1.9")
    (bundle / revocations.NAME).write_bytes(raw)
    (bundle / revocations.SIGNATURE_NAME).write_bytes(manifest.sign(raw, k, "t"))
    code, _ = run_check(tmp_path, monkeypatch, {}, "--bundle", str(bundle))
    assert code == check.REFUSED and "withdrawn" in capsys.readouterr().out
    (bundle / revocations.NAME).write_bytes(listed("0.1.7"))  # changed after signing
    code, _ = run_check(tmp_path, monkeypatch, {}, "--bundle", str(bundle))
    assert code == 1 and "does not match" in capsys.readouterr().err
