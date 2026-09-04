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
LPR enrichment for the event store (RFC-0001 C1, PR-C).

A vehicle visit's best frame is, by construction, the largest/sharpest look
Tier-0 had at the vehicle — the ideal OCR input. When such a visit is
ingested, this module runs fast-plate-ocr over the evidence crop ONCE (in a
FastAPI background task — never on the ingest request path) and writes
``plate_text`` onto the same row. "Which car entered at 3pm?" then answers
with a registration number, from one OCR call per visit instead of per
frame.

Best-effort by design: adapter missing/unreachable, OCR rejects the read,
or no plate visible → the row simply keeps ``plate_text = NULL``. History
must never depend on an optional adapter.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("plate_enrichment")

# Burst guard: a convoy of vehicles finishing tracks together must not fan
# out into unbounded concurrent OCR calls against the adapter. Two in
# flight, the rest queue on the semaphore — enrichment is background work
# with no latency SLA, so waiting is free.
import asyncio as _asyncio

_OCR_CONCURRENCY = _asyncio.Semaphore(2)

# COCO vehicle classes worth an OCR attempt.
VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}

PLATE_MODEL = "fast_plate_ocr"
PLATE_TASK = "license_plate_recognition"

# Issue #371: a missing OCR adapter used to be a per-event DEBUG line —
# operator-invisible, so "restart unregistered the adapter" looked
# exactly like "no plates in frame" for hours. Best-effort stays
# best-effort (rows keep plate_text=NULL, ingest never blocks), but a
# broken dependency now WARNs, rate-limited so a busy gate camera can't
# flood the log with one line per vehicle.
_MISSING_ADAPTER_WARN_INTERVAL_SECONDS = 600.0
# None = never warned. NOT 0.0: time.monotonic() counts from HOST BOOT,
# so on a machine up for less than the interval, ``now - 0.0 < 600``
# would swallow the very first warning — the one that matters most.
# (Caught by CI: fresh runner VMs boot seconds before pytest runs.)
_last_missing_adapter_warn: float | None = None


def _warn_adapter_missing(status_code: int) -> None:
    """Rate-limited operator signal that plate OCR is failing."""
    global _last_missing_adapter_warn
    import time as _time

    now = _time.monotonic()
    if (_last_missing_adapter_warn is not None
            and now - _last_missing_adapter_warn
            < _MISSING_ADAPTER_WARN_INTERVAL_SECONDS):
        return
    _last_missing_adapter_warn = now
    logger.warning(
        "plate enrichment: KAI-C answered %s for adapter '%s' — plate "
        "reads are FAILING. The adapter is not registered (or not "
        "approved) with KAI-C; check the AI Models page or "
        "GET /api/v1/adapters. This warning repeats at most every %d s.",
        status_code, PLATE_MODEL,
        int(_MISSING_ADAPTER_WARN_INTERVAL_SECONDS),
    )


#: A plate box within this many pixels of the crop boundary is treated as
#: CLIPPED. Deliberately tiny: only boxes literally abutting the edge are
#: rejected, so a plate that merely sits near the edge still reads. Measured
#: on the fragments that motivated this: every one had a margin of 0-1 px,
#: while whole-plate reads sat hundreds of pixels inside.
_PLATE_EDGE_TOLERANCE_PX = 2

