# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Read-only reader for the footage-search SQLite index.

The ``footage-search`` example builds an index of recorded inference
keyframes (object labels + scene captions, keyed by camera + time +
correlation_id). This module lets the camera-agent's ``search_footage``
tool query that same index so the user can ask the voice agent about the
*past* in natural language — "did a red truck come by the dock earlier?"
— not just the live frame.

It is intentionally a tiny, dependency-free (stdlib ``sqlite3``)
read-only view. If the index file doesn't exist yet (the indexer hasn't
run), ``available`` is False and the tool reports that cleanly rather
than erroring. The LLM does the natural-language → keywords/time
decomposition, so this reader only needs to run a structured filter.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class IndexHit:
    camera_id: str
    ts: float
    correlation_id: str
    labels: list[str]
    caption: str


class FootageIndex:
    """Read-only query view over the footage-search SQLite index."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_open()

    def _ensure_open(self) -> bool:
        """Open (or re-try opening) the index. Lazy on purpose: on a stock
        install the agent and the footage-search indexer start together, and
        the agent usually probes BEFORE the indexer has created its schema.
        A one-shot probe at boot would leave ``search_footage`` reporting
        "unavailable" forever until an agent restart — so every call re-tries
        until the index exists. Cheap when it doesn't: a Path.exists check."""
        if self._conn is not None:
            return True
        if not Path(self._path).exists():
            return False
        try:
            # Open read-only via URI so we never create or mutate the
            # indexer's database from the agent process.
            conn = sqlite3.connect(
                f"file:{self._path}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error:
            return False
        conn.row_factory = sqlite3.Row
        try:
            # Probe the expected table; the file can exist before the
            # indexer has created its schema — treat that as not-yet.
            conn.execute("SELECT 1 FROM keyframes LIMIT 1")
        except sqlite3.Error:
            conn.close()
            return False
        self._conn = conn
        return True

    @property
    def available(self) -> bool:
        return self._ensure_open()

    def search(
        self,
        *,
        keywords: list[str],
        within_minutes: float | None = None,
        camera_id: str | None = None,
        limit: int = 6,
    ) -> list[IndexHit]:
        """Find keyframes where every keyword appears in the labels or
        the caption, optionally within a recent time window and on one
        camera. Newest-first."""
        if not self._ensure_open():
            return []
        clauses: list[str] = []
        params: list[object] = []
        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if within_minutes is not None and within_minutes > 0:
            clauses.append("ts >= ?")
            params.append(time.time() - within_minutes * 60.0)
        for kw in keywords:
            kw = str(kw).strip().lower()
            if not kw:
                continue
            clauses.append("(labels LIKE ? OR caption LIKE ?)")
            params.append(f"%{kw}%")
            params.append(f"%{kw}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT camera_id, ts, correlation_id, labels, caption "
            f"FROM keyframes{where} ORDER BY ts DESC LIMIT ?"
        )
        params.append(int(limit))
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            # A failing query (index file replaced or truncated mid-read)
            # drops the handle so the next call re-opens fresh.
            self.close()
            return []
        return [
            IndexHit(
                camera_id=r["camera_id"], ts=r["ts"],
                correlation_id=r["correlation_id"],
                labels=r["labels"].split() if r["labels"] else [],
                caption=r["caption"],
            )
            for r in rows
        ]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
