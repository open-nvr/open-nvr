# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Image-signature policy (scripts/app-installer/signing.py) and its
seat in the reconciler: a pinned image must verify against its expected
signer before compose runs; unsigned or mis-signed → a failed intent
that never touches Docker.

Run with:
    python -m pytest scripts/app-installer/tests -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import signing  # noqa: E402
from reconciler import CuratedApp, Intent, RunResult, load_curated_index, reconcile_intent  # noqa: E402
from signing import Signer, SigningPolicy, cosign_verifier, expected_signer, verify_argv  # noqa: E402

PIN = "ghcr.io/open-nvr/loitering-detection@sha256:" + "a" * 64


class _Runner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, argv, env=None):
        self.calls.append((argv, env))
        return self.result


# ── who must have signed ────────────────────────────────────────────


def test_org_images_expect_the_orgs_ci():
    s = expected_signer(PIN)
    assert s is not None and s.issuer == signing.GITHUB_OIDC_ISSUER
    rx = re.compile(s.identity)
    ok = [
        "https://github.com/open-nvr/open-nvr/.github/workflows/publish-app-images.yml@refs/heads/main",
        "https://github.com/open-nvr/open-nvr/.github/workflows/publish-app-images.yml@refs/tags/v0.2.0",
        "https://github.com/open-nvr/app-plate-vip/.github/workflows/build.yml@refs/tags/v1.0.0-rc1",
    ]
    bad = [
        "https://github.com/evil-org/open-nvr/.github/workflows/publish-app-images.yml@refs/heads/main",
        "https://github.com/open-nvr/open-nvr/.github/workflows/publish-app-images.yml@refs/heads/feature",
        "https://github.com/open-nvr/open-nvr/.github/workflows/publish-app-images.yml@refs/pull/12/merge",
        "https://github.com/open-nvr-fake/x/.github/workflows/a.yml@refs/heads/main",
        "https://github.com/open-nvr/open-nvr/.github/workflows/publish-app-images.yml@refs/heads/main/extra",
    ]
    assert all(rx.match(i) for i in ok)
    assert not any(rx.match(i) for i in bad)


def test_other_registries_need_a_declared_signer():
    assert expected_signer("docker.io/vendor/app@sha256:" + "b" * 64) is None
    assert expected_signer("ghcr.io/open-nvr-fake/app@sha256:" + "b" * 64) is None
    declared = Signer(identity="^https://github.com/vendor/app/.*$", issuer="https://token.actions.githubusercontent.com")
    assert expected_signer("docker.io/vendor/app@sha256:" + "b" * 64, declared) is declared
    assert expected_signer(PIN, declared) is declared           # an entry's declaration wins


def test_parse_signer_rejects_malformed_declarations(caplog):
    assert signing.parse_signer(None) is None
    good = signing.parse_signer({"identity": "^https://github.com/vendor/.*$"})
    assert good == Signer(identity="^https://github.com/vendor/.*$", issuer=signing.GITHUB_OIDC_ISSUER)
    assert signing.parse_signer({"identity": "^x$", "issuer": "https://issuer.example"}).issuer == "https://issuer.example"
    for raw in ["str", {"identity": ""}, {"identity": "("}, {"identity": "^x$", "issuer": "http://plain"}, {}]:
        assert signing.parse_signer(raw) is None, raw
    assert "signing" in caplog.text


def test_curated_index_carries_the_declared_signer(tmp_path):
    p = tmp_path / "apps_index.yml"
    p.write_text(
        "- id: vendor-app\n  image: docker.io/vendor/app:1\n  image_digest: sha256:" + "c" * 64 + "\n"
        "  signing:\n    identity: '^https://github.com/vendor/app/.*$'\n"
        "- id: plain-app\n  image: ghcr.io/open-nvr/plain-app:latest\n"
    )
    idx = load_curated_index(p)
    assert idx["vendor-app"].signer == Signer(identity="^https://github.com/vendor/app/.*$")
    assert idx["plain-app"].signer is None


# ── the verifier + policy ───────────────────────────────────────────


