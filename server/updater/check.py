# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Preflight: may this site move to this release, and what exactly changes.

``decide`` is pure: every fact about the site comes in through ``Site``
(gathered by the host scripts and the CLI), so each rule is tested on its
own. Its answer is a ``Plan`` the host carries out step by step, or a
``Refusal`` that says what to do instead. Nothing has changed on the site
when either is returned.
"""

from __future__ import annotations

from dataclasses import dataclass

from updater.manifest import Image, Manifest, parse_version, upgrade_kind

# Exit codes the host scripts act on.
READY = 0
UP_TO_DATE = 10
REFUSED = 20
NEEDS_OPERATOR = 21


@dataclass(frozen=True)
class Site:
    """What the host and the installed release know about this site."""

    #: The version the running core reports (its own main.py).
    installed_version: str
    #: The installed release's verified manifest, or None when there is no
    #: record of one (first use with no manifest for it, or never managed).
    installed: Manifest | None
    #: ``.env`` as it is now.
    env: dict[str, str]
    #: ``git rev-parse HEAD`` of the checkout.
    head: str
    #: Tracked files the operator changed (``git status --porcelain -uno``).
    changed_files: tuple[str, ...]
    #: ``docker version`` server platform, e.g. ``linux/amd64``.
    platform: str
    #: Every image ref this site's compose files and overlays use now.
    site_images: tuple[str, ...]
    #: Image ID of the running core container, if it runs.
    core_image_id: str | None
    #: ``alembic_version`` in the database, if it could be read.
    db_revision: str | None
    #: Operator's answers: ran the manual steps; treat this checkout as
    #: managed even though it does not match a release; accept edge builds.
    accept_manual: bool = False
    adopt: bool = False
    allow_edge: bool = False
    #: Releases the project withdrew, {version: reason} (updater/revocations.py).
    revoked: dict[str, str] | None = None


@dataclass(frozen=True)
class PlannedImage:
    #: The ref compose will use after the update.
    ref: str
    digest: str
    #: Image IDs that prove the right image is on the host: the registry
    #: digest (a containerd image store reports that as the ID) and the
    #: config digest for this platform (the classic store reports that).
    accept: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    from_version: str
    to_version: str
    kind: str
    git_commit: str
    git_tag: str
    #: Where to put the checkout back on a rollback.
    from_commit: str
    pins: dict[str, str]
    images: tuple[PlannedImage, ...]
    #: True when the update moves the Alembic head: a rollback must put the
    #: database dump back, not just the old images.
    db_restore_on_rollback: bool
    #: "auto", or "manual" when the release changes data outside the
    #: database and putting the old database back would not undo it.
    rollback: str
    warnings: tuple[str, ...] = ()
    manual_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refusal:
    code: int
    reason: str
    #: What the operator can do about it, one line each.
    advice: tuple[str, ...] = ()


def repository(ref: str) -> str:
    """``ghcr.io/open-nvr/core`` for ``ghcr.io/open-nvr/core:0.1.9`` (or ``@sha256:…``)."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return ref.rsplit(":", 1)[0] if ":" in last else ref


def accepted_ids(image: Image, platform: str) -> tuple[str, ...]:
    """The digests that may be the local image ID of *image* on *platform*:
    the registry index digest (containerd store, pulled), the config digest
    (classic store), and the platform's own manifest digest (containerd
    store, loaded from a bundle). ``linux/arm64`` matches ``linux/arm64/v8``."""
    def ours(p: str) -> bool:
        return p == platform or p.startswith(platform + "/")
    ids = [i for p, i in image.ids.items() if ours(p)]
    if not ids:
        return ()
    return (image.digest, *ids, *(d for p, d in image.platform_manifests.items() if ours(p)))


