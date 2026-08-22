# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
SQLite-backed searchable index of inference events for footage-search.

Each indexed row is one inference event keyframe: which camera, when,
the correlation_id (so the operator can pull the exact recorded segment
from OpenNVR), the adapter that produced it, the detected object labels,
and — if a captioning adapter (BLIP) ran on the same frame — a natural-
language scene caption. Search runs over labels AND caption text, which
is what lets "red truck" match: ``truck`` from the detector's labels,
``red`` from the caption.

SQLite is deliberate: a single file, no server, queryable offline,
trivially portable. For very large deployments swap this module for a
real search backend behind the same ``FootageStore`` interface — the
rest of the example doesn't care.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class Keyframe:
    """One searchable inference keyframe."""

    camera_id: str
    ts: float                # POSIX seconds (event completed_at)
    correlation_id: str
    adapter: str
    labels: list[str]        # object labels present in the frame
    caption: str             # scene caption, or "" if none


@dataclass
class SearchResult:
    camera_id: str
    ts: float
    correlation_id: str
    adapter: str
    labels: list[str]
    caption: str


class FootageStore:
    """Append-and-query index over inference keyframes.

    Thread-safety: one connection per process; the indexer writes from
    a single asyncio task and ``search`` is called from the CLI in a
    separate invocation, so we don't share a connection across threads.
    ``check_same_thread=False`` is set anyway so a future combined
    process doesn't trip over it.
    """

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS keyframes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id      TEXT NOT NULL,
                ts             REAL NOT NULL,
                correlation_id TEXT NOT NULL,
                adapter        TEXT NOT NULL,
                labels         TEXT NOT NULL,   -- space-separated, lowercased
                caption        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_keyframes_ts ON keyframes(ts);
            CREATE INDEX IF NOT EXISTS idx_keyframes_cam ON keyframes(camera_id);
            """
        )
        self._conn.commit()

    def add(self, kf: Keyframe) -> None:
        """Index a keyframe, MERGING with any existing keyframe that
        shares its ``(camera_id, correlation_id)``.

        Detection events and caption events for the same frame arrive as
        separate inference events (one per adapter) but carry the same
        correlation_id. Merging them onto one row is what lets a query
        like "red truck" match — ``truck`` from the detector's labels and
        ``red`` from the captioner's text end up on the same row. A
        keyframe with no correlation_id can't be merged, so it's always
        inserted fresh.
        """
        labels = " ".join(sorted({s.lower() for s in kf.labels}))
        caption = kf.caption.lower()

        if kf.correlation_id:
            existing = self._conn.execute(
                "SELECT id, labels, caption, adapter FROM keyframes "
                "WHERE camera_id = ? AND correlation_id = ?",
                (kf.camera_id, kf.correlation_id),
            ).fetchone()
            if existing is not None:
                merged_labels = " ".join(
                    sorted(set(existing["labels"].split()) | set(labels.split()))
                )
                # Keep both captions if distinct (different adapters may
                # describe the frame differently); de-dup identical ones.
                old_cap = existing["caption"]
                if old_cap and caption and caption not in old_cap:
                    merged_caption = f"{old_cap} {caption}".strip()
                else:
                    merged_caption = caption or old_cap
                adapters = existing["adapter"]
                if kf.adapter and kf.adapter not in adapters.split(","):
                    adapters = f"{adapters},{kf.adapter}"
                self._conn.execute(
                    "UPDATE keyframes SET labels = ?, caption = ?, adapter = ? "
                    "WHERE id = ?",
                    (merged_labels, merged_caption, adapters, existing["id"]),
                )
                self._conn.commit()
                return

        self._conn.execute(
            "INSERT INTO keyframes "
            "(camera_id, ts, correlation_id, adapter, labels, caption) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kf.camera_id, kf.ts, kf.correlation_id, kf.adapter, labels, caption),
        )
        self._conn.commit()

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM keyframes")
        return int(cur.fetchone()["n"])

    def search(
        self,
        *,
        labels: Sequence[str] = (),
        keywords: Sequence[str] = (),
        since: float | None = None,
        until: float | None = None,
        camera_id: str | None = None,
        limit: int = 50,
    ) -> list[SearchResult]:
        """Find keyframes matching the filter.

        Matching semantics:
        * ``labels`` — every requested label must appear in the row's
          labels (AND across labels). Empty → no label constraint.
        * ``keywords`` — every keyword must appear in labels OR caption
          (AND across keywords; each keyword OR's across the two text
          columns). This is what makes "red truck" work when "truck" is
          a label and "red" is only in the caption.
        * ``since`` / ``until`` — POSIX-second time window (inclusive).
        * ``camera_id`` — exact camera match.

        Results are newest-first, capped at ``limit``.
        """
        clauses: list[str] = []
        params: list[object] = []

        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        for label in labels:
            # word-boundary-ish match against the space-separated labels
            clauses.append("(' ' || labels || ' ') LIKE ?")
            params.append(f"% {label.lower()} %")
        for kw in keywords:
            clauses.append("(labels LIKE ? OR caption LIKE ?)")
            params.append(f"%{kw.lower()}%")
            params.append(f"%{kw.lower()}%")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT camera_id, ts, correlation_id, adapter, labels, caption "
            f"FROM keyframes{where} ORDER BY ts DESC LIMIT ?"
        )
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SearchResult(
                camera_id=r["camera_id"], ts=r["ts"],
                correlation_id=r["correlation_id"], adapter=r["adapter"],
                labels=r["labels"].split() if r["labels"] else [],
                caption=r["caption"],
            )
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


def keyframe_from_event(event: dict) -> Keyframe | None:
    """Build a Keyframe from an OpenNVR inference event, or None if the
    event carries nothing searchable (no labels and no caption)."""
    if not isinstance(event, dict):
        return None
    camera_id = event.get("camera_id")
    if not camera_id:
        return None
    result = event.get("result")
    result = result if isinstance(result, dict) else {}

    labels: list[str] = []
    detections = result.get("detections")
    if isinstance(detections, list):
        for det in detections:
            if isinstance(det, dict):
                lab = det.get("label") or det.get("class")
                if lab:
                    labels.append(str(lab))

    # Tier-0 events carry their objects as top-level ``tracks`` and have no
    # ``result`` block at all — the always-on detector is the ONLY stream a
    # default install produces, so without this branch the index stays
    # permanently empty on a stock system. Labels are de-duplicated: one
    # frame with four people is searchable as "person", not "person" x4.
    if not labels:
        tracks = event.get("tracks")
        if isinstance(tracks, list):
            seen: set[str] = set()
            for tr in tracks:
                if not isinstance(tr, dict):
                    continue
                lab = tr.get("label")
                if lab and str(lab) not in seen:
                    seen.add(str(lab))
                    labels.append(str(lab))

    caption = result.get("caption")
    caption = str(caption).strip() if isinstance(caption, str) else ""

    if not labels and not caption:
        return None

    return Keyframe(
        camera_id=str(camera_id),
        # Tier-0 stamps the FRAME's epoch seconds in ``ts``; adapter events
        # carry an ISO ``completed_at``. Prefer whichever the event has so a
        # search for "around 3am" lands on when it was SEEN, not when it was
        # indexed.
        ts=_event_ts(event.get("completed_at"), event.get("wall_ts")),
        correlation_id=str(event.get("correlation_id") or ""),
        adapter=str(event.get("adapter") or "unknown"),
        labels=labels,
        caption=caption,
    )


def _event_ts(raw: object, wall_ts: object = None) -> float:
    """Best available WALL-CLOCK stamp for an event.

    ``raw`` is an adapter event's ISO ``completed_at``; ``wall_ts`` is
    Tier-0's epoch-seconds stamp. Tier-0's other time field (``ts``) is a
    ``time.monotonic()`` reading and is deliberately NOT accepted here —
    storing it would date every keyframe from the machine's boot origin,
    breaking every time-window search. Falls back to now: indexing happens
    within milliseconds of the event.
    """
    import datetime as _dt
    if isinstance(raw, str):
        try:
            ts = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            return ts.timestamp()
        except ValueError:
            pass
    # Epoch seconds (Tier-0's wall_ts). Bounded below by 2001-01-01: a
    # smaller number is a monotonic reading that leaked in, not a date.
    if isinstance(wall_ts, (int, float)) and wall_ts > 978_307_200:
        return float(wall_ts)
    return time.time()
