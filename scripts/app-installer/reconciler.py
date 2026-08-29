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

"""OpenNVR App Installer — the single privileged reconciler.

This is the ONLY OpenNVR component that holds the Docker socket. The web
app never runs Docker; it only writes desired-state rows into the
``app_install_intents`` table (see server/routers/apps.py). This
reconciler polls that table and drives the actual ``docker compose``
up/down for each intent, then writes back the reconcile ``status``.

Design (desired-state + reconciler):

* Read every intent row (id, image, image_digest, desired, status).
* **Re-validate every install intent against the installer's OWN baked
  copy of the curated index** (``apps_index.yml``, shipped in this
  image). The DB row only selects *which curated app*; the image and
  digest that actually deploy come from the index, never from the row.
  An id that isn't kebab-case or isn't in the index is marked
  ``failed`` without touching Docker. This is the trust boundary the
  socket-split exists for: a compromised web app can write any row it
  likes, but it cannot make this component run an uncurated image.
* For a ``desired="installed"`` row that isn't already applied → run
  ``docker compose ... up -d <id>`` and, when the INDEX carries a
  digest, pin the image to ``image@sha256:...``; on success mark
  ``status="applied"``, on non-zero exit mark ``status="failed"`` with
  the stderr in ``message``.
* For a ``desired="absent"`` row → run ``docker compose ... down`` /
  ``stop+rm`` for that service and mark ``status="applied"`` (removed).
  Teardown only needs the kebab-case id check (an app de-listed from
  the index must still be uninstallable).
* Status write-back is compare-and-swapped on the ``desired`` value the
  sweep acted on, so an operator flipping install→uninstall mid-run is
  never clobbered into a silently-lost request.
* An index entry without a digest means UNPINNED — a loud warning is
  logged and the run is documented as dev-only (do not run unpinned in
  production).

Testability: the docker/subprocess call is injected as a ``runner``
callable so unit tests pass a fake runner and never touch real Docker.
The DB access is likewise injected as a ``store`` so tests use an
in-memory fake. Nothing here is network-facing.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger("opennvr.app-installer")

# App ids are kebab-case by construction (the index validator enforces it
# at submission time); the reconciler re-checks because intent rows can be
# written by a compromised web app, bypassing every server-side gate. A
# non-kebab id is refused before it ever reaches a docker argv (defense
# against leading-dash flag injection like ``--remove-orphans``).
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# The compose files the reconciler drives, in overlay order. The apps
# overlay carries the per-app service blocks; the base file carries the
# core services they depend on / share a network with.
DEFAULT_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.apps.yml")
# Compose profile the app service blocks live under (see
# docker-compose.apps.yml — every app service is ``profiles: [apps]``).
DEFAULT_PROFILE = "apps"


# ── Injected seams ─────────────────────────────────────────────────────


@dataclass
class RunResult:
    """The outcome of one injected command run."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


# A ``runner`` takes the argv list plus an optional env override and
# returns a RunResult. The real runner shells out to ``docker`` with the
# override merged over ``os.environ``; tests pass a fake that records
# argv + env and returns a canned RunResult — no Docker involved. The
# env carries the per-service digest-pin (``<ID>_IMAGE=image@sha256:…``)
# so pinning actually reaches ``docker compose``.
Runner = Callable[[list[str], "dict[str, str] | None"], RunResult]


@dataclass
class Intent:
    """One desired-state row, decoupled from SQLAlchemy so the reconciler
    core is trivially unit-testable."""

    id: str
    image: str
    image_digest: str | None
    desired: str  # "installed" | "absent"
    status: str  # "pending" | "applied" | "failed"


class IntentStore(Protocol):
    """The persistence seam. The production impl is a thin SQLAlchemy
    reader/writer over ``app_install_intents``; tests use a fake dict."""

    def list_intents(self) -> list[Intent]:
        ...

    def set_status(
        self, intent_id: str, status: str, message: str, *, desired: str
    ) -> None:
        """Write status/message for one intent, guarded on ``desired``:
        the UPDATE must only apply if the row's desired value still equals
        the one this sweep acted on. If the operator flipped the desired
        state mid-reconcile, the guarded write matches nothing and the row
        stays ``pending`` for the next sweep — the new request is never
        clobbered by a stale completion."""
        ...


# ── Curated index (the installer's own trust anchor) ───────────────────


