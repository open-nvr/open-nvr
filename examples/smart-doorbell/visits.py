# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
One decision per visit, not one per frame.

The doorbell polls a frame a second. The first frame of an approach is
the worst one: side-on, blurred, half out of shot. Judged alone it is
"no match", and a doorbell that announced a stranger on it would then,
one clean frame later, announce the same person by name. Every product
that does this well — Frigate scores a tracked person over the whole
track, Double Take retries up to ten frames before calling anyone
unknown, the Dahua and Hikvision recorders pick one best shot and
compare once — decides per VISIT. So does this.

A visit opens on the first face a camera sees and runs for a short
hold. A confident match ends the hold at once: nobody waits to be
greeted. No match by the end of the hold means the visit is decided
from its best frame, and the answer is one of two things:

* ``unknown`` — nobody enrolled came close. A stranger, alerted.
* ``uncertain`` — the nearest enrolled face was within the band below
  the threshold. That is a known person seen badly far more often than
  a stranger who resembles one, so it is recorded on the wall (which
  is where the operator enrols the missing angle from) and NOT
  alerted. The band is ``unknown_score``; set it to 0 to switch the
  band off and alert on every miss.

A visit already announced as a stranger that matches later is
UPGRADED: the name goes out carrying the id of the alert it
supersedes, so a relay that can replace a notification does, and the
stranger tile comes off the wall. The ring does not repeat; the
visitor was already rung for.

Frames below the quality gate — a face narrower than ``min_face_px``
or one the detector was not sure of — neither open nor decide a
visit. They are the frames the mistakes come from.

A name is a VOTE, not a verdict, when ``min_match_reads`` is above 1:
each matching frame adds its similarity, weighted by the size of the
face it was read from, to that person's tally, and the visit is
announced as known once the leading person has that many matching
frames. A lone match that never repeats within the hold is recorded
as ``uncertain`` (leaning to that name) rather than announced, and
the next matching frame upgrades it. The default of 1 keeps the
first match deciding at once.

This module holds the decision and nothing else: no alerts, no tiles,
no clock of its own. The app feeds it reads and a time and acts on
what comes back, which is what makes the rule testable in isolation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("smart_doorbell.visits")

#: Seconds a visit waits for a match before deciding from its best
#: frame. Four polled frames at the default rate.
DEFAULT_HOLD_SECONDS: float = 4.0
#: Nearest-similarity floor for ``uncertain``. Below it, nobody we know
#: came close; at or above it (and below the threshold) the face is
#: probably somebody enrolled, seen badly.
DEFAULT_UNKNOWN_SCORE: float = 0.35
#: Faces narrower than this many pixels are not decided from. The
#: frames are the camera's main stream, so 100 px is a face within a
#: few metres of the door; below it "no match" is not evidence of a
#: stranger, only of distance. A match is never gated by size.
DEFAULT_MIN_FACE_PX: int = 100
#: Matching frames a person needs before the visit is announced as
#: known. 1 = the first match decides at once.
DEFAULT_MIN_MATCH_READS: int = 1
#: Face side (px) above which a frame's vote weighs no more.
VOTE_WEIGHT_CAP_PX: int = 200
#: Detector confidence below which a face is not decided from.
DEFAULT_MIN_FACE_CONFIDENCE: float = 0.6
#: A visit with no face for this long is over; the next face is a new
#: visit. Longer than the hold, shorter than the alert dedup.
DEFAULT_GAP_SECONDS: float = 10.0


@dataclass
class Observation:
    """One frame with a face on it, as the decision will remember it."""
    read: Any            # FaceRead
    frame: bytes
    at: float            # monotonic
    wall: float          # time.time()

    @property
    def area(self) -> int:
        b = self.read.face_bbox
        return (b[2] - b[0]) * (b[3] - b[1]) if b else 0

    @property
    def side(self) -> int:
        """The smaller side of the face box; 0 without one."""
        b = self.read.face_bbox
        return max(0, min(b[2] - b[0], b[3] - b[1])) if b else 0


@dataclass
class Tally:
    """One person's matching frames on a visit."""
    weighted: float = 0.0        # Σ similarity × weight
    weight: float = 0.0          # Σ weight (face side, capped)
    reads: int = 0
    best: Observation | None = None   # the closest match, to announce from

    @property
    def mean(self) -> float:
        return self.weighted / self.weight if self.weight > 0 else 0.0


