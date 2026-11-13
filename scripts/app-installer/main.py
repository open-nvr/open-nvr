# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Entrypoint for the OpenNVR app installer — poll + reconcile loop.

This is the single privileged, NON-network-facing component. It:

  1. reads the desired-state table (``app_install_intents``) via the
     SQLAlchemy store, and
  2. reconciles each pending intent by shelling out to ``docker
     compose`` through the real ``docker_runner``.

Both the store (DB) and the runner (Docker) are injected into the pure
``reconcile_once`` core, which is what the unit tests exercise with
fakes — this module is the thin production wiring only.

Config (env):

  DATABASE_URL          — required; points at the app_install_intents DB
                          (ideally a least-privilege SELECT/UPDATE role).
  APPS_INDEX_PATH       — the installer's baked copy of the curated
                          index (default /app/apps_index.yml). FAIL-
                          CLOSED: the installer refuses to start without
                          it — intents are only selectors into this
                          index, never trusted for image/digest.
  INSTALLER_POLL_SECONDS — poll interval, default 10.
  INSTALLER_COMPOSE_FILES — comma-separated compose files, default
                          "docker-compose.yml,docker-compose.apps.yml".
  INSTALLER_PROFILE     — compose profile, default "apps".
  INSTALLER_SIGNATURES  — "require" (default: a pinned image must carry a
                          valid Sigstore signature from its expected
                          signer, checked with cosign before compose
                          runs) or "off" (air-gapped / lab). signing.py.

Failure backoff: an intent that keeps failing (bad image, unpullable
digest, poison row) is retried with exponential backoff (10s doubling
to a 15-minute cap) instead of every poll tick, so a permanently-broken
intent can't churn CPU/network/logs forever. A success clears the
backoff; a new request (server resets status to pending) is picked up
on the next allowed retry.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from reconciler import (
    DEFAULT_COMPOSE_FILES,
    DEFAULT_PROFILE,
    docker_runner,
    load_curated_index,
    reconcile_once,
)
from signing import (
    DEFAULT_MODE as DEFAULT_SIGNING_MODE,
    SIGNING_MODES,
    SigningPolicy,
    cosign_available,
    cosign_verifier,
)
from store import SqlIntentStore

# Exponential failure backoff: base 10s, doubling per consecutive
# failure, capped at 15 minutes.
BACKOFF_BASE_SECONDS = 10.0
BACKOFF_CAP_SECONDS = 900.0

logging.basicConfig(
    level=os.environ.get("INSTALLER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("opennvr.app-installer")


def _compose_files() -> tuple[str, ...]:
    raw = os.environ.get("INSTALLER_COMPOSE_FILES")
    if not raw:
        return DEFAULT_COMPOSE_FILES
    return tuple(f.strip() for f in raw.split(",") if f.strip())


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2

    poll_seconds = float(os.environ.get("INSTALLER_POLL_SECONDS", "10"))
    compose_files = _compose_files()
    profile = os.environ.get("INSTALLER_PROFILE", DEFAULT_PROFILE)

    # Fail-closed trust anchor: no curated index, no installer. The DB
    # rows are selectors only; everything that actually deploys comes
    # from this baked file (see reconciler.load_curated_index).
    index_path = os.environ.get("APPS_INDEX_PATH", "/app/apps_index.yml")
    try:
        index = load_curated_index(index_path)
    except Exception:
        logger.exception(
            "curated index %s missing or unparseable — refusing to start "
            "(the installer never acts without its trust anchor)",
            index_path,
        )
        return 2
    logger.info(
        "curated index loaded: %d installable app(s): %s",
        len(index), sorted(index),
    )

    signing_mode = os.environ.get("INSTALLER_SIGNATURES", DEFAULT_SIGNING_MODE).strip().lower()
    if signing_mode not in SIGNING_MODES:
        logger.error("INSTALLER_SIGNATURES must be one of %s, got %r", SIGNING_MODES, signing_mode)
        return 2
    if signing_mode == "require" and not cosign_available():
        logger.error(
            "INSTALLER_SIGNATURES=require but 'cosign' is not on PATH — refusing "
            "to start (rebuild the installer image, or set INSTALLER_SIGNATURES=off "
            "for an air-gapped deployment that accepts unsigned images)"
        )
        return 2
    signing = SigningPolicy(
        mode=signing_mode,
        verify=cosign_verifier(docker_runner) if signing_mode == "require" else None,
    )
    if signing_mode == "off":
        logger.warning("IMAGE SIGNATURES NOT CHECKED (INSTALLER_SIGNATURES=off): pinned "
                       "images deploy without proof of who built them")

    store = SqlIntentStore(database_url)
    logger.info(
        "app-installer starting: poll=%ss compose_files=%s profile=%s signatures=%s",
        poll_seconds,
        list(compose_files),
        profile,
        signing_mode,
    )

    # Per-id failure backoff state: consecutive failures + next allowed
    # retry time. In-memory on purpose — a restart just retries once.
    failures: dict[str, int] = {}
    next_try: dict[str, float] = {}

    while True:
        try:
            now = time.monotonic()
            skip_ids = frozenset(
                app_id for app_id, t in next_try.items() if now < t
            )
            outcomes = reconcile_once(
                store,
                docker_runner,
                index=index,
                skip_ids=skip_ids,
                compose_files=compose_files,
                profile=profile,
                signing=signing,
            )
            for app_id, status_, message in outcomes:
                logger.info(
                    "reconciled %s -> %s (%s)", app_id, status_, message
                )
                if status_ == "failed":
                    failures[app_id] = failures.get(app_id, 0) + 1
                    delay = min(
                        BACKOFF_CAP_SECONDS,
                        BACKOFF_BASE_SECONDS * (2 ** (failures[app_id] - 1)),
                    )
                    next_try[app_id] = time.monotonic() + delay
                    logger.warning(
                        "%s failed %d time(s); next retry in %.0fs",
                        app_id, failures[app_id], delay,
                    )
                else:
                    failures.pop(app_id, None)
                    next_try.pop(app_id, None)
        except Exception:  # keep the loop alive; log and retry next tick
            logger.exception("reconcile sweep failed; retrying next poll")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
