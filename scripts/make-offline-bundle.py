#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make an offline update bundle for an air-gapped OpenNVR site (UPD-501).

Run on any machine with internet, git and Docker, from an OpenNVR checkout:

    python3 scripts/make-offline-bundle.py --version 0.2.1 \\
        --platform linux/arm64 --site-images site-images.txt \\
        --out opennvr-0.2.1-arm64-offline.tar

Carry the file to the site and run ``./start.sh update --bundle <file>``
(Windows: ``.\\start.ps1 update --bundle <file>``). The site verifies every
part again before anything changes.

The bundle holds: the release's signed manifest and the signed list of
withdrawn releases; the signed manifests of the releases before it (a site
with no record of what it runs is checked against them); a git bundle of
the release tag; and the images, pulled by digest for the site's platform.

For a network of many NVRs with no internet, ``--mirror-dir DIR`` writes
into a folder to serve over HTTP(S) instead (run it once per platform):
manifests under ``vX.Y.Z/`` and ``latest/``, the withdrawn list, and
``vX.Y.Z/opennvr-<platform>-offline.tar``. Each NVR with
``UPDATE_SOURCE_URL=<that URL>`` then checks the mirror and fetches its
bundle by itself, so Update now and automatic updates work there too.

``--since VERSION`` makes a delta bundle for a site already on VERSION: only
the images that changed since it go in (often a few hundred MB instead of
several GB). The site still checks every image the release names, so one
it does not have stops the update before anything changes.