@dataclass
class Visit:
    camera_id: str
    opened_at: float
    opened_wall: float
    best: Observation
    last_face_at: float
    reads: int = 1
    #: Highest nearest-similarity seen on any unrecognised read — the
    #: band test. The best FRAME is chosen by size, not by this.
    max_similarity: float | None = None
    #: Matching frames per person_id. The leader by weighted mean
    #: similarity is the name, once it has ``min_match_reads`` frames.
    tallies: dict[str, Tally] = field(default_factory=dict)
    #: None until decided; then "known" / "unknown" / "uncertain".
    decided: str | None = None
    person_id: str | None = None
    #: Set by the app once it has published, so an upgrade can say what
    #: it supersedes and which tile to take down.
    alert_id: str | None = None
    tile_id: str | None = None
    rang: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.camera_id}@{int(self.opened_wall)}"

    @property
    def leader(self) -> str | None:
        """The person with the highest weighted mean similarity so far."""
        if not self.tallies:
            return None
        return max(self.tallies, key=lambda pid: (self.tallies[pid].mean, self.tallies[pid].reads))


@dataclass(frozen=True)
class Decision:
    """What the app must now do about a visit.

    ``kind`` is ``known`` (announce the name), ``unknown`` (announce a
    stranger), ``uncertain`` (record, do not alert) or ``upgrade`` (a
    visit already announced as unknown/uncertain has a name now).
    ``obs`` is the frame to act from: the matching one for a name, the
    largest face seen for a stranger.
    """
    kind: str
    visit: Visit
    obs: Observation
    #: The same verdict again for a visit already decided — subject to
    #: the app's dedup window, never a new tile or line unless it fires.
    repeat: bool = False

    @property
    def read(self) -> Any:
        return self.obs.read


