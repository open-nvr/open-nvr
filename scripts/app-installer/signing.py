# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Image signatures — what the digest pin does not prove.

A digest pin says "these exact bytes". It does not say *who built
them*: a digest in the index could be anything a PR author pushed to
their own registry. Signing closes that gap. ``publish-app-images.yml``
signs every catalog image it pushes with **Sigstore keyless signing**
(cosign + the workflow's GitHub OIDC identity — no long-lived key to
leak or rotate), and this module makes the installer refuse a pinned
image whose signature does not verify against the expected identity
before ``docker compose up`` ever sees it.

The expected identity for an ``ghcr.io/open-nvr/…`` image is the
org's own CI: a workflow in a repository under ``github.com/open-nvr``
running on ``main`` or a ``v*`` tag, issued by GitHub's token service.
An index entry may name a different signer explicitly
(``signing: {identity: <regexp>, issuer: <url>}``) — that is how a
catalog app built by another org's CI is trusted, and it is reviewed
with the entry.

Modes (``INSTALLER_SIGNATURES``):

* ``require`` (default) — a pinned image must verify; otherwise the
  intent fails with a message the operator can read in the catalog.
  Unpinned (dev-only) installs are not verified: there is no digest to
  bind a signature to, and they already log the loud UNPINNED warning.
* ``off`` — no verification. For an air-gapped deployment that cannot
  reach the Sigstore transparency log, or a lab. Logged at start-up so
  nobody forgets.

Verification is ``cosign verify`` run through the same injected
``runner`` the compose calls use, so unit tests never need cosign.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("opennvr.app-installer")

SIGNING_MODES = ("require", "off")
DEFAULT_MODE = "require"

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
#: Images the org's CI builds: any workflow in any open-nvr repository,
#: running on main or a release tag. Anchored on both ends.
ORG_IDENTITY_RE = (
    r"^https://github\.com/open-nvr/[A-Za-z0-9_.-]+/\.github/workflows/"
    r"[A-Za-z0-9_.-]+\.ya?ml@refs/(heads/main|tags/v[0-9][A-Za-z0-9_.-]*)$"
)
ORG_IMAGE_RE = re.compile(r"^ghcr\.io/open-nvr/[a-z0-9][a-z0-9._-]*(?::[^@/]+)?(?:@sha256:[0-9a-f]{64})?$")

COSIGN_BIN = "cosign"


@dataclass(frozen=True)
class Signer:
    """Who must have signed an image: a certificate-identity regexp and
    the OIDC issuer that vouched for it."""

    identity: str
    issuer: str = GITHUB_OIDC_ISSUER


def expected_signer(image: str, declared: Signer | None = None) -> Signer | None:
    """The signer a pinned image must verify against: the entry's own
    declaration when it has one, the org's CI for ``ghcr.io/open-nvr``
    images, and ``None`` (no known signer → refused in ``require``
    mode) for anything else."""
    if declared is not None:
        return declared
    if ORG_IMAGE_RE.match(image.split("@", 1)[0]):
        return Signer(identity=ORG_IDENTITY_RE)
    return None


def parse_signer(raw: object) -> Signer | None:
    """``signing: {identity, issuer}`` from an index entry, validated.
    Malformed → ``None`` with an error log (never a half-trusted signer)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.error("curated index: 'signing' must be a mapping, got %r", type(raw).__name__)
        return None
    identity = raw.get("identity")
    issuer = raw.get("issuer", GITHUB_OIDC_ISSUER)
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 500:
        logger.error("curated index: 'signing.identity' must be a non-empty regexp")
        return None
    try:
        re.compile(identity)
    except re.error as exc:
        logger.error("curated index: 'signing.identity' is not a valid regexp: %s", exc)
        return None
    if not isinstance(issuer, str) or not issuer.startswith("https://"):
        logger.error("curated index: 'signing.issuer' must be an https URL")
        return None
    return Signer(identity=identity.strip(), issuer=issuer.strip())


def verify_argv(ref: str, signer: Signer) -> list[str]:
    """``cosign verify`` for one digest-pinned ref. Output is JSON on
    stdout on success; non-zero exit with the reason on stderr otherwise."""
    return [
        COSIGN_BIN, "verify",
        "--certificate-oidc-issuer", signer.issuer,
        "--certificate-identity-regexp", signer.identity,
        ref,
    ]


#: ``(ref, signer) -> (ok, message)``
Verifier = Callable[[str, Signer], tuple[bool, str]]


def cosign_verifier(runner) -> Verifier:
    """A verifier that shells ``cosign verify`` out through ``runner``
    (the reconciler's injected command seam)."""
    def verify(ref: str, signer: Signer) -> tuple[bool, str]:
        result = runner(verify_argv(ref, signer), None)
        if result.returncode == 0:
            return True, "signature verified"
        detail = (result.stderr or result.stdout or f"cosign exited {result.returncode}").strip()
        return False, detail.splitlines()[-1] if detail else "cosign failed"
    return verify


@dataclass(frozen=True)
class SigningPolicy:
    mode: str = DEFAULT_MODE
    verify: Verifier | None = None

    def __post_init__(self):
        if self.mode not in SIGNING_MODES:
            raise ValueError(f"INSTALLER_SIGNATURES must be one of {SIGNING_MODES}, got {self.mode!r}")
        if self.mode == "require" and self.verify is None:
            raise ValueError("signing mode 'require' needs a verifier")

    def check(self, app_id: str, pinned_ref: str, declared: Signer | None) -> tuple[bool, str]:
        """Whether a pinned image may deploy. Never raises."""
        if self.mode == "off":
            return True, "signature check off (INSTALLER_SIGNATURES=off)"
        signer = expected_signer(pinned_ref, declared)
        if signer is None:
            return False, (
                f"refused: {pinned_ref} has no known signer — it is not an "
                "ghcr.io/open-nvr image and its index entry declares no "
                "'signing' identity (set INSTALLER_SIGNATURES=off to install unsigned images)"
            )
        try:
            ok, message = self.verify(pinned_ref, signer)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 — a crashing verifier is a refusal
            ok, message = False, f"verifier error: {exc.__class__.__name__}: {exc}"
        if ok:
            logger.info("app %r: %s (identity %s, issuer %s)", app_id, message, signer.identity, signer.issuer)
            return True, message
        return False, (
            f"refused: signature of {pinned_ref} did not verify against "
            f"{signer.identity} (issuer {signer.issuer}): {message}"
        )


def cosign_available() -> bool:
    return shutil.which(COSIGN_BIN) is not None