``--site-images`` is the site's own image list, so the bundle holds only
what that site runs; make it on the site with ``./start.sh update
--list-images > site-images.txt``. Without it, every image of the release
goes in (tens of GB). Images the site builds itself (the catalog apps,
``opennvr/*:local-build``) are not in any release: build them here and add
them with ``--extra-image``, or the site keeps the ones it has.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server"))

from updater import manifest, revocations, source  # noqa: E402
from updater.check import accepted_ids, repository  # noqa: E402

GIT_URL = "https://github.com/open-nvr/open-nvr.git"
API_RELEASES = "https://api.github.com/repos/open-nvr/open-nvr/releases?per_page=100"


def run(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


def earlier_versions(target: str) -> list[str]:
    """Releases before *target* that publish a manifest (GitHub only)."""
    try:
        with urllib.request.urlopen(API_RELEASES, timeout=30) as r:
            releases = json.load(r)
    except Exception as exc:  # the history is a help, not a must
        say(f"  ! could not list earlier releases ({exc}); the bundle has no history")
        return []
    out = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        names = {a.get("name") for a in rel.get("assets", [])}
        try:
            older = manifest.parse_version(tag) < manifest.parse_version(target)
        except manifest.ManifestError:
            continue
        if older and not rel.get("draft") and manifest.MANIFEST_NAME in names:
            out.append(tag.removeprefix("v"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", help="the release (default: the latest)")
    ap.add_argument("--platform", help="the site's Docker platform, e.g. linux/arm64 (default: this machine's)")
    ap.add_argument("--site-images", type=Path, help="the site's image list (./start.sh update --list-images)")
    ap.add_argument("--extra-image", action="append", default=[], help="a local image to add (repeatable)")
    ap.add_argument("--since", metavar="VERSION",
                    help="a delta bundle for a site on VERSION: only the images that changed since it")
    ap.add_argument("--source-url", help="a mirror instead of GitHub releases")
    ap.add_argument("--git-url", default=GIT_URL)
    ap.add_argument("--out", type=Path, help="the bundle file")
    ap.add_argument("--mirror-dir", type=Path,
                    help="write into a mirror folder instead (serve it, and set UPDATE_SOURCE_URL on the sites)")
    a = ap.parse_args(argv)
    if not a.out and not a.mirror_dir:
        ap.error("give --out FILE or --mirror-dir DIR")

    platform = a.platform or run("docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}")
    raw, sig = source.fetch(a.version, a.source_url)
    try:
        release = manifest.verify(raw, sig)
        say(f"OpenNVR {release.version}: signature checked")
    except manifest.ManifestError as exc:
        if "trusts no release key" not in str(exc):
            raise SystemExit(f"Refusing to bundle: {exc}") from None
        release = manifest.parse(json.loads(raw))
        say(f"OpenNVR {release.version}: this checkout trusts no release key, so the signature is checked only at the site")

    if release.channel != "stable":
        raise SystemExit("Edge builds are not bundled: an air-gapped site runs releases.")

    with tempfile.TemporaryDirectory(prefix="opennvr-bundle-") as tmp:
        out = Path(tmp)
        source.save(out, raw, sig)
        for name in (revocations.NAME, revocations.SIGNATURE_NAME):
            root = (f"{source.GITHUB_RELEASES}/latest/download" if not a.source_url
                    else f"{a.source_url.rstrip('/')}/latest")
            try:
                with urllib.request.urlopen(f"{root}/{name}", timeout=30) as r:
                    (out / name).write_bytes(r.read())
            except Exception:
                break  # none published: nothing withdrawn
        history = earlier_versions(release.version) if not a.source_url else []
        for v in history:
            try:
                source.save(out / "history" / f"v{v}", *source.fetch(v, a.source_url))
            except manifest.ManifestError as exc:
                say(f"  ! no manifest for {v}: {exc}")
        say(f"  history: {', '.join(sorted(history, key=manifest.parse_version)) or 'none'}")

        say(f"  git: {release.git_tag} from {a.git_url}")
        run("git", "clone", "--quiet", "--bare", "--single-branch", "--branch", release.git_tag, a.git_url, str(out / "git"))
        got = run("git", "rev-parse", f"{release.git_tag}^{{commit}}", cwd=out / "git")
        if got != release.git_commit:
            raise SystemExit(f"{release.git_tag} is {got}, but the manifest says {release.git_commit}")
        run("git", "bundle", "create", str(out / "repo.bundle"), release.git_tag, cwd=out / "git")
        shutil.rmtree(out / "git", ignore_errors=True)

        wanted = None
        if a.site_images:
            wanted = {repository(line.strip()) for line in a.site_images.read_text().splitlines() if line.strip()}
        unchanged: set[str] = set()
        if a.since:
            # The site already has every image the base release had: the
            # same digest means the same image, so it can stay out.
            base = manifest.parse(json.loads(source.fetch(a.since.removeprefix("v"), a.source_url)[0]))
            unchanged = {i.digest for i in base.images}
        refs = []
        for img in release.images:
            if wanted is not None and img.repository not in wanted:
                continue
            if img.digest in unchanged:
                say(f"  same as in {a.since}: {img.ref}")
                continue
            accept = accepted_ids(img, platform)
            if not accept:
                say(f"  ! {img.ref}: not built for {platform}, left out")
                continue
            say(f"  pull {img.ref} ({platform})")
            run("docker", "pull", "--quiet", "--platform", platform, f"{img.repository}@{img.digest}")
            run("docker", "tag", f"{img.repository}@{img.digest}", img.ref)
            local = run("docker", "image", "inspect", "--format", "{{.Id}}", img.ref)
            if local not in accept:
                raise SystemExit(f"{img.ref} pulled as {local}, which the manifest does not name")
            refs.append(img.ref)
        if wanted is not None:
            missing = sorted(w for w in wanted if not release.image_for(w))
            if missing:
                say("  ! not in the release (built on the site; add with --extra-image if they change): "
                    + ", ".join(missing))
        refs += a.extra_image
        say(f"  saving {len(refs)} images ...")
        try:
            # Only the site's platform: a containerd image store pulled only
            # that one, and a plain save would ask for the whole index.
            run("docker", "save", "--platform", platform, "-o", str(out / "images.tar"), *refs)
        except subprocess.CalledProcessError as exc:
            if "unknown flag" not in (exc.stderr or ""):
                raise
            run("docker", "save", "-o", str(out / "images.tar"), *refs)  # older Docker: no --platform

        target = a.out
        if a.mirror_dir:
            target = a.mirror_dir / f"v{release.version}" / bundle_name(platform)
            _fill_mirror(a.mirror_dir, out, release.version)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, "w") as t:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    t.add(p, arcname=p.relative_to(out).as_posix())
    say(f"Wrote {target} ({target.stat().st_size / 1e9:.2f} GB).")
    if a.mirror_dir:
        say(f"Serve {a.mirror_dir} over HTTP(S) and set UPDATE_SOURCE_URL=<its URL> in each site's .env.")
    else:
        say(f"On the site: ./start.sh update --bundle {target.name}")
    return 0


def bundle_name(platform: str) -> str:
    """The file a mirror serves for one platform (update-host.* fetch it by this name)."""
    return f"opennvr-{platform.replace('/', '-')}-offline.tar"


def _fill_mirror(mirror: Path, bundle: Path, version: str) -> None:
    """The mirror layout the updater reads (updater/source.py): vX.Y.Z/ and
    latest/ for manifests, latest/ for the withdrawn list, and the earlier
    releases' manifests under their own versions."""
    def copy(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for name in (manifest.MANIFEST_NAME, manifest.SIGNATURE_NAME,
                     revocations.NAME, revocations.SIGNATURE_NAME):
            if (src / name).is_file():
                shutil.copyfile(src / name, dst / name)

    copy(bundle, mirror / f"v{version}")
    for old in (bundle / "history").glob("v*") if (bundle / "history").is_dir() else ():
        copy(old, mirror / old.name)
    # latest/ follows the newest release in the mirror, not the last one added.
    current = mirror / "latest" / manifest.MANIFEST_NAME
    newest = True
    if current.is_file():
        try:
            newest = not manifest.is_newer(json.loads(current.read_bytes())["version"], version)
        except (ValueError, KeyError, manifest.ManifestError):
            newest = True
    if newest:
        copy(bundle, mirror / "latest")
    else:
        for name in (revocations.NAME, revocations.SIGNATURE_NAME):  # always the current list
            if (bundle / name).is_file():
                shutil.copyfile(bundle / name, mirror / "latest" / name)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except manifest.ManifestError as exc:
        raise SystemExit(f"Cannot make the bundle: {exc}") from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Cannot make the bundle: {' '.join(exc.cmd)} failed:\n{(exc.stderr or '').strip()}") from None