#: JPEG start-of-image magic (0xFFD8).
_SOI = bytes((0xFF, 0xD8))


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker — no pixel decode.

    The clipping test below needs the crop's size, and the adapter reports
    the plate box in that crop's pixel coordinates but not the size itself.
    Parsing the header beats decoding the image on what is a per-visit path.
    Returns None for anything that isn't a JPEG we can read, which the
    caller treats as "cannot judge" (never as "clipped").
    """
    if not isinstance(data, (bytes, bytearray)) or data[:2] != _SOI:
        return None
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2                       # standalone markers carry no length
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2:
            return None
        # SOF0-SOF15 carry the frame size; C4/C8/CC are DHT/JPG/DAC.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w > 0 and h > 0 else None
        i += 2 + seg
    return None


def plate_box_is_clipped(
    box, image_size, *, tolerance: int = _PLATE_EDGE_TOLERANCE_PX
) -> bool:
    """Does the detected plate touch the edge of the crop it was found in?

    A vehicle crop is the tracked box plus a margin, clamped to the frame,
    so a vehicle leaving frame yields a crop whose edge cuts through the
    plate. The OCR then reads the CHARACTERS THAT SURVIVED and reports high
    confidence for them — "K884" out of "K884RS" scored 0.98. Confidence
    therefore cannot separate a partial read from a whole one; the geometry
    can. Unknown/misshapen input answers False: never invent a rejection.
    """
    if not image_size or box is None:
        return False
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
        w, h = (float(v) for v in image_size)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    return (
        x1 <= tolerance or y1 <= tolerance
        or x2 >= w - tolerance or y2 >= h - tolerance
    )


#: Default floor for the adapter's plate LOCALISATION confidence.
#: Measured on the install that reported #386: 22 genuine reads scored
#: 0.853-0.936, while the badge false-positive scored 0.3756. Anything
#: in that gap separates them; 0.6 sits in the middle of it, closer to
#: the false positive than to the weakest true read.
_DETECTION_FLOOR_DEFAULT = 0.6


def plate_detection_floor() -> float:
    """Minimum confidence for the plate localisation behind a read.
    0 disables the gate entirely.

    Read from the environment on every call, like ``dedup_window_s``:
    consulted once per OCR attempt, and reading live keeps tests and
    operators out of import-order traps. An install whose camera angle
    yields habitually weak-but-correct localisations can lower or
    disable it without a rebuild.
    """
    import os

    raw = os.environ.get("OPENNVR_PLATE_MIN_DETECTION_CONFIDENCE", "")
    try:
        value = float(raw)
    except ValueError:
        return _DETECTION_FLOOR_DEFAULT
    return value if value > 0 else 0.0


def detection_confidence_is_weak(
    confidence: object, *, floor: float | None = None
) -> bool:
    """Is this plate localisation too weak to believe? Scalar half of
    the guard, so the bus consumer can apply it to the forwarded number
    without inventing a detection dict.

    Unknown input answers False — the same "never invent a rejection"
    rule the clip guard follows. A missing confidence means the adapter
    did not say, not that it said something bad.
    """
    bar = plate_detection_floor() if floor is None else floor
    if bar <= 0:
        return False
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return False
    return float(confidence) < bar


def plate_detection_is_weak(
    detection: object, *, floor: float | None = None
) -> bool:
    """Did the adapter barely find the plate whose characters it just
    reported? Then this is not a plate (#386).

    A two-stage chain localises before it reads, and the reader is
    happy to spell out whatever it is handed: the Audi four-ring badge
    came back as "C00D" at 0.51 against a 0.45 floor, from a
    localisation the detector itself scored 0.3756. Read confidence
    cannot catch that — the characters really are those shapes — and
    neither can the #378 geometry guard, because a badge sits in the
    middle of the crop, nowhere near an edge. The detector's own doubt
    is the signal, and it was being dropped on the floor.

    Aspect ratio is NOT used, though it looks tempting: on the
    reporting install the badge measured 2.95:1 and genuine plates
    2.87-3.85:1, so it separates nothing.

    ``attempted is False`` (an OCR-only adapter that never localises)
    answers False: there is no opinion to judge, and gating on a field
    such an adapter never sends would silently stop plates.
    """
    if not isinstance(detection, dict):
        return False
    if detection.get("attempted") is False:
        return False
    return detection_confidence_is_weak(detection.get("confidence"),
                                        floor=floor)


def require_plate_localisation() -> bool:
    """Must a read come from a plate the adapter actually LOCALISED?

    Every image core sends the OCR adapter is a VEHICLE crop (Tier-0
    tracks cars, not plates). When the localiser looks and finds no
    plate, the adapter falls back to OCR-ing the whole car — and a
    car's grille, badge and bumper text OCR into plausible characters
    at plausible confidence. On the reporting install that is exactly
    where the garbage reads came from: a row captioned with a number
    and no plate anywhere in the picture. Default ON; an OCR-only
    adapter (``attempted`` false) is never affected — there is no
    opinion to require. ``OPENNVR_PLATE_REQUIRE_LOCALISATION=0`` turns
    it off for installs that feed tight plate crops.
    """
    import os

    raw = os.environ.get("OPENNVR_PLATE_REQUIRE_LOCALISATION", "").strip()
    return raw.lower() not in ("0", "false", "no", "off")


def plate_detection_is_missing(detection: object) -> bool:
    """Did the localiser look and find NOTHING? Then whatever the OCR
    read, it read off a car, not a plate. Absent/unknown detection
    answers False (no opinion, never an invented rejection)."""
    if not require_plate_localisation():
        return False
    if not isinstance(detection, dict):
        return False
    if detection.get("attempted") is not True:
        return False
    return detection.get("found") is not True


def plate_box_of(detection: object) -> tuple[float, float, float, float] | None:
    """The adapter's plate box as a well-formed tuple, or None.

    One parser for both consumers of the box: the clip guard above, and
    the evidence crop below (#385). Degenerate boxes (non-numeric,
    inverted, zero-area) answer None so neither caller has to re-check.
    """
    if not isinstance(detection, dict):
        return None
    box = detection.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def extract_plate(
    response: dict | None, *, image_size: tuple[int, int] | None = None
) -> str | None:
    """Pure parser for the adapter's §5 InferResponse → normalized plate.

    Honours the adapter's own ``accepted`` verdict when present (its
    confidence threshold, not ours), requires non-empty ``plate_text``, and
    normalizes to uppercase-no-spaces capped at the column width.

    With ``image_size`` (the OCR'd crop's dimensions) a read whose plate box
    abuts the crop boundary is rejected as a PARTIAL read. Those are worse
    than a miss: a truncated plate is written to the register as though it
    were a whole one, so one vehicle acquires several identities
    ("66HH07" also arriving as "66HH", "H07", "HHO7") and watchlist
    matching silently breaks. Without it the check is skipped.
    """
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("accepted") is False:
        return None
    detection = result.get("plate_detection")
    if isinstance(detection, dict) and plate_box_is_clipped(
        detection.get("box"), image_size
    ):
        return None
    if plate_detection_is_weak(detection):
        return None
    if plate_detection_is_missing(detection):
        return None
    text = result.get("plate_text")
    if not isinstance(text, str):
        return None
    text = "".join(text.split()).upper()
    return text[:32] or None


def extract_read(
    response: dict | None, *, image_size: tuple[int, int] | None = None
) -> dict | None:
    """Full-fidelity parse of the adapter's InferResponse — plate,
    overall confidence, per-character confidences, the accepted verdict
    and the floor it was judged against. Returns None only when there
    is no read at all (no/empty plate_text). Multi-frame OCR needs the
    REJECTED reads too: two near-miss reads of the same plate can merge
    into an accepted one (see ``merge_reads``).

    ``image_size`` is the OCR'd crop's own dimensions (issue #378): a
    read whose plate box abuts that crop's boundary is a PARTIAL read —
    the surviving characters are crisp, so neither confidence nor the
    accepted flag can catch it; only the geometry can. Clipped reads
    return None outright: a fragment must not be merged either (its
    characters are real, but of the wrong plate positions — character
    consensus with a fragment corrupts, not reconstructs). NOTE the box
    is in the coordinates of the crop THIS call OCR'd — candidate crops
    and early-attempt crops have their own sizes, so callers must pass
    the size of the exact bytes they sent, never the visit's evidence
    frame."""
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    detection = result.get("plate_detection")
    if isinstance(detection, dict) and plate_box_is_clipped(
        detection.get("box"), image_size
    ):
        return None
    # A weak localisation is not a near miss to be merged, it is a
    # different object (#386) — drop it outright, like a fragment.
    if plate_detection_is_weak(detection):
        return None
    # No plate found at all → the characters came off the car body.
    # Not a near miss either; nothing here is worth merging.
    if plate_detection_is_missing(detection):
        return None
    text = result.get("plate_text")
    if not isinstance(text, str):
        return None
    text = "".join(text.split()).upper()[:32]
    if not text:
        return None
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.0
    floor = result.get("min_confidence_applied")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        floor = None
    chars = []
    for i, entry in enumerate(result.get("characters") or []):
        if isinstance(entry, dict) and i < len(text):
            c = entry.get("confidence")
            chars.append(float(c) if isinstance(c, (int, float))
                         and not isinstance(c, bool) else 0.0)
    return {
        "plate": text,
        "confidence": float(confidence),
        "characters": chars if len(chars) == len(text) else None,
        "accepted": result.get("accepted") is not False,
        "floor": float(floor) if floor is not None else None,
        # The plate's own rectangle inside the crop we OCR'd (#385) —
        # what the stored evidence is cropped to. In THIS attempt's
        # pixel space, like the clip-guard box above, so it travels with
        # the read and is never applied to another frame.
        "box": plate_box_of(detection),
    }


def merge_reads(a: dict | None, b: dict | None) -> dict | None:
    """Character-level consensus between two imperfect reads.

    Two attempts that read ``H644LX`` and ``H644LK`` disagree in one
    position; taking each position's higher-confidence character often
    reconstructs the true plate from two rejects. Conservative on
    purpose: only same-length reads with per-character confidences
    merge, the merged confidence is the MIN of the chosen characters
    (same aggregation as the adapter), and the result counts as
    accepted only if it clears the STRICTER of the two floors — a
    merge must never be a way to sneak under the bar."""
    if not a or not b:
        return None
    if a["plate"] == b["plate"]:
        return None                      # agreement isn't a merge
    if len(a["plate"]) != len(b["plate"]):
        return None
    if not a.get("characters") or not b.get("characters"):
        return None
    plate = []
    confs = []
    for ca, pa, cb, pb in zip(a["plate"], a["characters"],
                              b["plate"], b["characters"]):
        if pa >= pb:
            plate.append(ca)
            confs.append(pa)
        else:
            plate.append(cb)
            confs.append(pb)
    merged_conf = min(confs) if confs else 0.0
    floors = [f for f in (a.get("floor"), b.get("floor")) if f is not None]
    floor = max(floors) if floors else None
    return {
        "plate": "".join(plate),
        "confidence": merged_conf,
        "characters": confs,
        "accepted": floor is not None and merged_conf >= floor,
        "floor": floor,
    }


#: Hard cap on OCR attempts per visit at ingest — the compute budget.
MAX_INGEST_ATTEMPTS: int = 4


# ── Consensus: a plate is written when the looks AGREE ──────────────
# One accepted read used to be final — the sweep early-exited on it and
# the early attempt (fired at track-confirm, when the car is smallest
# and farthest) wrote first and won. On a blurry 640x360 clip that put
# R-197-GB into the register as R183JF, L656XH and L605HZ, each at
# "conf=1.00": per-character probabilities saturate on blur, so the
# confidence floor filters nothing. What DOES separate a true read from
# a hallucination is that two different looks at the car read the same
# thing — hallucinations differ from frame to frame, the plate does not.
#
# Policy: OCR every look (early read + candidates, bounded by the same
# budget), cluster the accepted reads by edit distance, and write the
# largest cluster's text only when it holds ≥ MIN_AGREEING reads. A
# visit that only ever had ONE look (a single candidate, or just the
# evidence frame) still writes that read — there is nothing to agree
# with, and a lone honest read beats a NULL — but the row is marked
# ``plate_reads=1`` so the UI can say so.

_MIN_AGREEING_DEFAULT = 2


def min_agreeing_reads() -> int:
    """How many looks must agree before a plate is written. 1 restores
    first-accepted-read-wins. Read live, like ``dedup_window_s``."""
    import os

    raw = os.environ.get("OPENNVR_PLATE_MIN_AGREEING_READS", "")
    try:
        value = int(raw)
    except ValueError:
        return _MIN_AGREEING_DEFAULT
    return max(1, value)


def plate_distance(a: str, b: str, *, cap: int = 2) -> int:
    """Bounded Levenshtein distance between two normalised plates.
    Returns ``cap + 1`` as soon as the distance is known to exceed
    ``cap`` — the plates are short and the callers only ask "within
    N?", so the full matrix is never needed."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(cost)
            best = min(best, cost)
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1] if prev[-1] <= cap else cap + 1


