#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed offline bundles for the upgrade test's releases (tests/upgrade/run.sh).

    make_bundles.py WORK VERSION...

WORK/rel is a clone holding a vVERSION tag per release, each built into
ghcr.io/open-nvr/core:VERSION. Writes WORK/opennvr-VERSION-offline.tar in
the offline bundle format (release-manifest.json + .sig, history/ with the
earlier releases' manifests, repo.bundle, images.tar) and WORK/keys.json,
the throwaway test key to pass as OPENNVR_UPDATE_EXTRA_KEYS. The project's
release key is never involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "server"), str(REPO / "scripts" / "release")]

import make_manifest as mk  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from updater import manifest  # noqa: E402


def run(*cmd: str, cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    work, versions = Path(sys.argv[1]).resolve(), sys.argv[2:]
    rel = work / "rel"
    key = Ed25519PrivateKey.generate()
    (work / "keys.json").write_text(json.dumps({"keys": [manifest.public_key_entry(key, "upgrade-test")]}))
    platform = run("docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", cwd=work)
    signed: dict[str, tuple[bytes, bytes]] = {}
    for v in versions:
        commit = run("git", "rev-parse", f"v{v}^{{commit}}", cwd=rel)
        wt = work / f"wt-{v}"
        if not wt.exists():
            run("git", "worktree", "add", "-q", "--detach", str(wt), f"v{v}", cwd=rel)
        head, revisions = mk.alembic_graph(wt / "server" / "migrations" / "versions")
        image = f"ghcr.io/open-nvr/core:{v}"
        image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image, cwd=work)
        doc = {"schema": 1, "product": "opennvr", "version": v, "channel": "stable", "min_updater": 1,
               "git_commit": commit, "git_tag": f"v{v}", "alembic_head": head, "alembic_revisions": revisions,
               "pins": {"CORE_TAG": v},
               # Built here, never pushed: the local ID stands in for the
               # registry digest (on a containerd store they are the same).
               "images": [{"ref": image, "digest": image_id, "ids": {platform: image_id}}]}
        raw = (json.dumps(doc, indent=2) + "\n").encode()
        sig = manifest.sign(raw, key, "upgrade-test")
        manifest.verify(raw, sig, {"upgrade-test": key.public_key()})
        signed[v] = (raw, sig)

        out = work / "bundles" / v
        out.mkdir(parents=True, exist_ok=True)
        (out / manifest.MANIFEST_NAME).write_bytes(raw)
        (out / manifest.SIGNATURE_NAME).write_bytes(sig)
        for earlier in versions[:versions.index(v)]:
            h = out / "history" / f"v{earlier}"
            h.mkdir(parents=True, exist_ok=True)
            (h / manifest.MANIFEST_NAME).write_bytes(signed[earlier][0])
            (h / manifest.SIGNATURE_NAME).write_bytes(signed[earlier][1])
        run("git", "bundle", "create", str(out / "repo.bundle"), f"v{v}", cwd=rel)
        run("docker", "save", "-o", str(out / "images.tar"), image, cwd=work)
        tar = work / f"opennvr-{v}-offline.tar"
        with tarfile.open(tar, "w") as t:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    t.add(p, arcname=p.relative_to(out).as_posix())
        # The unpacked copy would double the disk the test needs (images.tar).
        shutil.rmtree(out)
        print(f"{tar.name}: {tar.stat().st_size / 1e9:.2f} GB, commit {commit[:8]}, migration head {head}")
    shutil.rmtree(work / "bundles", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
