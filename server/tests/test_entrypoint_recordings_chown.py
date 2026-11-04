# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The recordings ownership fix must never hold up core's boot.

``docker-entrypoint.sh`` chowns whatever under ``/app/recordings`` is not
owned by opennvr (#243). It used to do that in the foreground, before
supervisord. On a mount where ownership does not stick (NFS with
root_squash, SMB, NTFS through Docker Desktop), every file is "not
opennvr" on every start, so every boot re-walked the whole archive and
core sat on "Fixing ownership of non-opennvr files in recordings tree..."
until its healthcheck marked it unhealthy. An operator worked around it
by mounting their own entrypoint without the line.

The walk must run in the background, and only where a probe shows chown
actually works on that mount.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")


def _recordings_block() -> str:
    match = re.search(
        r'^if \[ -d "/app/recordings" \]; then\n(.*?)^fi\n', _ENTRYPOINT, re.M | re.S
    )
    assert match, "recordings ownership block not found in docker-entrypoint.sh"
    return match.group(1)


def test_the_walk_runs_in_the_background():
    block = _recordings_block()
    sub = re.search(r"^\s*\(\n(.*?)^\s*\) &\s*$", block, re.M | re.S)
    assert sub, "the recordings find is not inside a backgrounded ( ... ) & subshell"
    assert "find /app/recordings" in sub.group(1)
    # And nowhere outside it.
    assert "find /app/recordings" not in block.replace(sub.group(0), "")


def test_the_walk_is_skipped_where_chown_does_not_stick():
    block = _recordings_block()
    probe = block.index("chown opennvr:opennvr \"$_probe\"")
    walk = block.index("find /app/recordings")
    assert probe < walk, "the chown probe must run before, and gate, the walk"
    assert 'stat -c %U "$_probe"' in block, (
        "the probe must check the resulting owner: on some mounts chown "
        "returns 0 and changes nothing"
    )