class VisitTracker:
    """Open visits per camera, and the rule that decides them."""

    def __init__(
        self,
        *,
        hold_seconds: float = DEFAULT_HOLD_SECONDS,
        unknown_score: float = DEFAULT_UNKNOWN_SCORE,
        min_face_px: int = DEFAULT_MIN_FACE_PX,
        min_face_confidence: float = DEFAULT_MIN_FACE_CONFIDENCE,
        gap_seconds: float = DEFAULT_GAP_SECONDS,
        min_match_reads: int = DEFAULT_MIN_MATCH_READS,
    ) -> None:
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.unknown_score = max(0.0, float(unknown_score))
        self.min_face_px = max(0, int(min_face_px))
        self.min_face_confidence = max(0.0, float(min_face_confidence))
        self.gap_seconds = max(0.0, float(gap_seconds))
        self.min_match_reads = max(1, int(min_match_reads))
        self._open: dict[str, Visit] = {}

    # ── Introspection ───────────────────────────────────────────────

    @property
    def open_visits(self) -> list[Visit]:
        return list(self._open.values())

    def settings(self) -> dict[str, Any]:
        return {
            "hold_seconds": self.hold_seconds,
            "unknown_score": self.unknown_score,
            "min_face_px": self.min_face_px,
            "min_face_confidence": self.min_face_confidence,
            "gap_seconds": self.gap_seconds,
            "min_match_reads": self.min_match_reads,
        }

    # ── The quality gate ────────────────────────────────────────────

    def usable(self, read: Any) -> bool:
        """Is this frame one to decide from?

        A recognised face is always usable: the adapter matched it above
        the threshold and that is the whole point. An unrecognised one
        must be big enough and sure enough that "no match" means
        something. Missing numbers (an older adapter) pass; the gate
        cannot refuse what it was not told.
        """
        if not read.face_detected:
            return False
        if read.recognized:
            return True
        bbox = read.face_bbox
        if (bbox is not None and self.min_face_px > 0
                and min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < self.min_face_px):
            return False
        conf = read.confidence
        return not (conf is not None and self.min_face_confidence > 0
                    and conf < self.min_face_confidence)

    # ── Feeding frames ──────────────────────────────────────────────

    def observe(self, camera_id: str, read: Any, frame: bytes, *,
                now: float, wall: float) -> list[Decision]:
        """One frame with a face on it. Returns the decisions it caused —
        usually none (the hold is running), sometimes one (a match, or
        an immediate decision when the hold is 0)."""
        if not self.usable(read):
            return []
        obs = Observation(read=read, frame=frame, at=now, wall=wall)
        visit = self._open.get(camera_id)
        if visit is not None and now - visit.last_face_at > self.gap_seconds:
            # Whoever that was has gone; this is somebody new.
            self._open.pop(camera_id, None)
            visit = None

        out: list[Decision] = []
        if visit is None:
            visit = Visit(camera_id=camera_id, opened_at=now, opened_wall=wall,
                          best=obs, last_face_at=now)
            self._open[camera_id] = visit
        else:
            visit.reads += 1
            visit.last_face_at = now
            if self._better(obs, visit.best):
                visit.best = obs
        if (not read.recognized and read.similarity is not None
                and (visit.max_similarity is None
                     or read.similarity > visit.max_similarity)):
            visit.max_similarity = read.similarity

        if read.recognized:
            pid = read.person_id or ""
            tally = self._vote(visit, pid, obs)
            leader = visit.leader
            if visit.decided is None:
                if leader == pid and tally.reads >= self.min_match_reads:
                    visit.decided, visit.person_id = "known", pid
                    out.append(Decision("known", visit, tally.best or obs))
                # else: a vote; the hold keeps looking for the next frame
            elif visit.decided in ("unknown", "uncertain"):
                if leader == pid and tally.reads >= self.min_match_reads:
                    visit.decided, visit.person_id = "known", pid
                    out.append(Decision("upgrade", visit, tally.best or obs))
            elif visit.person_id != pid:
                # A second person at the same door, once they have the
                # same number of matching frames the first one needed.
                # The first visit is over; this one is decided on the spot.
                if tally.reads >= self.min_match_reads:
                    visit = Visit(camera_id=camera_id, opened_at=now, opened_wall=wall,
                                  best=obs, last_face_at=now, decided="known",
                                  person_id=pid)
                    visit.tallies[pid] = tally
                    self._open[camera_id] = visit
                    out.append(Decision("known", visit, tally.best or obs))
            else:
                # The same person, still there. Offered again as a
                # repeat; the app's dedup window says whether anybody
                # hears it twice (its 0 means "every read", as before).
                out.append(Decision("known", visit, obs, repeat=True))
        elif visit.decided is None and self.hold_seconds <= 0:
            out.append(self._decide_undecided(visit))
        elif visit.decided == "unknown":
            out.append(Decision("unknown", visit, obs, repeat=True))
        # A miss on a visit decided "uncertain" or "known" changes
        # nothing: the first never alerts, the second has a name.
        return out

    def settle(self, now: float) -> list[Decision]:
        """Time passing. Decides visits whose hold has run out and
        forgets visits nobody has been seen at for the gap."""
        out: list[Decision] = []
        for camera_id, visit in list(self._open.items()):
            if visit.decided is None and now - visit.opened_at >= self.hold_seconds:
                out.append(self._decide_undecided(visit))
            if visit.decided is not None and now - visit.last_face_at > self.gap_seconds:
                self._open.pop(camera_id, None)
        return out

    def forget(self, camera_id: str) -> None:
        self._open.pop(camera_id, None)

    # ── Internals ───────────────────────────────────────────────────

    @staticmethod
    def _better(new: Observation, old: Observation) -> bool:
        """The frame a visit is decided and shown from. A match beats a
        miss; between matches the closer one; between misses the
        bigger face, then the surer one."""
        nr, orr = bool(new.read.recognized), bool(old.read.recognized)
        if nr != orr:
            return nr
        if nr:
            return (new.read.similarity or 0.0) > (old.read.similarity or 0.0)
        if new.area != old.area:
            return new.area > old.area
        return (new.read.confidence or 0.0) > (old.read.confidence or 0.0)

    @staticmethod
    def _vote(visit: Visit, pid: str, obs: Observation) -> Tally:
        """Add a matching frame to ``pid``'s tally: its similarity
        weighted by the face's size (a 200 px face counts twice a 100 px
        one; a frame without a box counts as the smallest)."""
        tally = visit.tallies.get(pid)
        if tally is None:
            tally = visit.tallies[pid] = Tally()
        weight = float(min(max(obs.side, 1), VOTE_WEIGHT_CAP_PX))
        sim = float(obs.read.similarity or 0.0)
        tally.weighted += sim * weight
        tally.weight += weight
        tally.reads += 1
        if tally.best is None or sim > float(tally.best.read.similarity or 0.0):
            tally.best = obs
        return tally

    def _decide_undecided(self, visit: Visit) -> Decision:
        near = visit.max_similarity
        leader = visit.leader
        if leader is not None:
            # Matched, but not on enough frames to be announced: a name
            # seen once is recorded, leaning to that person, not rung for.
            visit.decided = "uncertain"
            visit.extra["leaning"] = leader
        elif self.unknown_score > 0 and near is not None and near >= self.unknown_score:
            visit.decided = "uncertain"
        else:
            visit.decided = "unknown"
        logger.debug("visit %s decided %s after %d read(s), nearest %s",
                     visit.key, visit.decided, visit.reads,
                     f"{near:.2f}" if near is not None else "n/a")
        return Decision(visit.decided, visit, visit.best)
