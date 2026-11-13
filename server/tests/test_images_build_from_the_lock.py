# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The image must install what CI tested, or fail saying it cannot.

Both Dockerfiles ran

    uv sync --frozen ... || uv sync ...

and neither copied a ``uv.lock``. So ``--frozen`` could not work — it
exits with "Unable to find lockfile" every time — and the fallback
quietly resolved the newest of everything instead. CI tested one
dependency set and the shipped image ran another, for as long as that
has been true, with nothing reporting the difference.

It has now cost two live failures.

* FastAPI drifted to 0.141.1 against a lock pinning 0.135.3, where
  ``scope["route"].path`` loses the include prefix, and every API token
  got a 403. ``docs/design/home-assistant-integration-implementation-
  plan.md`` records it as a "Trap found live" and works around the
  symptom in ``_route_key``.
* SQLAlchemy 2.1.0 changed the DBAPI a bare ``postgresql://`` resolves
  to, from psycopg2 to psycopg 3, which this project does not install.
  The backend could not import at all, and the smoke test could only
  say "opennvr-core never reached /health within 3 minutes".

A fallback that turns "the lock is stale" — one command to fix — into
"build something else and say nothing" is the defect this repository
keeps finding in other shapes. These assertions are string-level
because a Dockerfile has no other test surface, and they are cheap.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
from pathlib import Path

# ``core.config`` builds a Settings() at import, so the required values
# have to exist BEFORE anything imports it. Whether they already do
# depends on which test module ran first, and a test that passes only
# in a full run is not a test.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_images_lock.db")
for _k in ("SECRET_KEY", "MEDIAMTX_SECRET", "INTERNAL_API_KEY"):
    os.environ.setdefault(_k, secrets.token_hex(32))
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(os.urandom(32)).decode(),
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every Dockerfile that installs Python dependencies with uv, and the
#: manifest directories it installs from.
_IMAGES: dict[str, tuple[str, ...]] = {
    "Dockerfile": ("server", "kai-c"),
    "kai-c/Dockerfile": ("kai-c",),
}


def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_the_files_are_where_this_test_thinks():
    """Guard the guard: a moved Dockerfile must fail loudly here rather
    than leave every assertion below inspecting nothing."""
    for rel in _IMAGES:
        assert (REPO_ROOT / rel).is_file(), f"{rel} is gone or moved"
        assert "uv sync" in _text(rel), f"{rel} no longer installs with uv"


def test_no_image_falls_back_to_an_unlocked_resolve():
    """The rule.

    ``--frozen || <unfrozen>`` means the build NEVER fails for a bad
    lock, and never uses it either.
    """
    # One shape, because one shape is what a fallback looks like: a
    # `uv sync` line carrying `||`. Line continuations mean the command
    # can span lines, so the whole RUN block is joined first.
    offenders = []
    for rel in _IMAGES:
        joined = _text(rel).replace("\\\n", " ")
        for line in joined.splitlines():
            # Comments describe the fallback that was removed; they are
            # not the fallback. Without this the note explaining the fix
            # fails the test the fix added, which is a special kind of
            # silly and cost a run to notice.
            if line.lstrip().startswith("#"):
                continue
            if "uv sync" in line and "||" in line:
                offenders.append(rel)
                break

    assert not offenders, (
        "these images fall back to an unlocked resolve when --frozen "
        "fails, so they install whatever is newest rather than what CI "
        "tested: " + ", ".join(offenders) + ". Drop the fallback — a "
        "stale lock is one `uv lock` away and the build should say so.")


def test_every_image_copies_the_lock_it_claims_to_use():
    """``--frozen`` without a lockfile is not strict, it is broken.

    uv exits "Unable to find lockfile" — which, with a fallback, looked
    exactly like success.
    """
    missing: list[str] = []
    for rel, manifests in _IMAGES.items():
        body = _text(rel)
        if "--frozen" not in body:
            continue
        for manifest in manifests:
            # Either "COPY server/pyproject.toml server/uv.lock ..." or a
            # bare "COPY pyproject.toml uv.lock ." inside that directory.
            copied = re.search(rf"COPY[^\n]*{re.escape(manifest)}/uv\.lock", body) \
                or (rel.startswith(manifest + "/") and re.search(r"COPY[^\n]*\buv\.lock", body))
            if not copied:
                missing.append(f"{rel} -> {manifest}/uv.lock")

    assert not missing, (
        "syncs --frozen but never copies the lockfile, so the flag can "
        "only ever fail: " + ", ".join(missing))


def test_the_locks_that_are_promised_exist():
    for manifests in _IMAGES.values():
        for manifest in manifests:
            lock = REPO_ROOT / manifest / "uv.lock"
            assert lock.is_file(), (
                f"{manifest}/uv.lock is missing, so --frozen cannot "
                f"succeed in any image that installs from it")


def test_the_postgres_driver_is_named_rather_than_defaulted():
    """The second line of defence, and why it exists.

    SQLAlchemy 2.1.0 moved the default DBAPI for a bare
    ``postgresql://`` from psycopg2 to psycopg 3. The lock is what keeps
    the VERSION steady; this keeps the DRIVER steady even if a bump gets
    through, because "which library talks to the database" should not be
    a thing a patch release decides.
    """
    from sqlalchemy.engine.url import make_url

    from core.config import Settings

    def url(value: str) -> str:
        return Settings(
            _env_file=None,
            database_url=value,
            secret_key="s" * 40,
            mediamtx_secret="m" * 40,
            internal_api_key="i" * 40,
            credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
        ).database_url

    for given in ("postgresql://u:p@db:5432/d", "postgres://u:p@db/d"):
        got = url(given)
        assert got.startswith("postgresql+psycopg2://"), f"{given} -> {got}"
        assert make_url(got).get_dialect().__module__.endswith("psycopg2")

    # An explicit driver is somebody choosing on purpose. Left alone.
    assert url("postgresql+psycopg://u:p@db/d").startswith("postgresql+psycopg://")
    assert url("sqlite:///./x.db") == "sqlite:///./x.db"