@dataclass(frozen=True)
class CuratedApp:
    """The only fields of an index entry the installer acts on."""

    id: str
    image: str
    image_digest: str | None
    # RFC-0002 Phase 3 (decision 7): KAI-C adapters provisioned WITH the
    # app. Upped alongside it; refcounted across installed apps on
    # uninstall (released only when no other installed app requires it).
    requires_adapters: tuple[str, ...] = ()


def load_curated_index(path: str | Path) -> dict[str, CuratedApp]:
    """Load the installer's baked copy of ``apps_index.yml``.

    This is the reconciler's trust anchor: intents from the DB are only
    ever *selectors into this dict* — the image/digest that deploy come
    from here, never from the row. The file is baked into the installer
    image at build time (same release, same bytes as the server's copy),
    so a compromised web app — which by design can write arbitrary intent
    rows — still cannot choose what actually runs.

    Fail-closed: a missing or unparseable index raises, and ``main()``
    refuses to start; entries without an ``id``/``image`` are skipped
    with a logged error rather than half-trusted.
    """
    import yaml  # local import: the pure reconcile core stays stdlib-only

    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError(f"curated index {path}: expected a top-level list")
    index: dict[str, CuratedApp] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            logger.error("curated index: skipping non-mapping entry %r", entry)
            continue
        app_id, image = entry.get("id"), entry.get("image")
        if not isinstance(app_id, str) or not isinstance(image, str):
            logger.error("curated index: skipping entry without id/image: %r",
                         entry.get("id"))
            continue
        digest = entry.get("image_digest")
        raw_adapters = entry.get("requires_adapters") or []
        adapters = tuple(
            a for a in raw_adapters
            if isinstance(a, str) and _ADAPTER_RE.fullmatch(a)
        ) if isinstance(raw_adapters, list) else ()
        index[app_id] = CuratedApp(
            id=app_id,
            image=image,
            image_digest=digest if isinstance(digest, str) else None,
            requires_adapters=adapters,
        )
    return index


# ── The real docker runner (production only; never imported by tests) ──


# Adapter names are snake_case KAI-C registry names; their overlay
# service is <name-with-dashes>-adapter (the fast-plate-ocr-adapter
# convention). Same trust rule as app ids: only names from the baked
# curated index ever reach an argv, and only in this validated shape.
_ADAPTER_RE = re.compile(r"[a-z0-9]+(_[a-z0-9]+)*")


def adapter_service_name(adapter: str) -> str:
    """KAI-C adapter name -> its compose service (decision 7 convention)."""
    return adapter.replace("_", "-") + "-adapter"


def docker_runner(
    argv: list[str], env: dict[str, str] | None = None
) -> RunResult:
    """Shell out to ``docker`` (or any argv). Production runner only.

    Kept dead simple and side-effect-only so the interesting logic stays
    in the pure reconcile functions below, which take an injected runner.

    ``env`` (when given) is the per-service image-override the installer
    sets for digest pinning (``<ID>_IMAGE=image@sha256:…``). It is MERGED
    OVER ``os.environ`` — not a replacement — so ``docker compose`` keeps
    the operator's PATH / DOCKER_HOST / .env context and only the pin is
    added. ``argv`` stays a plain list (no shell), so nothing here is
    injectable regardless of the env values.
    """
    run_env: dict[str, str] | None = None
    if env:
        run_env = {**os.environ, **env}
    # noqa: S603 — argv is a fixed compose template plus an app id that
    # reconcile_intent has already checked against the kebab-case grammar
    # AND the installer's baked curated index; env values never enter argv.
    proc = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
    )
    return RunResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


# ── Compose argv builders ──────────────────────────────────────────────


def _compose_base(compose_files: tuple[str, ...], profile: str) -> list[str]:
    argv = ["docker", "compose"]
    for f in compose_files:
        argv += ["-f", f]
    argv += ["--profile", profile]
    return argv


def build_up_argv(
    intent: Intent,
    *,
    adapters: tuple[str, ...] = (),
    compose_files: tuple[str, ...] = DEFAULT_COMPOSE_FILES,
    profile: str = DEFAULT_PROFILE,
) -> list[str]:
    """``docker compose ... up -d <id>`` for one app service.

    Digest pinning: when the intent carries an ``image_digest``, the
    pinned ref ``image@sha256:...`` is passed to compose via the
    per-service image override env the app compose blocks read
    (``<ID>_IMAGE``), so the reconciler deploys the exact bytes the
    curated index vouched for. When absent, the service's own ``image:``
    default is used (unpinned — dev only).

    RFC-0002 Phase 3 (decision 7): the app's required adapters (from the
    curated index) are upped in the same command, ahead of the app — an
    already-running adapter is a no-op (reuse), a missing service block
    fails the whole install LOUDLY (the "cannot be provisioned" refusal).
    """
    services = [
        adapter_service_name(a) for a in (adapters or ())
    ] + [intent.id]
    return _compose_base(compose_files, profile) + ["up", "-d", *services]