def choose_consensus(votes: list[dict], *, min_agreeing: int,
                     looks: int) -> tuple[dict | None, int]:
    """Pick the plate the looks agree on.

    ``votes`` are accepted reads (``extract_read`` dicts, plus any
    early read reconstructed from the attempt cache) in the order they
    were taken. Reads are clustered greedily by edit distance ≤ 1 —
    ``H644LX`` and ``H644LK`` are one car, not two — and the cluster
    with the most votes wins (ties: the higher best confidence). The
    winner's TEXT is the most-voted exact spelling inside the cluster,
    ties again to confidence; the winner's EVIDENCE (the read dict
    returned) is the most confident vote carrying that spelling, so the
    stored crop is the clearest picture of the number written.

    Returns ``(read, agreeing)``: the read to write and how many looks
    agreed with it, or ``(None, 0)`` when the looks do not agree. A
    visit with fewer looks than ``min_agreeing`` cannot reach consensus
    by construction, so its best single read is returned with
    ``agreeing == 1`` — a lone honest read beats NULL.
    """
    if not votes:
        return None, 0
    clusters: list[list[dict]] = []
    for v in votes:
        for cluster in clusters:
            if plate_distance(cluster[0]["plate"], v["plate"], cap=1) <= 1:
                cluster.append(v)
                break
        else:
            clusters.append([v])

    def _best_conf(reads: list[dict]) -> float:
        return max(float(r.get("confidence") or 0.0) for r in reads)

    clusters.sort(key=lambda c: (len(c), _best_conf(c)), reverse=True)
    winner = clusters[0]
    if len(winner) < min_agreeing and looks >= min_agreeing:
        return None, 0
    spellings: dict[str, list[dict]] = {}
    for v in winner:
        spellings.setdefault(v["plate"], []).append(v)
    text = max(spellings, key=lambda t: (len(spellings[t]),
                                         _best_conf(spellings[t])))
    read = max(spellings[text],
               key=lambda r: float(r.get("confidence") or 0.0))
    return read, len(winner)


