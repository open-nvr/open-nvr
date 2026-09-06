#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Pin the App Catalog: write each installable entry's ``image_digest``.

The one-click installer deploys ``image@sha256:…`` — the exact bytes a
reviewer vouched for — only when the index carries a digest. This
script asks the registry what each entry's tag resolves to right now
and writes it into ``server/config/apps_index.yml`` **textually** (the
file is mostly comments; a YAML round-trip would lose them). Run it as
the release step after the publish workflows have finished:

    python3 scripts/pin_apps_index.py                 # pin every installable entry
    python3 scripts/pin_apps_index.py --app footage-search --app alert-notifier
    python3 scripts/pin_apps_index.py --check         # exit 1 if any pin differs from the tag now
    python3 scripts/pin_apps_index.py --verify        # also cosign-verify each digest (needs cosign)
    python3 scripts/pin_apps_index.py --dry-run

Digests come from the registry's manifest endpoint (an anonymous pull
token for ghcr.io; ``Docker-Content-Digest`` of the multi-arch index),
so no Docker daemon is needed. ``--verify`` runs ``cosign verify`` with
the org's CI identity (scripts/app-installer/signing.py) so a release
never pins an unsigned image.

Standard library only. The resolver is injectable for tests.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "server" / "config" / "apps_index.yml"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "app-installer"))

_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REF_RE = re.compile(r"^(?P<registry>[^/]+)/(?P<repo>[^:@]+)(?::(?P<tag>[^@]+))?$")

Resolver = Callable[[str], str]


def split_ref(image: str) -> tuple[str, str, str]:
    """``ghcr.io/open-nvr/x:latest`` → (registry, repository, tag)."""
    m = _REF_RE.match(image.split("@", 1)[0])
    if not m:
        raise ValueError(f"not a registry image ref: {image!r}")
    registry, repo, tag = m.group("registry"), m.group("repo"), m.group("tag") or "latest"
    if "." not in registry and ":" not in registry and registry != "localhost":
        raise ValueError(f"{image!r} has no registry host (a local build tag cannot be pinned)")
    return registry, repo, tag


def _anonymous_token(registry: str, repo: str) -> str | None:
    """A pull token for a public repository (ghcr.io and Docker-Hub-style
    ``/token`` endpoints); ``None`` when the registry does not need one."""
    if registry == "ghcr.io":
        url = f"https://ghcr.io/token?scope=repository:{urllib.parse.quote(repo)}:pull"
    elif registry in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{urllib.parse.quote(repo)}:pull"
    else:
        return None
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 — fixed registry URLs
        return json.loads(resp.read().decode()).get("token")


def resolve_digest(image: str) -> str:
    """The digest ``image``'s tag points at right now (HEAD manifest)."""
    registry, repo, tag = split_ref(image)
    host = "registry-1.docker.io" if registry in ("docker.io", "index.docker.io") else registry
    req = urllib.request.Request(f"https://{host}/v2/{repo}/manifests/{tag}", method="HEAD",
                                 headers={"Accept": _ACCEPT})
    token = _anonymous_token(registry, repo)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            digest = resp.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{image}: registry answered {exc.code} — is the package public and published?") from exc
    if not _DIGEST_RE.match(digest or ""):
        raise RuntimeError(f"{image}: registry returned no usable digest ({digest!r})")
    return digest


