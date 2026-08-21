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

"""
Evidence blob store — the JPEGs behind the events table.

Content-addressed (sha256-named) JPEGs under ``<recordings>/.evidence/``, so
they live on the same volume as the footage they explain, share its capacity
planning, and a re-posted identical crop costs nothing. Paths stored in the
DB are RELATIVE to the evidence root — the root can move with the recordings
volume without a data migration.

Size sanity: one visit = one crop (~30-80 KB). A busy camera seeing 500
visits/day stores ~25 MB/day — noise next to the recordings themselves.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.config import settings

_EVIDENCE_DIR = ".evidence"
# One JPEG per visit; anything above this is not a crop we produced.
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024


def evidence_root() -> Path:
    root = Path(settings.recordings_base_path) / _EVIDENCE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_evidence_jpeg(data: bytes) -> str:
    """Store a JPEG, return its relative path. Content-addressed: idempotent."""
    if not data or len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence must be 1..{MAX_EVIDENCE_BYTES} bytes")
    # Cheap magic check — we only ever store JPEGs.
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("evidence is not a JPEG")
    digest = hashlib.sha256(data).hexdigest()
    rel = f"{digest[:2]}/{digest}.jpg"
    path = evidence_root() / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".partial")
        tmp.write_bytes(data)
        tmp.replace(path)
    return rel


def resolve_evidence(rel_path: str) -> Path | None:
    """Relative DB path -> absolute file path, refusing traversal outside the
    evidence root (same discipline as the recordings resolver)."""
    root = evidence_root().resolve()
    try:
        p = (root / rel_path).resolve()
    except OSError:
        return None
    if root not in p.parents:
        return None
    return p if p.is_file() else None
