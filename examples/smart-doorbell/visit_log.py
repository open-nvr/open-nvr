# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Who came to the door, kept across restarts.

The doorbell's "Recent visitors" feed and its stranger wall were both
``collections.deque`` — 25 entries and 8 tiles, in memory. Which meant
the app answered "who is at the door now" perfectly and "who came to my
door three days ago" not at all: a redeploy, a crash or a `docker
compose up` wiped every stranger it had ever seen. For a doorbell that
is not a missing nicety, it is the question.

Where the history lives, and why not in the event store
-------------------------------------------------------
The platform's canonical event store is where visits belong, and
everything else in OpenNVR has been moved onto it. This app cannot use
it for the identity, and the reason is worth stating so nobody
"improves" it later: a ``FrameApp`` polls snapshots. It has a frame and
a face; it has no ``event_id`` and no ``track_id``. Attaching a name to
a platform visit would mean guessing which visit the frame belonged to
by matching timestamps, and a guessed identity written into the shared
store is indistinguishable, afterwards, from a measured one. That is the
exact failure the descriptor store's confidence column exists to
prevent.

So the identity stays the app's own record, in the SDK's durable
per-app key/value store — which is in core, and survives restarts and
redeploys.

Text for everyone, a picture for the newest few
-----------------------------------------------
The whole log is one JSON value, rewritten on every visit, so what goes
in it has to stay small. Each visit costs a line of text — when, which
camera, recognised or not, and who. Only the most recent unrecognised
visits also carry a thumbnail, because those are the tiles an operator
actually looks at; an older stranger keeps the line and loses the
picture, and the wall says "snapshot aged out" rather than showing a
broken tile. The visit still happened, and knowing somebody came at
03:10 on Tuesday is worth more than nothing once the photo has gone.

The full-size crop goes to the platform's evidence store all the same
(``nvr.save_evidence()``), and its path is recorded here, because that
is where alerts already cite their pictures and where the platform's own
retention governs them. Reading one back by path needs an app-scoped
evidence GET that core does not have yet; until it does, this log does
not depend on it — which is why the thumbnail is kept here rather than
fetched.

Best-effort, always
-------------------
Every method swallows its own failures. A doorbell whose alert does not
fire because a key/value write timed out is worse than a doorbell with a
gap in its history.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

logger = logging.getLogger("smart_doorbell.visit_log")

#: One key, one JSON list. The whole log is rewritten per visit, which is
#: why it is capped hard below: at a few hundred compact entries this is
#: tens of kilobytes, and a doorbell sees visits at human rates, not
#: frame rates.
STATE_KEY = "visit_log"

#: Hard ceilings, independent of the configured ones. A config that says
#: "keep 100000" must not turn every visit into a megabyte round-trip.
MAX_ENTRIES = 1000
MAX_DAYS = 365

#: How many of the newest strangers keep their thumbnail. Everything past
#: this keeps its line and drops the picture — the one field in an entry
#: that is kilobytes rather than bytes.
MAX_THUMBNAILS = 12


def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


class VisitLog:
    """The durable list of visits, newest last.

    Reads are served from an in-memory mirror so the dashboard poll never
    waits on core; writes go through to the store. The mirror is loaded
    once, on first use.
    """

    def __init__(self, nvr: Any, *, max_entries: int = 200,
                 max_days: int = 30) -> None:
        self._nvr = nvr
        self._max_entries = _clamp(max_entries, 1, MAX_ENTRIES, 200)
        self._max_days = _clamp(max_days, 1, MAX_DAYS, 30)
        self._entries: list[dict[str, Any]] = []
        self._loaded = False

    # ── loading ────────────────────────────────────────────────────

    def load(self) -> list[dict[str, Any]]:
        """Read the stored log once. A failure leaves the log EMPTY and
        marked loaded: retrying on every poll would turn an unreachable
        core into a request storm, and the app works without history."""
        if self._loaded:
            return self._entries
        self._loaded = True
        try:
            raw = self._nvr.state.get(STATE_KEY, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("visit history unavailable (%s)", exc)
            return self._entries
        if isinstance(raw, list):
            self._entries = [e for e in raw if isinstance(e, dict)]
            self._entries = self._prune(self._entries)
        return self._entries

    @property
    def entries(self) -> list[dict[str, Any]]:
        return self.load()

    # ── writing ────────────────────────────────────────────────────

    def record(self, entry: dict[str, Any], crop: bytes | None = None) -> dict[str, Any]:
        """Append one visit, uploading its crop if there is one.

        Returns the stored entry (with ``evidence_path`` filled in when
        the upload worked), so the caller can put the same record on the
        live wall without a second round-trip.
        """
        self.load()
        stored = dict(entry)
        stored.setdefault("at", time.time())
        if crop:
            stored["evidence_path"] = self._save(crop)
        self._entries.append(stored)
        self._entries = self._prune(self._entries)
        self._flush()
        return stored

    def _drop_old_thumbnails(self, entries: list[dict[str, Any]]) -> None:
        """Keep a picture on the newest strangers only, in place."""
        kept = 0
        for entry in reversed(entries):
            if not entry.get("thumb"):
                continue
            kept += 1
            if kept > MAX_THUMBNAILS:
                # The line stays, so the visit is still in the history;
                # only the kilobytes go.
                entry.pop("thumb", None)
                entry["thumb_dropped"] = True

    def forget(self, visit_id: str) -> bool:
        """Drop one visit — used when a stranger is enrolled and stops
        being a stranger. The tile must not come back on the next
        restart."""
        self.load()
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.get("id") != visit_id]
        if len(self._entries) == before:
            return False
        self._flush()
        return True

    # ── reading ────────────────────────────────────────────────────

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        """Newest first — the order a feed is read in."""
        return list(reversed(self.entries))[:max(0, int(limit))]

    def strangers(self, limit: int = 8) -> list[dict[str, Any]]:
        return [e for e in self.recent(len(self.entries))
                if not e.get("recognized")][:max(0, int(limit))]

    def entry(self, visit_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e.get("id") == visit_id), None)

    def crop(self, visit_id: str) -> bytes | None:
        """The FULL-SIZE face crop for a visit, fetched back from the
        platform's evidence store.

        Not the wall thumbnail. The thumbnail is ~190px and lives in the
        log so the wall can draw without a round trip; this is the 320px
        crop the enroller needs, and teaching the adapter the thumbnail
        instead would quietly give it a worse face than the operator
        believes they handed over.

        ``None`` when the picture is gone — retention sweeps evidence,
        and the caller says "no longer available" rather than enrolling
        from something else.
        """
        path = (self.entry(visit_id) or {}).get("evidence_path")
        if not path:
            return None
        try:
            return self._nvr.read_evidence(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.debug("evidence %s unreadable (%s)", path, exc)
            return None

    # ── internals ──────────────────────────────────────────────────

    def _save(self, crop: bytes) -> str | None:
        try:
            return self._nvr.save_evidence(crop)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crop upload failed (%s)", exc)
            return None

    def _prune(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        cutoff = time.time() - self._max_days * 86400
        kept = [e for e in entries
                if isinstance(e.get("at"), (int, float)) and e["at"] >= cutoff]
        # Entries with no usable timestamp are dropped rather than kept
        # forever: an undated visit cannot be aged out, and one bad write
        # would otherwise pin a row in the log for the life of the site.
        kept = kept[-self._max_entries:]
        self._drop_old_thumbnails(kept)
        return kept

    def _flush(self) -> None:
        try:
            self._nvr.state.set(STATE_KEY, self._entries)
        except Exception as exc:  # noqa: BLE001
            # The in-memory mirror keeps the dashboard right for this
            # process; only the durability is lost, and the next
            # successful write restores it.
            logger.warning("visit history not saved (%s)", exc)