def cosign_verify(image: str, digest: str) -> tuple[bool, str]:
    """``cosign verify`` against the signer the installer would expect."""
    from signing import expected_signer, verify_argv  # scripts/app-installer

    signer = expected_signer(image.split("@", 1)[0] + "@" + digest)
    if signer is None:
        return False, "no known signer (not a ghcr.io/open-nvr image and no 'signing' declared)"
    ref = image.split("@", 1)[0].rsplit(":", 1)[0] if ":" in image.split("/")[-1] else image
    proc = subprocess.run(verify_argv(f"{ref}@{digest}", signer),  # noqa: S603 — fixed argv
                          capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return True, "signature verified"
    return False, (proc.stderr or proc.stdout or "cosign failed").strip().splitlines()[-1]


# ── textual edit of the index ───────────────────────────────────────

_ENTRY_START = re.compile(r"^- id:\s*(?P<id>[a-z0-9-]+)\s*$")
_IMAGE_LINE = re.compile(r"^(?P<indent>\s+)image:\s*(?P<image>\S+)\s*(?P<comment>#.*)?$")
_DIGEST_LINE = re.compile(r"^(?P<indent>\s+)(?P<commented>#\s*)?image_digest:.*$")


def set_digests(text: str, digests: dict[str, str]) -> tuple[str, dict[str, str | None]]:
    """Return the index text with ``image_digest`` set for every id in
    ``digests`` — the existing (or commented-out) ``image_digest:`` line
    of that entry replaced, or a new one inserted right after its
    ``image:`` line — plus ``{id: previous digest or None}``. Every other
    byte of the file, comments included, is left alone."""
    lines = text.split("\n")
    # Entry line ranges: [start, next start).
    starts = [i for i, line in enumerate(lines) if _ENTRY_START.match(line)]
    ranges = {}
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        ranges[_ENTRY_START.match(lines[i]).group("id")] = (i, end)
    missing = [i for i in digests if i not in ranges]
    if missing:
        raise KeyError(f"entries not found in the index: {missing}")

    previous: dict[str, str | None] = {}
    edits: list[tuple[int, str | None, str]] = []      # (line index, replace|None=insert-after, text)
    for app_id, digest in digests.items():
        lo, hi = ranges[app_id]
        image_at = digest_at = None
        for i in range(lo, hi):
            if image_at is None and _IMAGE_LINE.match(lines[i]):
                image_at = i
            md = _DIGEST_LINE.match(lines[i])
            if md and digest_at is None:
                digest_at = i
                value = lines[i].split("image_digest:", 1)[1].split("#", 1)[0].strip()
                previous[app_id] = None if md.group("commented") else (value or None)
        if image_at is None:
            raise KeyError(f"no 'image:' line in entry {app_id!r}")
        indent = _IMAGE_LINE.match(lines[image_at]).group("indent")
        new_line = f"{indent}image_digest: {digest}"
        if digest_at is not None:
            edits.append((digest_at, "replace", new_line))
        else:
            previous.setdefault(app_id, None)
            edits.append((image_at, "after", new_line))

    out: list[str] = []
    by_line = {i: (kind, t) for i, kind, t in edits}
    for i, line in enumerate(lines):
        kind, t = by_line.get(i, (None, None))
        if kind == "replace":
            out.append(t)
        else:
            out.append(line)
            if kind == "after":
                out.append(t)
    return "\n".join(out), previous


def load_entries(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text()) or []
    return [e for e in raw if isinstance(e, dict) and e.get("id")]


def main(argv: list[str] | None = None, *, resolver: Resolver = resolve_digest,
         verifier=cosign_verify, index_path: Path = INDEX_PATH) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app", action="append", default=[], help="only this id (repeatable)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any entry's pin differs from what its tag resolves to now; write nothing")
    ap.add_argument("--verify", action="store_true", help="cosign-verify every digest before writing")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    ap.add_argument("--index", default=str(index_path), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    path = Path(args.index)

    entries = [e for e in load_entries(path) if e.get("kind", "installable") != "external"]
    if args.app:
        unknown = sorted(set(args.app) - {e["id"] for e in entries})
        if unknown:
            print(f"error: not installable entries: {unknown}", file=sys.stderr)
            return 2
        entries = [e for e in entries if e["id"] in args.app]

    digests: dict[str, str] = {}
    rc = 0
    for e in entries:
        image = str(e.get("image") or "")
        try:
            digest = resolver(image)
        except Exception as exc:  # noqa: BLE001 — report per entry, keep going
            print(f"  {e['id']}: FAILED — {exc}", file=sys.stderr)
            rc = 1
            continue
        if args.verify:
            ok, why = verifier(image, digest)
            if not ok:
                print(f"  {e['id']}: UNSIGNED — {why} (not pinned)", file=sys.stderr)
                rc = 1
                continue
        current = e.get("image_digest")
        if args.check:
            if current != digest:
                print(f"  {e['id']}: pin {current or '(none)'} != tag now {digest}")
                rc = 1
            else:
                print(f"  {e['id']}: pinned and current")
            continue
        digests[e["id"]] = digest
        mark = "unchanged" if current == digest else ("was " + current if current else "NEW")
        print(f"  {e['id']}: {digest}  ({mark})")

    if args.check or args.dry_run or not digests:
        return rc
    text, _previous = set_digests(path.read_text(), digests)
    path.write_text(text)
    print(f"wrote {len(digests)} pin(s) to {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