def decide(site: Site, candidate: Manifest) -> Plan | Refusal:
    installed = site.installed

    revoked = site.revoked or {}
    installed_withdrawn = revoked.get(site.installed_version)

    # ── Is it an update at all, and may this site take it? ───────────
    if parse_version(candidate.version) <= parse_version(site.installed_version):
        if installed_withdrawn:
            return Refusal(UP_TO_DATE, f"OpenNVR {site.installed_version} is installed, and the project has "
                                       f"withdrawn it: {installed_withdrawn}",
                           ("No newer release is out yet. Watch the release page for the fix.",))
        return Refusal(UP_TO_DATE, f"OpenNVR {site.installed_version} is installed; "
                                   f"{candidate.version} is not newer.")
    if candidate.version in revoked:
        return Refusal(REFUSED, f"OpenNVR {candidate.version} was withdrawn by the project: {revoked[candidate.version]}",
                       ("It is not installed. The next release will be offered when it is out.",))
    if candidate.channel == "edge" and not site.allow_edge:
        return Refusal(REFUSED, f"{candidate.version} is an edge build, not a release.",
                       ("Edge builds are only installed when asked for explicitly (--channel edge).",))
    if candidate.min_upgrade_from and parse_version(site.installed_version) < parse_version(candidate.min_upgrade_from):
        return Refusal(REFUSED, f"{candidate.version} cannot be installed straight over {site.installed_version}.",
                       (f"Update to {candidate.min_upgrade_from} (or later) first, then to {candidate.version}.",))

    if site.changed_files:
        shown = ", ".join(site.changed_files[:8]) + (" …" if len(site.changed_files) > 8 else "")
        return Refusal(REFUSED, f"These files in the OpenNVR folder were changed by hand: {shown}",
                       ("The update replaces them with the release's own. Move your changes out "
                        "(an override file, or .env) and run the update again.",
                        "See what changed with: git status && git diff"))

    core = candidate.image_for("ghcr.io/open-nvr/core")
    if core is None or not accepted_ids(core, site.platform):
        return Refusal(REFUSED, f"{candidate.version} has no core image for this machine ({site.platform}).")

    # ── Is the site really running the release it says it is? ────────
    if installed is not None and not site.adopt:
        problems = []
        if site.head != installed.git_commit:
            problems.append(f"the OpenNVR folder is at commit {site.head[:12]}, "
                            f"not {installed.version}'s {installed.git_commit[:12]}")
        core = installed.image_for("ghcr.io/open-nvr/core")
        if core and site.core_image_id and site.core_image_id not in accepted_ids(core, site.platform):
            problems.append(f"the running core is not the {installed.version} release image")
        if problems:
            return Refusal(REFUSED, "This site is not running a release the updater can vouch for: "
                                    + "; ".join(problems) + ".",
                           (f"To move it to {candidate.version} anyway, run the update with --adopt: "
                            "the release's files and images replace what is there now.",))
    if installed is None and not site.adopt:
        return Refusal(REFUSED, f"There is no signed record of the installed release ({site.installed_version}), "
                                "so the updater cannot tell what it would be replacing.",
                       (f"Run the update with --adopt to treat this site as {site.installed_version} "
                        f"and move it to {candidate.version}.",))

    # ── Would the new release understand this database? ─────────────
    if candidate.alembic_revisions is not None and site.db_revision and site.db_revision not in candidate.alembic_revisions:
        return Refusal(REFUSED, f"The database is at migration {site.db_revision}, which {candidate.version} "
                                "does not know: it was used by a newer build than this release.",
                       ("Wait for a release that includes this migration, or ask for help before going back.",))

    if candidate.manual_steps and not site.accept_manual:
        return Refusal(NEEDS_OPERATOR, f"{candidate.version} needs you to do something before it can be installed.",
                       (*(s.text for s in candidate.manual_steps),
                        "When that is done, run the update again with --accept-manual."))

    # ── What changes: pins and images ─────────────────────────────────
    # A value that differs from the installed release's was chosen by the
    # operator and is kept. Adopting is the exception: the site does not
    # match its record, so the release's own values replace whatever is
    # there, and the plan says what they replace.
    keep_overrides = installed is not None and not site.adopt
    warnings: list[str] = []
    pins: dict[str, str] = {}
    for key, new in candidate.pins.items():
        current = site.env.get(key, "")
        released = installed.pins.get(key) if installed else None
        if keep_overrides and current and current != released:
            warnings.append(f"{key}={current} was set by hand (the release had {released}); left as it is.")
            continue
        if current != new:
            if site.adopt and current:
                warnings.append(f"{key}={current} is replaced by the release's {new}.")
            pins[key] = new

    images: list[PlannedImage] = []
    seen: set[str] = set()
    for ref in site.site_images:
        repo = repository(ref)
        target = candidate.image_for(repo)
        if target is None or repo in seen:
            continue  # built locally, or not part of the release (compose handles it)
        seen.add(repo)
        old = installed.image_for(repo) if keep_overrides else None
        if old is not None and ref != old.ref:
            warnings.append(f"{ref} is not the image the release named ({old.ref}); left as it is.")
            continue
        accept = accepted_ids(target, site.platform)
        if not accept:
            warnings.append(f"{target.ref} has no build for {site.platform}; compose will pull it as usual.")
            continue
        images.append(PlannedImage(ref=target.ref, digest=target.digest, accept=accept))

    if installed is None:
        warnings.append(f"Adopted: this site is treated as {site.installed_version} from now on.")
    if installed_withdrawn:
        warnings.append(f"The installed {site.installed_version} was withdrawn by the project "
                        f"({installed_withdrawn}): this update replaces it.")
    if candidate.state_migrations:
        warnings.append(f"{candidate.version} changes data outside the database: if it fails to start, "
                        "it is not rolled back automatically.")

    return Plan(
        from_version=site.installed_version, to_version=candidate.version,
        kind=upgrade_kind(site.installed_version, candidate.version),
        git_commit=candidate.git_commit, git_tag=candidate.git_tag, from_commit=site.head,
        pins=pins, images=tuple(images),
        db_restore_on_rollback=installed is None or installed.alembic_head != candidate.alembic_head,
        rollback="manual" if candidate.state_migrations else "auto",
        warnings=tuple(warnings), manual_steps=tuple(s.text for s in candidate.manual_steps),
    )
