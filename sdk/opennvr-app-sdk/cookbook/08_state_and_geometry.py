# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`keyed_state`, `Zone` and `Tripwire` — the two things every rule needs.

Demonstrates: `keyed_state`, `KeyedState`, `StateRecord`, `.touch`,
`.gc`, `.alerted`, `.data`, `Point`, `Zone`, `Zone.from_config`,
`Zone.contains`, `Tripwire`, `Tripwire.crossing`, `bbox_center`.

Almost every rule reduces to "has this object been somewhere, for long
enough?". `keyed_state` answers the *how long*, geometry answers the
*where*, and between them they replace the state machine apps used to
hand-roll. (The `App` facade wraps both behind `zone=` and `dwell=`;
this is what it wraps.)
"""
from opennvr_app_sdk import Point, Tripwire, Zone, bbox_center, keyed_state

# ── Zones: is the object inside the area the operator drew? ─────────
#
# The catalog's zone editor emits NORMALIZED vertices (0–1 of the
# frame), so a zone survives a resolution change. Work in that space
# and pass frame dims of 1 to bbox_center.

DRIVEWAY = Zone.from_config("driveway", [[0.1, 0.4], [0.9, 0.4],
                                         [0.9, 0.95], [0.1, 0.95]])


def in_driveway(detection: dict) -> bool:
    """A detection's bbox centre against a polygon."""
    centre = bbox_center(detection.get("bbox", {}), 1, 1)
    return DRIVEWAY.contains(centre)


# ── Tripwires: did the object cross a line, and which way? ──────────

ENTRANCE = Tripwire.from_config("entrance", [0.0, 0.5], [1.0, 0.5])


def crossed(previous: Point, current: Point) -> str | None:
    """`"a_to_b"`, `"b_to_a"`, or None. Direction is what separates
    "12 people entered" from "12 people milled about the door"."""
    return ENTRANCE.crossing(previous, current)


# ── Keyed TTL state: how long has this been true? ───────────────────


class Dwell:
    """The loitering pattern in nine lines.

    TTL is in seconds of EVENT time — whatever timeline you pass to
    `touch(at=...)`. A key not touched within the TTL is garbage-
    collected, which is what ends a presence episode and re-arms the
    latch.
    """

    def __init__(self, threshold_s: float = 30.0) -> None:
        self.threshold = threshold_s
        # ttl comfortably longer than the gap between events, shorter
        # than "the object really left".
        self.present = keyed_state(ttl=10.0)

    def saw(self, camera_id: str, track_id: str, at: float) -> float | None:
        """Returns the dwell time the first moment it crosses the
        threshold, then None until the object leaves and returns."""
        record = self.present.touch((camera_id, track_id), at=at)
        if record.age < self.threshold or record.alerted:
            return None
        record.alerted = True                 # the latch: once per episode
        return record.age

    def note(self, camera_id: str, track_id: str, **values) -> None:
        """`record.data` is a free-form scratchpad per key — a phase, a
        counter, the last zone the object was in."""
        self.present[(camera_id, track_id)].data.update(values)

    @property
    def tracked(self) -> int:
        return len(self.present)