def test_cosign_verifier_argv_and_outcomes():
    signer = Signer(identity="^id$", issuer="https://iss")
    assert verify_argv(PIN, signer) == [
        "cosign", "verify", "--certificate-oidc-issuer", "https://iss",
        "--certificate-identity-regexp", "^id$", PIN,
    ]
    ok_runner = _Runner(RunResult(0, "[{...}]", "Verification for ... : OK"))
    assert cosign_verifier(ok_runner)(PIN, signer) == (True, "signature verified")
    assert ok_runner.calls[0][1] is None                          # no image-override env
    bad = _Runner(RunResult(1, "", "Error: no matching signatures\nmain.go: error"))
    assert cosign_verifier(bad)(PIN, signer) == (False, "main.go: error")


def test_policy_modes():
    with pytest.raises(ValueError):
        SigningPolicy(mode="maybe")
    with pytest.raises(ValueError):
        SigningPolicy(mode="require")                             # needs a verifier
    off = SigningPolicy(mode="off")
    assert off.check("x", "docker.io/unknown/app@sha256:" + "0" * 64, None)[0] is True

    seen = []

    def verify(ref, signer):
        seen.append((ref, signer.identity))
        return ref == PIN, "sig"

    req = SigningPolicy(mode="require", verify=verify)
    ok, msg = req.check("loitering-detection", PIN, None)
    assert ok and seen == [(PIN, signing.ORG_IDENTITY_RE)]
    ok, msg = req.check("x", "ghcr.io/open-nvr/x@sha256:" + "1" * 64, None)
    assert not ok and "did not verify" in msg
    ok, msg = req.check("x", "docker.io/vendor/x@sha256:" + "1" * 64, None)
    assert not ok and "no known signer" in msg and len(seen) == 2        # never asked

    def boom(ref, signer):
        raise RuntimeError("cosign missing")
    ok, msg = SigningPolicy(mode="require", verify=boom).check("x", PIN, None)
    assert not ok and "verifier error" in msg


# ── in the reconciler ───────────────────────────────────────────────

INDEX = {
    "loitering-detection": CuratedApp(
        id="loitering-detection", image="ghcr.io/open-nvr/loitering-detection:latest",
        image_digest="sha256:" + "a" * 64),
    "occupancy-counting": CuratedApp(
        id="occupancy-counting", image="ghcr.io/open-nvr/occupancy-counting:latest",
        image_digest=None),
}


def _intent(app_id="loitering-detection", digest="sha256:" + "a" * 64):
    return Intent(id=app_id, image=INDEX[app_id].image, image_digest=digest,
                  desired="installed", status="pending")


def test_pinned_install_verifies_before_compose_and_refuses_on_failure():
    calls = []
    policy = SigningPolicy(mode="require", verify=lambda ref, s: (calls.append(ref) or False, "no matching signatures"))
    runner = _Runner(RunResult(0, "up", ""))
    status, message = reconcile_intent(_intent(), runner, index=INDEX, signing=policy)
    assert status == "failed" and "did not verify" in message and "no matching signatures" in message
    assert calls == [PIN]
    assert runner.calls == []                                     # compose never ran


def test_pinned_install_proceeds_when_the_signature_verifies():
    policy = SigningPolicy(mode="require", verify=lambda ref, s: (True, "signature verified"))
    runner = _Runner(RunResult(0, "up", ""))
    status, _ = reconcile_intent(_intent(), runner, index=INDEX, signing=policy)
    assert status == "applied"
    argv, env = runner.calls[0]
    assert argv[-2:] == ["egress-proxy", "loitering-detection"]
    assert env == {"LOITERING_DETECTION_IMAGE": PIN}


def test_unpinned_install_is_not_verified_and_stays_dev_only(caplog):
    policy = SigningPolicy(mode="require", verify=lambda ref, s: (False, "should not be asked"))
    runner = _Runner(RunResult(0, "up", ""))
    status, _ = reconcile_intent(_intent("occupancy-counting", None), runner, index=INDEX, signing=policy)
    assert status == "applied" and "UNPINNED" in caplog.text


def test_signing_off_skips_the_check():
    runner = _Runner(RunResult(0, "up", ""))
    status, _ = reconcile_intent(_intent(), runner, index=INDEX, signing=SigningPolicy(mode="off"))
    assert status == "applied" and len(runner.calls) == 1
