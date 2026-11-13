# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Reading ``.env`` and writing plain ``KEY=value`` files for the host scripts."""

from __future__ import annotations

import re
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9_.:/@+,=-]*$")


def read_env(path: Path) -> dict[str, str]:
    """KEY=value pairs, the way compose and start.sh read them: comments and
    blank lines skipped, surrounding quotes dropped, the last one wins."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def write_kv(path: Path, values: dict[str, str]) -> None:
    """A file of ``KEY=value`` lines the host scripts read without a shell
    ``source``: every value must be a plain token, or this refuses to write it."""
    lines = []
    for key, value in values.items():
        value = str(value)
        if not re.match(r"^[A-Z][A-Z0-9_]*$", key) or not _SAFE.match(value):
            raise ValueError(f"refusing to write an unsafe line: {key}={value!r}")
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