# ── Sweeps in flight ────────────────────────────────────────────────
# KAI-C publishes plate.recognized.v1 for EVERY accepted read, including
# the ones this module's own sweep initiates — so the bus consumer used
# to race the sweep for the same row, win (it has no images to store),
# and leave the sweep's crop and frame on the floor: a plate with no
# proof, captioned by a vehicle-best frame that on a merged track shows
# a different car. While a sweep owns a row, the consumer defers to it.
# Not a producer check (consumers must not branch on producer — the
# contract's whole point); a check on core's OWN state.

import threading as _threading

_sweeps_lock = _threading.Lock()
_sweeping: set[int] = set()


def mark_sweep_pending(event_id: int) -> None:
    """Called at ingest, BEFORE the background task is queued, so the
    bus can never get there first."""
    with _sweeps_lock:
        _sweeping.add(int(event_id))


def clear_sweep_pending(event_id: int) -> None:
    with _sweeps_lock:
        _sweeping.discard(int(event_id))


def sweep_is_pending(event_id: int) -> bool:
    with _sweeps_lock:
        return int(event_id) in _sweeping


# ── Plate evidence: the crop the read actually came from (#382) ─────


#: Context kept around the plate box, as a fraction of the box's longer
#: side. Enough to show the plate's border and that it is mounted on a
#: vehicle; not enough to turn the picture back into a car photo.
_PLATE_CROP_PAD_RATIO = 0.08

#: The crop is a few hundred pixels of small text. Encode it well —
#: quality is what makes the number readable to the operator who is
#: double-checking the OCR, and the file is tiny either way.
_PLATE_CROP_JPEG_QUALITY = 92


def crop_to_plate_box(
    jpeg: bytes | None, box: tuple[float, float, float, float] | None,
    *, pad_ratio: float = _PLATE_CROP_PAD_RATIO,
) -> bytes | None:
    """Cut the plate (plus a little context) out of the frame it was
    read from; None when that cannot be done.

    The attempts we OCR are VEHICLE crops — Tier-0 tracks cars, not
    plates — so the frame the read came from is a picture of a car, and
    storing it whole captions a plate number with an image in which the
    plate is a handful of pixels (or, at close range, absent: #386's
    badge false-positive is exactly that failure). The adapter already
    localises the plate; this narrows the stored evidence to what it
    found.

    Decoding costs one JPEG per successful read — a per-visit path, and
    only after OCR has already succeeded. Any failure answers None,
    which the caller turns into "store nothing".
    """
    if not jpeg or box is None:
        return None
    try:
        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box
        pad = max(2.0, pad_ratio * max(x2 - x1, y2 - y1))
        # Clamp to the frame: a plate near the crop edge yields a
        # smaller margin on that side, never an out-of-bounds slice.
        left, top = max(0, int(x1 - pad)), max(0, int(y1 - pad))
        right, bottom = min(w, int(x2 + pad) + 1), min(h, int(y2 + pad) + 1)
        if right - left < 2 or bottom - top < 2:
            return None
        ok, buf = cv2.imencode(
            ".jpg", frame[top:bottom, left:right],
            [int(cv2.IMWRITE_JPEG_QUALITY), _PLATE_CROP_JPEG_QUALITY],
        )
        return bytes(buf.tobytes()) if ok else None
    except Exception:  # noqa: BLE001
        logger.debug("plate evidence: could not crop to the plate box",
                     exc_info=True)
        return None


def store_plate_crop(
    jpeg: bytes | None,
    box: tuple[float, float, float, float] | None = None,
) -> str | None:
    """Persist the plate the read came from; return its relative path.

    Separate from the visit's ``evidence_path``, which is the
    vehicle-best frame — chosen for thumbnail quality and, by
    construction, usually the WRONG frame for the plate (a car is
    biggest when closest, which is when its plate leaves the crop).

    ``box`` is the plate's rectangle in ``jpeg``'s own pixel space, as
    reported by the adapter for THIS attempt. Without a usable one we
    store nothing (#385): the attempt is a vehicle crop, and saving it
    here would make the UI drop its "vehicle frame (no separate plate
    crop stored)" caveat while still showing a car — a caption that
    lies is worse than one that admits what it has.

    Best-effort: any failure answers None and the caller simply keeps
    ``plate_evidence_path`` NULL, falling back to the vehicle frame.
    Evidence storage must never cost us a plate we successfully read.

    SYNC on purpose (a JPEG decode plus a file write), so async callers
    must hand it to a thread — ``await asyncio.to_thread(...)`` — rather
    than blocking the event loop every other request shares.
    """
    crop = crop_to_plate_box(jpeg, box)
    if not crop:
        return None
    try:
        from services.evidence_store import save_evidence_jpeg

        return save_evidence_jpeg(crop)
    except Exception:  # noqa: BLE001
        logger.debug("plate evidence: could not store crop", exc_info=True)
        return None