def build_down_argv(
    intent: Intent,
    *,
    release_adapters: tuple[str, ...] = (),
    compose_files: tuple[str, ...] = DEFAULT_COMPOSE_FILES,
    profile: str = DEFAULT_PROFILE,
) -> list[str]:
    """``docker compose ... rm -s -f <id> [adapters]`` — stop and remove
    the app service plus any of its adapters whose refcount dropped to
    zero (decision 7: uninstall RELEASES, and the last release removes;
    an adapter another installed app still requires is never listed)."""
    services = [intent.id] + [
        adapter_service_name(a) for a in (release_adapters or ())
    ]
    return _compose_base(compose_files, profile) + ["rm", "-s", "-f", *services]


def image_env_key(app_id: str) -> str:
    """The compose image-override env var name for one app id.

    The SINGLE source of truth for the ``app-id → ENV`` transform: the id
    is upper-snake-cased and suffixed ``_IMAGE`` (e.g.
    ``license-plate-recognition`` → ``LICENSE_PLATE_RECOGNITION_IMAGE``).
    docker-compose.apps.yml reads exactly this var
    (``image: ${<KEY>:-opennvr/<id>:local-build}``), so this transform
    and the compose file MUST stay in lock-step — it is unit-tested."""
    return app_id.upper().replace("-", "_") + "_IMAGE"


def pinned_image_ref(intent: Intent) -> str | None:
    """The digest-pinned image ref (``image@sha256:...``) or None when
    unpinned. Exposed so the runner/env wiring and tests agree on the
    exact pin string."""
    if not intent.image_digest:
        return None
    digest = intent.image_digest
    # Accept both "sha256:abc..." and a bare "abc..." digest.
    if not digest.startswith("sha256:"):
        digest = f"sha256:{digest}"
    # Strip any existing tag before appending the digest.
    base = intent.image.split("@", 1)[0]
    base = base.rsplit(":", 1)[0] if ":" in base.split("/")[-1] else base
    return f"{base}@{digest}"


# ── Reconcile core (pure logic + injected runner/store) ────────────────


def _run_env(intent: Intent) -> dict[str, str]:
    """The per-service image-override env the runner should apply. Only
    set when the intent is pinned; the key mirrors the app compose
    block's ``${<ID>_IMAGE}`` override slot."""
    pin = pinned_image_ref(intent)
    if pin is None:
        return {}
    return {image_env_key(intent.id): pin}


def reconcile_intent(
    intent: Intent,
    runner: Runner,
    *,
    index: dict[str, CuratedApp],
    held_adapters: frozenset[str] = frozenset(),
    compose_files: tuple[str, ...] = DEFAULT_COMPOSE_FILES,
    profile: str = DEFAULT_PROFILE,
) -> tuple[str, str]:
    """Reconcile ONE intent. Returns ``(status, message)``.

    Pure except for the injected ``runner`` — no Docker, no DB. This is
    the function the unit tests drive with a fake runner.

    Trust boundary: the intent row only *selects* a curated app. Before
    anything touches Docker the id must be kebab-case (no argv flag
    injection) and, for installs, present in the installer's own baked
    ``index`` — and the image/digest that deploy are taken from that
    index entry, with the row's copies ignored (logged when divergent).
    A compromised web app can therefore write any row it likes and still
    only ever start/stop apps the curated index vouches for.
    """
    if not _KEBAB_RE.fullmatch(intent.id or ""):
        return "failed", (
            f"refused: app id {intent.id!r} is not kebab-case — not a "
            "curated app id (see server/config/apps_index.yml)"
        )

    if intent.desired == "installed":
        curated = index.get(intent.id)
        if curated is None:
            return "failed", (
                f"refused: app id {intent.id!r} is not in the installer's "
                "curated index — the DB row cannot select an uncurated app"
            )
        if intent.image != curated.image or (
            intent.image_digest or None
        ) != (curated.image_digest or None):
            logger.warning(
                "intent %r carries image %r@%r but the curated index says "
                "%r@%r — IGNORING the row's copy (only the index is "
                "trusted). A divergent row usually means a stale intent "
                "after an index update, or a tampered DB.",
                intent.id, intent.image, intent.image_digest,
                curated.image, curated.image_digest,
            )
        # Deploy WHAT THE INDEX SAYS, never what the row says.
        intent = Intent(
            id=intent.id,
            image=curated.image,
            image_digest=curated.image_digest,
            desired=intent.desired,
            status=intent.status,
        )
        pin = pinned_image_ref(intent)
        # The env override is what actually pins the deploy: when a digest
        # is present ``_run_env`` yields ``{<ID>_IMAGE: image@sha256:…}``,
        # which the app's compose ``image:`` line reads; when absent it is
        # empty (``{}``) so compose falls back to the local-build default
        # AND we log the loud UNPINNED warning below.
        override = _run_env(intent)
        if pin is None:
            logger.warning(
                "UNPINNED — dev only: app %r has no image_digest; deploying "
                "%r without supply-chain pinning. Do NOT run unpinned images "
                "in production — add an image_digest to the curated index.",
                intent.id,
                intent.image,
            )
        else:
            logger.info("Pinning app %r to %s", intent.id, pin)
        argv = build_up_argv(
            intent, adapters=curated.requires_adapters,
            compose_files=compose_files, profile=profile,
        )
        result = runner(argv, override or None)
        if result.returncode == 0:
            return "applied", (result.stdout or "compose up succeeded").strip()
        return "failed", (
            result.stderr or f"compose up exited {result.returncode}"
        ).strip()

    if intent.desired == "absent":
        # Decision 7 refcount: release this app's adapters, but only the
        # ones NO other installed app requires (``held_adapters``, computed
        # by the sweep from the full intent list). A delisted app releases
        # nothing (its requirements are unknown) — never guess a teardown.
        curated = index.get(intent.id)
        release = tuple(
            a for a in (curated.requires_adapters if curated else ())
            if a not in held_adapters
        )
        argv = build_down_argv(
            intent, release_adapters=release,
            compose_files=compose_files, profile=profile,
        )
        # No image override on teardown — ``rm`` doesn't resolve the image.
        result = runner(argv, None)
        if result.returncode == 0:
            return "applied", (result.stdout or "compose down succeeded").strip()
        return "failed", (
            result.stderr or f"compose down exited {result.returncode}"
        ).strip()

    return "failed", f"unknown desired state {intent.desired!r}"


def reconcile_once(
    store: IntentStore,
    runner: Runner,
    *,
    index: dict[str, CuratedApp],
    skip_ids: frozenset[str] = frozenset(),
    compose_files: tuple[str, ...] = DEFAULT_COMPOSE_FILES,
    profile: str = DEFAULT_PROFILE,
) -> list[tuple[str, str, str]]:
    """One reconcile sweep over every pending intent.

    Skips rows already in their terminal ``applied`` state for the
    current desired value (the server resets ``status`` to ``pending`` on
    every new request, so a re-request is always re-applied), plus any id
    in ``skip_ids`` — the caller's failure-backoff set, so a poison
    intent that always fails doesn't get retried every poll tick.

    The status write-back is guarded on the ``desired`` value this sweep
    acted on (see ``IntentStore.set_status``): if the operator flipped
    install→uninstall while compose was running, the guarded UPDATE
    matches nothing, the row stays ``pending``, and the NEW desired state
    is reconciled next sweep instead of being silently lost.

    Returns a list of ``(id, status, message)`` for observability.
    """
    outcomes: list[tuple[str, str, str]] = []
    intents = store.list_intents()
    for intent in intents:
        if intent.status == "applied":
            continue  # nothing to do until the server flips it to pending
        if intent.id in skip_ids:
            continue  # failure backoff — the caller will retry later
        # Decision 7 refcount: adapters still required by ANY other app
        # whose desired state is installed. Desired state (not live
        # containers) is the source of truth the reconciler converges on.
        held = frozenset(
            a
            for other in intents
            if other.id != intent.id and other.desired == "installed"
            for a in (
                index[other.id].requires_adapters
                if other.id in index else ()
            )
        )
        status_, message = reconcile_intent(
            intent, runner,
            index=index, held_adapters=held,
            compose_files=compose_files, profile=profile,
        )
        store.set_status(intent.id, status_, message, desired=intent.desired)
        outcomes.append((intent.id, status_, message))
    return outcomes