def store_plate_frame(jpeg: bytes | None) -> str | None:
    """Persist the WHOLE attempt the plate was read from; path or None.

    ``store_plate_crop`` cuts the plate rectangle out of these bytes and
    throws the rest away. The rest is the point: it is a crop of the
    vehicle whose plate this is, at the moment it was read, which is the
    only image on the row guaranteed to show the right car.

    The visit's own ``evidence_path`` cannot make that promise. A track
    can span more than one vehicle (association merges a departing car
    with the arriving one behind it), and the best-thumbnail frame is
    then a DIFFERENT car from the one the plate came off — the row shows
    a black Audi captioned with the number of the car before it.

    Best-effort, like every other evidence write: a failure answers None
    and the row simply falls back.
    """
    if not jpeg:
        return None
    try:
        from services.evidence_store import save_evidence_jpeg

        return save_evidence_jpeg(jpeg)
    except Exception:  # noqa: BLE001
        logger.debug("plate evidence: could not store frame", exc_info=True)
        return None


def store_plate_images(
    jpeg: bytes | None,
    box: tuple[float, float, float, float] | None = None,
) -> tuple[str | None, str | None]:
    """``(plate crop, the frame it was cut from)``, both best-effort.

    One call so the pair costs ONE thread hop: both halves decode or
    write, and both callers want both. SYNC on purpose for the same
    reason ``store_plate_crop`` is — hand it to a thread.
    """
    return store_plate_crop(jpeg, box), store_plate_frame(jpeg)


def stamp_plate_evidence(row, rel_path: str | None, *,
                         frame_path: str | None = None,
                         merged: bool = False,
                         reads: int | None = None,
                         source: str | None = None,
                         confidence: float | None = None) -> None:
    """Record which crop a row's plate was read from, whether the read
    was reconstructed from more than one of them, and how many looks
    agreed on it.

    ``merged`` / ``plate_reads`` / ``plate_source`` ride in the existing
    ``payload`` JSON rather than columns of their own — display caveats
    and a writer-policy hint, not queryable state. The dict is UPDATED,
    never replaced: ``payload`` already carries ``stationary`` for tier0
    visits (see ``timeline_service``), and dropping that to record a
    caption would be a poor trade.

    ``source`` names the writer (``early`` — a single track-confirm
    read; ``sweep`` — the ingest sweep; ``bus`` — a forwarded event).
    The sweep uses it to decide what it may overwrite: a consensus of
    several looks outranks any single read, and nothing outranks an
    earlier consensus.
    """
    if rel_path:
        row.plate_evidence_path = rel_path
    if frame_path:
        row.plate_frame_path = frame_path
    if merged or reads is not None or source is not None \
            or confidence is not None:
        payload = dict(row.payload or {})
        if merged:
            payload["plate_merged"] = True
        else:
            payload.pop("plate_merged", None)
        if reads is not None:
            payload["plate_reads"] = int(reads)
        if source is not None:
            payload["plate_source"] = source
        if confidence is not None:
            payload["plate_confidence"] = round(float(confidence), 4)
        row.payload = payload


_PLATE_MARKS = ("plate_merged", "plate_reads", "plate_source",
                "plate_confidence")


def clear_plate(row) -> None:
    """Take a plate OFF a row — text, images and the marks above — so
    it is once more an ordinary vehicle visit. Used when a single read
    is retracted (the looks agreed on a different, already-seen car)."""
    row.plate_text = None
    row.plate_evidence_path = None
    row.plate_frame_path = None
    payload = dict(row.payload or {})
    for key in _PLATE_MARKS:
        payload.pop(key, None)
    row.payload = payload


def plate_reads_of(row) -> int:
    """How many looks agreed on the row's current plate. Rows written
    before the field existed count as ONE look (a single read), which
    is what they were."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    reads = payload.get("plate_reads")
    if isinstance(reads, int) and not isinstance(reads, bool) and reads > 0:
        return reads
    return 1


# ── Duplicate-sighting dedup ────────────────────────────────────────
# A broken track fragments one physical pass into several visits — a
# moving camera, an occlusion, a starved re-verify — and multi-frame OCR
# then reads every fragment successfully, so one car becomes N register
# rows. The only cross-track identity a vehicle has IS its plate, and
# the plate is only known AFTER the first OCR call, so that first call
# per fragment is unavoidable. Everything past it is not: once a read
# comes back matching a plate seen on the same camera within the window,
# the sighting is FOLDED — no plate written (the row stays an ordinary
# vehicle visit), no further OCR spent on that visit.
#
# The window is ROLLING: every sighting, written or folded, restarts it.
# A chain of fragments seconds apart therefore collapses into one
# sighting no matter how long the chain, while the same car genuinely
# returning after a quiet gap makes a new row.
#
# In-memory by design: the map is tiny (plates seen in the last window),
# and the one thing a restart costs is that a fragment chain straddling
# it may produce one extra row — acceptable for a best-effort dedup, and
# far simpler than reconciling wall-clock rows with a monotonic window.

_DEDUP_WINDOW_DEFAULT_S = 30.0
#: Edit distance within which two plates seen in one window are the
#: SAME car. A fragmented pass reads the same plate imperfectly each
#: time (R183JF / R187JF / R183JP …); exact-match dedup let every
#: variant through as a new vehicle. 0 = exact match only.
_DEDUP_DISTANCE_DEFAULT = 1

_sightings_lock = _threading.Lock()
_recent_sightings: dict[tuple[int, str], float] = {}
#: Hard bound on the map — beyond it the oldest entries are evicted.
#: Only reachable if a camera reads >_SIGHTINGS_MAX distinct plates
#: inside one window, i.e. never in practice.
_SIGHTINGS_MAX = 4096


def dedup_window_s() -> float:
    """The rolling dedup window (seconds). 0 disables dedup entirely.

    Read from the environment on every call — it is consulted a handful
    of times per vehicle, and reading live keeps tests and operators
    free of import-order traps."""
    import os

    raw = os.environ.get("OPENNVR_PLATE_DEDUP_WINDOW_S", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEDUP_WINDOW_DEFAULT_S
    return value if value > 0 else 0.0


def dedup_distance() -> int:
    """Max edit distance for two sightings to count as one car."""
    import os

    raw = os.environ.get("OPENNVR_PLATE_DEDUP_DISTANCE", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEDUP_DISTANCE_DEFAULT
    return max(0, value)


def _dedup_key(camera_id: int, plate: str) -> tuple[int, str]:
    # Same normalization as the stored column, so "h644 lx" and the
    # written "H644LX" cannot slip past each other.
    return int(camera_id), "".join(plate.split()).upper()[:32]


def note_sighting(camera_id: int, plate: str, now: float | None = None) -> None:
    """Record that ``plate`` was just seen on ``camera_id`` — called on
    every decision, written AND folded, which is what makes the window
    rolling."""
    import time as _time

    ts = _time.monotonic() if now is None else now
    with _sightings_lock:
        _recent_sightings[_dedup_key(camera_id, plate)] = ts
        if len(_recent_sightings) > _SIGHTINGS_MAX:
            for key in sorted(_recent_sightings,
                              key=_recent_sightings.get)[:len(_recent_sightings)
                                                         - _SIGHTINGS_MAX]:
                del _recent_sightings[key]


def is_duplicate_sighting(camera_id: int, plate: str,
                          now: float | None = None) -> bool:
    """Was this plate seen on this camera within the rolling window?

    Pure lookup — recording the new sighting is the caller's job (via
    ``note_sighting``), on whichever branch it takes."""
    window = dedup_window_s()
    if window <= 0:
        return False
    import time as _time

    ts = _time.monotonic() if now is None else now
    cam, text = _dedup_key(camera_id, plate)
    distance = dedup_distance()
    with _sightings_lock:
        last = _recent_sightings.get((cam, text))
        if last is None and distance > 0:
            # Near miss of a plate seen moments ago on the same camera:
            # the same car, read slightly differently. Small map, short
            # plates — the scan is cheap.
            for (other_cam, other), seen in _recent_sightings.items():
                if other_cam != cam or other == text:
                    continue
                if 0 <= ts - seen <= window and plate_distance(
                        text, other, cap=distance) <= distance:
                    last = seen
                    break
    return last is not None and 0 <= ts - last <= window


async def _ocr_jpeg(jpeg: bytes, camera_handle: str,
                    event_id: int | None = None) -> dict | None:
    """One OCR attempt through KAI-C. Returns ``extract_read``'s dict,
    or None on transport failure / non-200 / empty read. Factored out
    of ``enrich_event_plate`` so the early-attempt endpoint and the
    multi-candidate sweep share one client path (same auth, same
    camera-handle convention, same missing-adapter warning)."""
    from core.config import settings
    from services.adapter_contract import build_infer_payload

    import httpx

    params: dict = {"camera_id": camera_handle}
    if event_id is not None:
        params["event_id"] = int(event_id)
    payload = build_infer_payload(task=PLATE_TASK, jpeg_bytes=jpeg,
                                  params=params)
    try:
        async with _OCR_CONCURRENCY:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.post(
                    f"{settings.kai_c_url}/api/v1/infer/{PLATE_MODEL}",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Internal-Api-Key": settings.internal_api_key or "",
                    },
                )
    except httpx.HTTPError as e:
        logger.debug("plate OCR: KAI-C unreachable (%s): %s", camera_handle, e)
        return None
    if resp.status_code != 200:
        logger.debug("plate OCR: adapter answered %s (%s)",
                     resp.status_code, camera_handle)
        if resp.status_code in (403, 404):
            _warn_adapter_missing(resp.status_code)
        return None
    try:
        # image_size = THIS crop's dimensions — the clip guard (#378)
        # must measure the plate box in the pixel space it was reported
        # in, which is whatever bytes we just sent.
        return extract_read(resp.json(), image_size=jpeg_dimensions(jpeg))
    except ValueError:
        return None


def _row_already_reads(event_id: int, plate: str) -> bool:
    """Does the row ALREADY carry this plate? A short read on its own
    session — the sweep holds none between phases."""
    from core.database import SessionLocal
    from models import TimelineEvent

    db = SessionLocal()
    try:
        row = db.query(TimelineEvent).filter(
            TimelineEvent.id == event_id).first()
        return row is not None and row.plate_text == plate
    finally:
        db.close()


async def enrich_event_plate(
    event_id: int, candidate_jpegs: list[bytes] | None = None,
) -> None:
    """Background task: read the visit's plate, multi-frame style.

    Multi-frame OCR: sweep the visit's plate CANDIDATES (best first,
    shipped by Tier-0 alongside the visit) instead of betting everything
    on the single vehicle-best frame. Every look is OCR'd (bounded by
    MAX_INGEST_ATTEMPTS); near-miss rejects are character-merged
    pairwise (see ``merge_reads``) so two imperfect looks can still
    produce one read; and the plate is written only when the looks
    AGREE (see ``choose_consensus``). An early read parked by the
    track-confirm attempt counts as one look — so early + one agreeing
    candidate is a consensus, and the sweep stops the moment it has
    enough agreeing votes (the budget is still saved on clean reads).
    The evidence crop remains the fallback attempt when no candidates
    rode the visit — exactly the old behaviour.

    Runs in three phases — READ, then OCR with NO session held, then
    REOPEN and write. The middle phase can take a minute (up to
    MAX_INGEST_ATTEMPTS attempts, each waiting on _OCR_CONCURRENCY and
    then on a 15s HTTP timeout), and holding a connection through it is
    what exhausted core's pool: at roughly one vehicle per second, tasks
    merely QUEUED on the semaphore pinned every connection there was,
    and plate reads stopped while events kept flowing.

    The cost of letting go is a race — see phase 3, which re-reads the
    row. Precedence there is by EVIDENCE, not by arrival: a consensus of
    several looks may replace any single read (early attempt or bus
    event), the same plate written meanwhile gets the crop and frame it
    was missing, and an earlier consensus is never overwritten. Every
    failure path is a debug/warning log and a clean return; an
    exception must never escape the task runner.
    """
    from core.database import SessionLocal
    from models import TimelineEvent
    from services.evidence_store import resolve_evidence

    try:
        # ── phase 1: read what the sweep needs, then LET GO ──────────
        # Everything the OCR loop uses is copied out as a plain scalar:
        # past the close the row is DETACHED, so touching it again would
        # re-query rather than quietly read stale pixels.
        db = SessionLocal()
        try:
            row = db.query(TimelineEvent).filter(
                TimelineEvent.id == event_id).first()
            if row is None or (row.label or "") not in VEHICLE_LABELS:
                return
            camera_id = int(row.camera_id)
            evidence_path = row.evidence_path
            prior_plate = row.plate_text
            prior_reads = plate_reads_of(row) if prior_plate else 0
            prior_payload = dict(row.payload or {}) if prior_plate else {}
        finally:
            db.close()

        min_agree = min_agreeing_reads()
        if prior_plate and (prior_reads >= max(min_agree, 2)
                            or min_agree <= 1):
            # A consensus is already on the row (or the policy is
            # first-read-wins, in which case any read is final). Zero
            # OCR spent on a done row.
            return

        # Attempt list: candidates best-first; the evidence crop as the
        # sole attempt when none were shipped (pre-multi-frame producers,
        # non-LPR cameras).
        attempts: list[bytes] = list(candidate_jpegs or [])[:MAX_INGEST_ATTEMPTS]
        if not attempts:
            if not evidence_path:
                return
            path = resolve_evidence(evidence_path)
            if path is None:
                return
            # Off the loop: a full-frame read is blocking file I/O on the
            # same loop every other request is served from.
            attempts = [await _asyncio.to_thread(path.read_bytes)]

        # The early read (a single track-confirm look, already on the
        # row) is one vote. It brings its own stored images, so a
        # consensus that lands on it re-uses them rather than re-storing.
        votes: list[dict] = []
        looks = len(attempts)
        early_vote: dict | None = None
        if prior_plate and prior_payload.get("plate_source") == "early":
            early_vote = {
                "plate": prior_plate, "accepted": True,
                "confidence": float(prior_payload.get("plate_confidence")
                                    or 0.0),
                "characters": None, "floor": None, "box": None,
                "_early": True,
            }
            votes.append(early_vote)
            looks += 1

        # camera_id is the platform HANDLE ("cam{N}") — see _ocr_jpeg's
        # callers; "3" != "cam3" would silently drop consumer-side scoping.
        camera_handle = f"cam{camera_id}"
        # Each read is kept WITH the crop it came from (#382): the
        # winning crop is the only image that actually shows this plate,
        # and it is discarded the moment this function returns unless we
        # persist it here.
        rejects: list[tuple[dict, bytes]] = []
        jpeg_of: dict[int, bytes] = {}
        merged_ids: set[int] = set()
        for jpeg in attempts:
            read = await _ocr_jpeg(jpeg, camera_handle, event_id=event_id)
            if read is None:
                continue
            if read["accepted"]:
                votes.append(read)
                jpeg_of[id(read)] = jpeg
            else:
                # Character-consensus with every earlier reject: two near
                # misses of the same plate often reconstruct the truth.
                # A merged plate appears WHOLE in neither crop. Keep the
                # more confident of the two contributors — the closest
                # thing to a photo of this read — and label the row so
                # the UI never passes it off as a clean single-frame
                # read. The box travels with the crop it was measured
                # in: the contributors are different frames, so the
                # loser's box would cut the wrong rectangle out of the
                # winner's pixels (#385).
                for prev, prev_jpeg in rejects:
                    merged = merge_reads(prev, read)
                    if merged is not None and merged["accepted"]:
                        keep_prev = prev["confidence"] >= read["confidence"]
                        merged["box"] = (prev if keep_prev else read).get("box")
                        votes.append(merged)
                        jpeg_of[id(merged)] = prev_jpeg if keep_prev else jpeg
                        merged_ids.add(id(merged))
                        break
                rejects.append((read, jpeg))
            # Enough agreeing looks already? Then the rest of the budget
            # is pure waste — stop here.
            if len(votes) >= min_agree:
                agreed, count = choose_consensus(
                    votes, min_agreeing=min_agree, looks=looks)
                if agreed is not None and count >= min_agree:
                    break

        winner, agreeing = choose_consensus(
            votes, min_agreeing=min_agree, looks=looks)
        if winner is None:
            if votes:
                logger.info(
                    "plate enrichment: event %s — %d look(s) read %s, no "
                    "%d agree; nothing written",
                    event_id, len(votes),
                    "/".join(v["plate"] for v in votes), min_agree,
                )
            return                       # honest non-read beats a guess
        plate = winner["plate"][:32]
        was_merged = id(winner) in merged_ids
        if prior_plate and plate == prior_plate:
            # The early read held up; the row just learns how many
            # looks agree with it (and keeps the images it already has).
            db = SessionLocal()
            try:
                row = db.query(TimelineEvent).filter(
                    TimelineEvent.id == event_id).first()
                if row is not None and row.plate_text == plate:
                    stamp_plate_evidence(row, None, reads=agreeing,
                                         source="sweep")
                    db.commit()
            finally:
                db.close()
            logger.info(
                "plate enrichment: event %s confirms %s (%d of %d looks agree)",
                event_id, plate, agreeing, looks,
            )
            return
        if not prior_plate and is_duplicate_sighting(camera_id, plate) \
                and not _row_already_reads(event_id, plate):
            # Track fragmentation: this "new" vehicle is the car we just
            # read. Fold the sighting — the visit row stays (it is a
            # real detection), the plate is not repeated, and the sweep
            # stops HERE: identity is established, so any remaining
            # candidates would be pure waste. (Unless the sighting that
            # armed the window is THIS row's — a forwarded event wrote
            # the same plate here while we were in OCR; then the row is
            # not a fragment, it is missing its evidence, and phase 3
            # attaches it.)
            note_sighting(camera_id, plate)
            logger.info(
                "plate dedup: event %s reads %s — seen on cam %s within "
                "%.0fs, sighting folded (no plate written)",
                event_id, plate, camera_id, dedup_window_s(),
            )
            return
        # Off the event loop: the crop decodes a full frame through cv2
        # (~15ms on a 1080x720 attempt, measured) and then writes a file.
        # Small per read, but this task runs once per vehicle on a busy
        # camera, and everything else core serves shares this loop.
        # Deliberately BEFORE the reopen, so no session is held across it.
        # (The early vote can only win with its own plate, which the
        # confirm branch above already handled — so the winner here
        # always has bytes of its own.)
        crop_rel, frame_rel = await _asyncio.to_thread(
            store_plate_images, jpeg_of.get(id(winner)), winner.get("box"))

        # ── phase 3: reopen and write ───────────────────────────────
        # The sweep was away from the DB for as long as the OCR took, so
        # nothing learned in phase 1 can be trusted. Re-read.
        db = SessionLocal()
        try:
            row = db.query(TimelineEvent).filter(
                TimelineEvent.id == event_id).first()
            if row is None:
                return                   # retention swept it mid-sweep
            if row.plate_text:
                current_reads = plate_reads_of(row)
                if row.plate_text == plate:
                    # The bus consumer (or the ingest claim) wrote the
                    # SAME plate while we were in OCR — from the very
                    # bytes we just stored. Give the row the proof it is
                    # missing; never leave a plate without its picture.
                    if not row.plate_frame_path or not row.plate_evidence_path:
                        stamp_plate_evidence(row, crop_rel, frame_path=frame_rel,
                                             merged=was_merged,
                                             reads=max(agreeing, current_reads),
                                             source="sweep")
                    else:
                        stamp_plate_evidence(row, None, reads=max(
                            agreeing, current_reads), source="sweep")
                    db.commit()
                    logger.info(
                        "plate enrichment: event %s already read as %s — "
                        "evidence attached (%d looks agree)",
                        event_id, plate, max(agreeing, current_reads),
                    )
                    return
                if current_reads >= agreeing or agreeing < max(min_agree, 2):
                    # Their read has at least as many looks behind it as
                    # ours (or ours is a lone read): first writer wins.
                    logger.info(
                        "plate enrichment: event %s already read as %s "
                        "(%d looks) while OCR was in flight — sweep result "
                        "%s (%d looks) dropped",
                        event_id, row.plate_text, current_reads, plate, agreeing,
                    )
                    return
                # A consensus outranks a single read: the early attempt
                # (or a lone bus event) put a hallucination on the row
                # and several later looks disagree with it. Replace it,
                # images and all — unless the plate the looks agree on
                # is the car we read moments ago (a fragment), in which
                # case the honest row is a visit with NO plate.
                if is_duplicate_sighting(camera_id, plate):
                    note_sighting(camera_id, plate)
                    logger.info(
                        "plate enrichment: event %s single read %s "
                        "retracted — looks agree on %s, seen on cam %s "
                        "within %.0fs (sighting folded)",
                        event_id, row.plate_text, plate, camera_id,
                        dedup_window_s(),
                    )
                    clear_plate(row)
                    db.commit()
                    return
                logger.info(
                    "plate enrichment: event %s re-read %s -> %s "
                    "(%d looks agree; previous was a single read)",
                    event_id, row.plate_text, plate, agreeing,
                )
                row.plate_evidence_path = None
                row.plate_frame_path = None
            elif is_duplicate_sighting(camera_id, plate):
                # Re-checked here too: another fragment of the same pass
                # can have been written during the OCR window.
                note_sighting(camera_id, plate)
                return
            row.plate_text = plate
            stamp_plate_evidence(row, crop_rel, frame_path=frame_rel,
                                 merged=was_merged, reads=agreeing,
                                 source="sweep")
            note_sighting(camera_id, plate)
            db.commit()
        finally:
            db.close()
        logger.info(
            "plate enrichment: event %s -> %s (conf=%.2f, looks=%d, "
            "agree=%d%s)",
            event_id, plate, winner["confidence"], looks, agreeing,
            ", merged" if was_merged else "",
        )
    except Exception:
        logger.warning("plate enrichment failed for event %s", event_id,
                       exc_info=True)
    finally:
        clear_sweep_pending(event_id)


def wants_plate(label: str | None, evidence_path: str | None,
                enabled: bool = True) -> bool:
    """Should this freshly-ingested visit be queued for OCR? Pure, tested."""
    return bool(enabled and evidence_path and (label or "") in VEHICLE_LABELS)
