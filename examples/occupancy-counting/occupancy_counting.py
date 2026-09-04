# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Occupancy-counting example app — on the ``opennvr-app-sdk``
(App SDK spec §08 step 5, swept after the loitering reference
migration).

Counts how many watched-label entities (people, vehicles, …) are
inside each operator-defined zone on every inference frame, and fires
an alert when a zone crosses an occupancy threshold — too many
(over-occupancy: a crowded exit, an over-capacity room) or, optionally,
too few (under-occupancy: a post that should always be staffed).

What lives where after the migration
------------------------------------

The SDK's :class:`~opennvr_app_sdk.Detector` base owns the NATS
subscribe loop, per-message JSON decoding + exception isolation, the
``camera_id`` / ``result.detections`` payload walk, alert dispatch,
the CLI, signal handling, and the §03 contract endpoints. The §11.5
alert stack and the zone geometry moved to ``opennvr_app_sdk.alerts``
/ ``opennvr_app_sdk.geometry`` (thin shims remain at ``alerts.py`` /
``zone.py`` for import compatibility).

What's left here is the rule — the edge-triggered occupancy state
machine — plus this app's config parsing and its declarative MANIFEST.

Architecture (unchanged)
------------------------

Like ``loitering-detection`` and unlike ``intrusion-detection``, this
app SUBSCRIBES to KAI-C's NATS inference broadcast surface
(``opennvr.inference.>``) rather than driving its own inference. It
rides whatever detection stream another app (e.g. intrusion-detection)
is already producing, so it pays zero adapter/GPU cost on top — one
inference fans out to N counting consumers.

State machine (per camera × zone)
---------------------------------

Occupancy is a level, not an event, so we alert on *transitions* of an
edge-triggered state machine rather than on every frame:

* ``normal``  → ``over``   when count > ``max_occupancy``   → fire OVER
* ``normal``  → ``under``  when count < ``min_occupancy``   → fire UNDER
* ``over`` / ``under`` → ``normal`` when count returns to the
  acceptable band → fire CLEARED (only if ``clear_alerts: true``)

Edge-triggering is what stops a crowded room from emitting one alert
per inference frame. A short ``debounce_frames`` requirement (default
1) can be raised so a single noisy frame doesn't flip the state.

The state is deliberately a plain ``dict`` rather than the SDK's
``keyed_state``: occupancy is a *level* keyed by a bounded, config-known
camera set — there is no TTL/absence semantics to garbage-collect, and
the debounce latch is a frame counter, not a time latch.

Per-track identity is NOT used: occupancy is a count of in-zone
detections per frame, so a detector that emits ``track_id`` and one
that doesn't both work. Double-counting from duplicate boxes on the
same object is mitigated by the detector's own NMS upstream.

Run::

    python occupancy_counting.py --config config.yml
    python occupancy_counting.py --config config.yml --once
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from opennvr_app_sdk import (
    Alert,
    AlertType,
    AppManifest,
    Detector,
    DomainEventPublisher,
    Param,
    StateView,
    app,
)
from opennvr_app_sdk.cameras import (
    UNIT_FRAME,
    discover_cameras,
    filter_cameras_for_skill,
    full_frame_polygon,
)
from opennvr_app_sdk.config import load_yaml
from opennvr_app_sdk.geometry import (
    Point, Tripwire, Zone, bbox_center, scale_vertices,
)
from opennvr_app_sdk.state import keyed_state

logger = logging.getLogger("occupancy-counting")

# How often an auto-derived camera set is re-checked against OpenNVR.
# Cameras get added and removed while the app runs; a set captured once
# at boot would silently never watch camera 5 added tomorrow.
DISCOVERY_REFRESH_S: int = 300

# The capability name this app answers to in per-camera assignments
# ("camera 2 and 3 do occupancy_counting" on the camera settings page).
# When at least one camera is assigned this skill, auto-derivation scopes
# to exactly those cameras; when none is, nothing is restricted and the
# app watches every camera, exactly as before assignments existed.
SKILL: str = "occupancy_counting"


def _scope_to_assignment(discovered: list[dict]) -> list[dict]:
    """Narrow a discover_cameras() payload to this app's assigned cameras.

    No camera assigned SKILL -> no restriction declared -> unchanged.
    Assignments are additive intent: they point THIS app's attention;
    streaming/recording/Tier-0 on the other cameras are unaffected.
    """
    assigned = filter_cameras_for_skill(discovered, SKILL)
    if assigned is None:
        return discovered
    keep = set(assigned)
    scoped = [c for c in discovered if str(c.get("camera_id")) in keep]
    logger.info(
        "per-camera assignment active: %d of %d camera(s) assigned %r (%s)",
        len(scoped), len(discovered), SKILL,
        ", ".join(sorted(keep)) or "-",
    )
    return scoped


#: The contracted history feed this app publishes (EVENT_CONTRACTS.md):
#: on every committed level transition, and otherwise at most once per
#: this many seconds per camera while the count moves.
OCCUPANCY_SCHEMA = "occupancy.changed.v1"
PUBLISH_MIN_INTERVAL_SECONDS = 10.0

# ── Spatial heatmap (EVENT_CONTRACTS.md occupancy.heatmap.v1) ──────────
# Every detection the app already receives carries a normalised box; the
# box's FOOT point (bottom-centre — where the person stands, not where
# their head is) is binned into a fixed unit-space grid per camera. The
# grid is resolution-independent (the boxes are 0-1), so it lines up
# with any still of the camera. Counts accrue between publishes and are
# shipped as a SPARSE delta every ``heatmap_publish_seconds``; core sums
# deltas into camera-hour rows and serves any window from them. The app
# keeps nothing across restarts — history is core's job, as with the
# head-count samples.
HEATMAP_SCHEMA = "occupancy.heatmap.v1"
HEATMAP_COLS: int = 48
HEATMAP_ROWS: int = 27          # 16:9 cells; a 4:3 camera just gets squarer cells
HEATMAP_PUBLISH_SECONDS_DEFAULT: int = 60

# ── Footfall + dwell (EVENT_CONTRACTS.md occupancy.footfall.v1) ────────
# Tier-0 detections carry a ``track_id``, which turns a per-frame count
# into per-visitor facts: an optional per-camera ENTRY LINE (a tripwire
# drawn in the catalog; a→b is "in") counts entries and exits — far more
# robust for a doorway than in-zone counts — and each track's time
# inside the zone is its DWELL, finalised when it leaves or its track
# expires. Both ride the heatmap's publish cadence as one delta event.
FOOTFALL_SCHEMA = "occupancy.footfall.v1"
#: A track unseen for this long is gone; its dwell is finalised then.
TRACK_TTL_SECONDS_DEFAULT: float = 15.0


MANIFEST = AppManifest(
    id="occupancy-counting",
    name="Occupancy Counting",
    version="1.2.0",
    category="analytics",
    summary="Alerts on zone occupancy threshold crossings (over / under / cleared).",
    requires_tasks=["object_detection"],  # checked vs GET /api/v1/adapters
    # This app powers the first-class Occupancy page.
    provides=["occupancy"],
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"]),
        Param("max_occupancy", int, required=True,
              description="Fire OVER when the in-zone count exceeds this."),
        Param("min_occupancy", int,
              description="Fire UNDER when the in-zone count drops below this."),
        Param("debounce_frames", int, default=1,
              description="Consecutive frames a new band must persist before firing."),
        Param("clear_alerts", bool, default=False,
              description="Also fire a low-severity alert when a zone returns to normal."),
        Param("heatmap_enabled", bool, default=True,
              description="Accumulate WHERE watched entities stand (a per-camera heat grid), shown on the Occupancy page."),
        Param("heatmap_publish_seconds", int, default=HEATMAP_PUBLISH_SECONDS_DEFAULT,
              description="How often the accumulated heat grid (and footfall/dwell) is shipped to the platform's history."),
        Param("max_dwell_seconds", int, default=0,
              description="Alert when one tracked entity has stayed inside the zone longer than this (0 = off)."),
        Param("zones", "geometry.polygon", per_camera=True),  # drawn in the catalog UI
        Param("entry_line", "geometry.tripwire", per_camera=True,
              description="Optional doorway line: crossings a→b count as entries, b→a as exits."),
    ],
    # Store listing (the catalog's Details section).
    description=(
        "Live head-counts for the spaces you care about. Draw a zone "
        "on any camera and the app counts the people (or any watched "
        "label) inside it, riding the detection stream the platform "
        "already produces — zero extra inference cost.\n\n"
        "Set a maximum and it alerts the moment a zone goes over "
        "(and, optionally, when it drops under a minimum or returns "
        "to normal). The first-class Occupancy page shows every zone "
        "live; thresholds apply immediately."
    ),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Over-occupancy alarms for halls, gyms, canteens, waiting areas",
        "Factory floor and warehouse zone limits (safety compliance)",
        "Live 'people now' board across every watched space",
        "Under-occupancy: know when a manned post is left empty",
        "Footfall heatmap: where people actually stand, queue and linger — by hour, day or week",
        "Entries / exits through a doorway line, and how long each visitor dwells in a zone",
    ],
    emits=[
        AlertType("occupancy_over", severity="high"),
        AlertType("occupancy_under", severity="medium"),
        AlertType("occupancy_cleared", severity="low"),
        AlertType("occupancy_dwell", severity="medium"),
    ],
    # Declarative live-state views — the catalog renders GET /state
    # (state_snapshot below) with zero app-specific UI code. The
    # cameras dict renders as a table with the camera id as the
    # leading column.
    state_schema=[
        StateView(
            name="people",
            label="People counted",
            kind="metric",
            path="total_people",
            description="Sum of the last head-count across all watched zones.",
        ),
        StateView(
            name="over",
            label="Zones over limit",
            kind="metric",
            path="zones_over",
            description="How many zones are currently above their max_occupancy.",
        ),
        StateView(
            name="cameras",
            label="Live occupancy",
            kind="table",
            path="cameras",
            columns=["id", "level", "last_count", "pending"],
            description="Current occupancy band + last count per camera.",
        ),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class CameraZone:
    """One camera + one counted zone + its pixel dimensions.

    ``max_occupancy`` / ``min_occupancy`` may be set per-camera to
    override the app-level defaults — a doorway and a stadium concourse
    on the same deployment want very different thresholds.
    """

    camera_id: str
    zone: Zone
    frame_width: int
    frame_height: int
    max_occupancy: int
    min_occupancy: int | None
    # Optional doorway tripwire in the same pixel space as ``zone``;
    # a→b counts as an entry, b→a as an exit.
    entry_line: Tripwire | None = None


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    watch_labels: list[str]
    debounce_frames: int
    clear_alerts: bool
    cameras: dict[str, CameraZone]  # keyed by camera_id for O(1) lookup
    webhook_url: str | None
    heatmap_enabled: bool = True
    heatmap_publish_seconds: int = HEATMAP_PUBLISH_SECONDS_DEFAULT
    max_dwell_seconds: int = 0
    track_ttl_seconds: float = TRACK_TTL_SECONDS_DEFAULT
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = "opennvr.alerts"
    # Consume the always-on Tier-0 detector (docs/tier0-consumption.md).
    # ON by default: Tier-0 ships enabled in the standard stack and is the
    # only detection stream a default install produces, so an app that
    # ignored it would sit silent forever. The SDK's own default is off
    # (contract compatibility); an app that also subscribes to a heavy
    # adapter should narrow ``subject_pattern`` to
    # ``opennvr.inference.tier0.>`` — as the shipped config does — or turn
    # this off, otherwise the same object is counted from both streams.
    consume_tier0: bool = True
    # True when 'cameras' came from OpenNVR rather than the YAML — only
    # then is the set refreshed at runtime (an explicit list is the
    # operator's word and is never second-guessed).
    cameras_auto_derived: bool = False
    # App-level thresholds, kept so a camera discovered AFTER boot gets
    # the same defaults the ones discovered at boot did.
    default_max: int = 0
    default_min: int | None = None
    opennvr_url_for_discovery: str = ""
    # The key used for BOOT discovery, kept so the refresh loop presents
    # the same credentials. Boot honouring a YAML ``internal_api_key``
    # while refresh silently fell back to the env var was a real (if
    # docker-invisible) way to discover cameras once and never again.
    internal_api_key: str | None = None

    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state;
    # ``opennvr_url`` triggers registry self-registration on boot.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def _auto_camera_zone(
    camera_id: str, max_occupancy: int, min_occupancy: int | None
) -> CameraZone:
    """A whole-frame zone for a camera nobody drew a zone for.

    Expressed in the SDK's unit space, which is exactly right at any real
    resolution: Tier-0's pixel boxes are normalised by the bridge and
    scaled back by these same dimensions, so point and polygon always
    share one coordinate system.
    """
    return CameraZone(
        camera_id=camera_id,
        zone=Zone.from_config(name="full-frame (auto)", vertices=full_frame_polygon()),
        frame_width=UNIT_FRAME,
        frame_height=UNIT_FRAME,
        max_occupancy=max_occupancy,
        min_occupancy=min_occupancy,
    )


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig.

    Raises ``ValueError`` on malformed config so the CLI can surface a
    useful operator message and exit non-zero."""
    raw = load_yaml(path)

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")

    if "subject_pattern" in raw:
        subject = str(raw.get("subject_pattern") or "").strip()
        if not subject:
            raise ValueError("config: 'subject_pattern' must not be empty")
    else:
        subject = "opennvr.inference.>"

    try:
        debounce = int(raw.get("debounce_frames", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'debounce_frames' must be an integer") from exc
    if debounce < 1:
        raise ValueError("config: 'debounce_frames' must be >= 1")

    clear_alerts = bool(raw.get("clear_alerts", False))

    # App-level default thresholds; per-camera entries may override.
    default_max = raw.get("max_occupancy")
    default_min = raw.get("min_occupancy")

    watch_labels_raw = raw.get("watch_labels")
    if watch_labels_raw is None:
        watch_labels = ["person"]
    else:
        watch_labels = [str(s).lower() for s in watch_labels_raw]
        if not watch_labels:
            raise ValueError(
                "config: 'watch_labels' must not be empty (omit the key to "
                "use the default ['person'], or list at least one label)"
            )

    cameras_raw = raw.get("cameras") or []
    auto_derived = not cameras_raw
    if not cameras_raw:
        # No cameras listed → ASK OpenNVR which ones exist instead of
        # refusing to boot. Hand-copying camera ids is the most common way
        # this app ends up counting nothing: OpenNVR names them ``cam1``,
        # a hand-written config almost always says ``cam-1``, and the two
        # look identical until you notice nothing has ever fired. Each
        # discovered camera gets a whole-frame zone in the SDK's unit
        # space, which is exactly correct at any real resolution (see
        # opennvr_app_sdk.cameras). Draw a real zone in the catalog UI —
        # or list cameras explicitly here — to override.
        discovered = _scope_to_assignment(discover_cameras(
            str(raw.get("opennvr_url") or ""),
            api_key=raw.get("internal_api_key"),
        ))
        cameras_raw = [
            {
                "camera_id": c["camera_id"],
                "zone_name": "full-frame (auto)",
                "zone": full_frame_polygon(),
                "frame_width": UNIT_FRAME,
                "frame_height": UNIT_FRAME,
            }
            for c in discovered
        ]
        if cameras_raw:
            if default_max is None:
                # Auto-derived cameras have no per-camera value to fall back
                # on, so the app-level default is mandatory. Raise HERE with
                # a message naming the actual situation — letting the
                # per-camera loop below catch it produces "camera entry 0
                # malformed", pointing the operator at a YAML entry that
                # does not exist.
                raise ValueError(
                    "config: 'max_occupancy' is required when cameras are "
                    "discovered from OpenNVR — set it as an app-level "
                    "default (auto-derived cameras have no per-camera value)"
                )
            logger.info(
                "no cameras configured — watching all %d camera(s) OpenNVR "
                "knows about with a whole-frame zone: %s",
                len(cameras_raw), ", ".join(c["camera_id"] for c in cameras_raw),
            )
        elif not str(raw.get("opennvr_url") or "").strip():
            # Nothing listed AND nowhere to ask: a genuine misconfiguration,
            # worth failing loudly at startup.
            raise ValueError(
                "config: no 'cameras' entries and no 'opennvr_url' to discover "
                "them from — set opennvr_url (and OPENNVR_INTERNAL_API_KEY), "
                "or list cameras explicitly"
            )
        elif default_max is None:
            # Booting empty is fine, but a camera the refresh loop adds
            # later still needs a ceiling. Without an app-level default it
            # would silently get 0 and alert on the first person to walk
            # past — while the SAME missing setting is a hard error when a
            # camera exists at boot. Fail the same way in both cases.
            raise ValueError(
                "config: 'max_occupancy' is required when cameras are "
                "discovered from OpenNVR — cameras added later have no "
                "per-camera value to fall back on"
            )
        else:
            # Core is reachable but has no cameras yet — the normal state of a
            # fresh install where the app was enabled before any camera was
            # added. Booting with none and re-checking (see _discovery_loop) is
            # right; exiting here would crash-loop the container forever over
            # something the operator fixes in the UI a minute later.
            logger.warning(
                "no cameras configured and OpenNVR reports none yet — "
                "watching nothing for now; will re-check every %d minutes",
                DISCOVERY_REFRESH_S // 60,
            )
    cameras: dict[str, CameraZone] = {}
    for idx, c in enumerate(cameras_raw):
        try:
            zone = Zone.from_config(
                name=str(c.get("zone_name", f"zone-{idx}")),
                vertices=c["zone"],
            )
            frame_width = int(c.get("frame_width", 1920))
            frame_height = int(c.get("frame_height", 1080))
            if frame_width <= 0 or frame_height <= 0:
                raise ValueError(
                    f"frame_width and frame_height must be > 0; got "
                    f"frame_width={frame_width}, frame_height={frame_height}"
                )
            # Threshold resolution: per-camera value wins, else the
            # app-level default. max_occupancy is mandatory (an
            # occupancy counter with no ceiling never fires OVER);
            # min_occupancy is optional (most deployments only care
            # about over-occupancy).
            raw_max = c.get("max_occupancy", default_max)
            if raw_max is None:
                raise ValueError(
                    f"camera entry {idx}: 'max_occupancy' is required "
                    "(set it per-camera or as an app-level default)"
                )
            max_occ = int(raw_max)
            if max_occ < 0:
                raise ValueError("'max_occupancy' must be >= 0")
            raw_min = c.get("min_occupancy", default_min)
            min_occ = int(raw_min) if raw_min is not None else None
            if min_occ is not None and min_occ < 0:
                raise ValueError("'min_occupancy' must be >= 0")
            if min_occ is not None and min_occ > max_occ:
                raise ValueError(
                    f"'min_occupancy' ({min_occ}) must be <= "
                    f"'max_occupancy' ({max_occ})"
                )
            cam = CameraZone(
                camera_id=str(c["camera_id"]),
                zone=zone,
                frame_width=frame_width,
                frame_height=frame_height,
                max_occupancy=max_occ,
                min_occupancy=min_occ,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"config: camera entry {idx} malformed: {exc}"
            ) from exc
        if cam.camera_id in cameras:
            raise ValueError(
                f"config: duplicate camera_id {cam.camera_id!r} at entry {idx}"
            )
        cameras[cam.camera_id] = cam

    nats_alerts_url = str(raw["nats_alerts_url"]).strip() if raw.get("nats_alerts_url") else None
    nats_alerts_token = str(raw["nats_alerts_token"]) if raw.get("nats_alerts_token") else None
    if "nats_alerts_subject_prefix" in raw:
        nats_prefix = str(raw["nats_alerts_subject_prefix"]).strip()
        if not nats_prefix:
            raise ValueError(
                "config: 'nats_alerts_subject_prefix' must not be empty "
                "(omit the key to use the default 'opennvr.alerts')"
            )
    else:
        nats_prefix = "opennvr.alerts"

    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        watch_labels=watch_labels,
        debounce_frames=debounce,
        clear_alerts=clear_alerts,
        cameras=cameras,
        heatmap_enabled=bool(raw.get("heatmap_enabled", True)),
        heatmap_publish_seconds=max(5, int(
            raw.get("heatmap_publish_seconds", HEATMAP_PUBLISH_SECONDS_DEFAULT)
            or HEATMAP_PUBLISH_SECONDS_DEFAULT)),
        max_dwell_seconds=max(0, int(raw.get("max_dwell_seconds", 0) or 0)),
        track_ttl_seconds=max(1.0, float(
            raw.get("track_ttl_seconds", TRACK_TTL_SECONDS_DEFAULT)
            or TRACK_TTL_SECONDS_DEFAULT)),
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        nats_alerts_url=nats_alerts_url,
        nats_alerts_token=nats_alerts_token,
        nats_alerts_subject_prefix=nats_prefix,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        consume_tier0=bool(raw.get("consume_tier0", True)),
        cameras_auto_derived=auto_derived,
        default_max=int(default_max or 0),
        default_min=int(default_min) if default_min is not None else None,
        opennvr_url_for_discovery=str(raw.get("opennvr_url") or ""),
        internal_api_key=raw.get("internal_api_key") or None,
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── Per-camera occupancy state ─────────────────────────────────────


@dataclass
class _ZoneState:
    """Edge-triggered occupancy state for one camera × zone.

    ``level`` is the current alerting band: ``"normal"`` / ``"over"`` /
    ``"under"``. ``pending`` + ``pending_count`` implement the debounce:
    a candidate new level must persist for ``debounce_frames`` frames
    before it becomes the committed ``level`` and fires an alert.
    """

    level: str = "normal"
    pending: str | None = None
    pending_count: int = 0
    last_count: int = 0


# ── The rule ───────────────────────────────────────────────────────


class OccupancyCounter(Detector):
    """Consumes inference events (via the SDK's Detector loop) and
    counts in-zone entities per camera, firing edge-triggered
    occupancy alerts.

    Stateful — one ``_ZoneState`` per camera_id. State is bounded by
    the configured camera set (events from unknown cameras are dropped
    before any state is created)."""

    manifest = MANIFEST

    def setup(self) -> None:
        self._states: dict[str, _ZoneState] = {}
        # occupancy.changed.v1 sampling state: last published (count, ts)
        # per camera. The publisher never raises on bus trouble.
        self._occupancy_publisher = DomainEventPublisher(
            self._config.nats_url, token=self._config.nats_token,
            producer="app:occupancy-counting")
        self._last_published: dict[str, tuple[int, float]] = {}
        self._history_events_published: int = 0
        # Heat grids: camera_id -> flat list[int] of HEATMAP_COLS*HEATMAP_ROWS
        # cells, counts accrued SINCE THE LAST PUBLISH (a delta, not a
        # total), plus the frames that contributed. Reset on publish.
        self._heat: dict[str, list[int]] = {}
        self._heat_frames: dict[str, int] = {}
        self._heatmaps_published: int = 0
        # Footfall + dwell. Per (camera, track): last foot point, whether
        # it is inside the zone, when it entered. TTL-expired tracks are
        # finalised (their dwell recorded) by the explicit gc each event
        # runs — auto_gc would drop them silently.
        self._tracks = keyed_state(self._config.track_ttl_seconds, auto_gc=False)
        # Deltas since the last publish, per camera.
        self._footfall: dict[str, dict[str, float]] = {}
        # Lifetime tallies for /state (since app start).
        self._footfall_total: dict[str, dict[str, float]] = {}
        self._footfall_published: int = 0
        self._warned_missing_track = False
        # Only auto-derived camera sets are refreshed; an explicit list is
        # the operator's word and is never second-guessed.
        self._auto_cameras: bool = bool(
            getattr(self._config, "cameras_auto_derived", False)
        )
        # Camera ids seen on the bus that this app has no config for —
        # tracked so the "not my camera" warning fires once each, not per
        # frame (Tier-0 publishes continuously).
        self._unknown_cameras: set[str] = set()

    # ── Camera-set refresh ────────────────────────────────────────

    def refresh_cameras(
        self, discovered: list[dict[str, Any]] | None = None
    ) -> tuple[list[str], list[str]]:
        """Re-derive the watched camera set from OpenNVR. Returns
        ``(added, removed)``. No-op (and no network call) when the set was
        pinned in config. Separated from the timer so it is testable
        without a running loop.

        ``discovered`` lets the caller supply an already-fetched camera
        list so the dict mutation runs on the event loop while only the
        blocking HTTP call runs in a worker thread (see
        ``_discovery_loop``) — mutating ``_config.cameras`` / ``_states``
        from another thread could race a concurrent ``/state`` snapshot.
        Discovery presents the same credentials boot did (a YAML
        ``internal_api_key`` wins, else the env var)."""
        if not self._auto_cameras:
            return [], []
        if discovered is None:
            discovered = discover_cameras(
                self._config.opennvr_url_for_discovery,
                api_key=self._config.internal_api_key,
            )
        # Assignment scoping runs on every refresh, so assigning or
        # un-assigning a camera on the settings page takes effect within
        # one refresh interval — no app restart.
        discovered = _scope_to_assignment(discovered)
        ids = {c["camera_id"] for c in discovered}
        if not ids:
            # Treat "core answered with nothing" as no news rather than
            # "delete every camera": a transient blip mid-poll must not
            # silently stop the app watching everything it was watching.
            return [], []
        current = set(self._config.cameras)
        added = sorted(ids - current)
        removed = sorted(current - ids)
        for cam_id in added:
            self._config.cameras[cam_id] = _auto_camera_zone(
                cam_id, self._config.default_max, self._config.default_min
            )
        for cam_id in removed:
            self._config.cameras.pop(cam_id, None)
            self._states.pop(cam_id, None)
            self._unknown_cameras.discard(cam_id)
        if added or removed:
            logger.info("camera set refreshed: +%s -%s (now watching %s)",
                        added or "-", removed or "-",
                        sorted(self._config.cameras) or "(none)")
        return added, removed

    async def _discovery_loop(self, interval_s: int = DISCOVERY_REFRESH_S) -> None:
        """Keep an auto-derived camera set current. Best-effort: a failed
        refresh must never take the counter down."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                # Blocking HTTP in a worker thread; the dict mutation then
                # happens here on the loop, serialized with on_detections
                # and state_snapshot — no cross-thread writes.
                discovered = await asyncio.to_thread(
                    discover_cameras,
                    self._config.opennvr_url_for_discovery,
                    api_key=self._config.internal_api_key,
                )
                self.refresh_cameras(discovered=discovered)
            except Exception:
                logger.warning("camera refresh failed", exc_info=True)

    async def run(self, *, once: bool = False) -> None:
        refresher: asyncio.Task | None = None
        flusher: asyncio.Task | None = None
        if not once and self._auto_cameras:
            refresher = asyncio.create_task(self._discovery_loop())
        if not once:
            flusher = asyncio.create_task(self._heatmap_flush_loop())
        try:
            await super().run(once=once)
        finally:
            if refresher is not None:
                refresher.cancel()
            if flusher is not None:
                flusher.cancel()

    # ── Live config (registry poll, spec §05) ─────────────────────

    DRAWN_ZONE_NAME = "drawn"

    @staticmethod
    def _camera_key(raw_key: object, known: dict[str, CameraZone]) -> str | None:
        """Resolve a per-camera config key to this app's camera id.
        The catalog's geometry editor keys by the numeric core id
        (``"3"``); the app keys by the platform handle (``"cam3"``);
        hand-written config may use either. Unknown → None."""
        key = str(raw_key).strip()
        if key in known:
            return key
        if key.isdigit() and f"cam{key}" in known:
            return f"cam{key}"
        return None

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Apply catalog / Occupancy-page edits LIVE.

        Field bug this closes: the app never overrode this hook, so the
        thresholds the Occupancy page saves ("applied live", it said),
        the watch labels, and every zone drawn in the catalog were
        silently ignored until the container restarted — and even then
        drawn zones were never read. Idempotent (the poll re-delivers
        the same config on its first fetch); every rebind is one
        attribute swap, atomic enough under the GIL against the run
        loop and ``/state``.
        """
        cfg = self._config
        changed: list[str] = []

        if "watch_labels" in config:
            labels = [str(v).lower().strip() for v in (config.get("watch_labels") or [])
                      if str(v).strip()]
            if labels and labels != list(cfg.watch_labels):
                cfg.watch_labels = labels
                changed.append(f"watch_labels={labels}")

        new_max = cfg.default_max
        if "max_occupancy" in config:
            try:
                new_max = max(0, int(config.get("max_occupancy")))
            except (TypeError, ValueError):
                new_max = cfg.default_max
        new_min: int | None = cfg.default_min
        if "min_occupancy" in config:
            raw_min = config.get("min_occupancy")
            try:
                new_min = int(raw_min) if raw_min not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                new_min = cfg.default_min
            if new_min is not None and new_min < 0:
                new_min = None
        if new_min is not None and new_max and new_min > new_max:
            new_min = None
        if (new_max, new_min) != (cfg.default_max, cfg.default_min):
            cfg.default_max, cfg.default_min = new_max, new_min
            # App-level thresholds apply to every camera: the catalog
            # form has no per-camera ceiling, so a differing per-camera
            # value could only have come from hand-written YAML — and an
            # operator saving on the page expects the page to win.
            for cam in cfg.cameras.values():
                cam.max_occupancy = new_max
                cam.min_occupancy = new_min
            changed.append(f"max={new_max} min={new_min}")

        if "debounce_frames" in config:
            try:
                debounce = max(1, int(config.get("debounce_frames")))
            except (TypeError, ValueError):
                debounce = cfg.debounce_frames
            if debounce != cfg.debounce_frames:
                cfg.debounce_frames = debounce
                changed.append(f"debounce={debounce}")
        if "clear_alerts" in config:
            clear = bool(config.get("clear_alerts"))
            if clear != cfg.clear_alerts:
                cfg.clear_alerts = clear
                changed.append(f"clear_alerts={clear}")
        if "heatmap_enabled" in config:
            enabled = bool(config.get("heatmap_enabled"))
            if enabled != cfg.heatmap_enabled:
                cfg.heatmap_enabled = enabled
                if not enabled:
                    self._heat.clear()
                    self._heat_frames.clear()
                changed.append(f"heatmap={'on' if enabled else 'off'}")
        if "heatmap_publish_seconds" in config:
            try:
                period = max(5, int(config.get("heatmap_publish_seconds")))
            except (TypeError, ValueError):
                period = cfg.heatmap_publish_seconds
            if period != cfg.heatmap_publish_seconds:
                cfg.heatmap_publish_seconds = period
                changed.append(f"heatmap_period={period}s")

        if "max_dwell_seconds" in config:
            try:
                dwell = max(0, int(config.get("max_dwell_seconds") or 0))
            except (TypeError, ValueError):
                dwell = cfg.max_dwell_seconds
            if dwell != cfg.max_dwell_seconds:
                cfg.max_dwell_seconds = dwell
                changed.append(f"max_dwell={dwell}s")

        if "entry_line" in config:
            lines = config.get("entry_line")
            lines = lines if isinstance(lines, dict) else {}
            line_changes: list[str] = []
            for cam_id, cam in list(cfg.cameras.items()):
                raw_line = None
                for raw_key, val in lines.items():
                    if self._camera_key(raw_key, cfg.cameras) == cam_id:
                        raw_line = val
                        break
                wire = None
                if isinstance(raw_line, dict) and raw_line.get("a") and raw_line.get("b"):
                    try:
                        a, b = scale_vertices([raw_line["a"], raw_line["b"]],
                                              cam.frame_width, cam.frame_height)
                        wire = Tripwire.from_config(
                            name="entry", a=a, b=b,
                            count_direction="both")
                    except (TypeError, ValueError, KeyError) as exc:
                        logger.warning("entry line for %s ignored: %s", cam_id, exc)
                        continue
                before = cam.entry_line
                if (before is None) != (wire is None) or (
                        before is not None and wire is not None
                        and (before.a, before.b) != (wire.a, wire.b)):
                    cam.entry_line = wire
                    line_changes.append(f"{cam_id}:{'line' if wire else 'none'}")
            if line_changes:
                changed.append("entry_line " + ", ".join(line_changes))

        if "zones" in config:
            drawn = config.get("zones")
            drawn = drawn if isinstance(drawn, dict) else {}
            resolved: dict[str, list] = {}
            for raw_key, verts in drawn.items():
                cam_id = self._camera_key(raw_key, cfg.cameras)
                if cam_id is not None and isinstance(verts, list) and len(verts) >= 3:
                    resolved[cam_id] = verts
            rebuilt: dict[str, CameraZone] = {}
            zone_changes: list[str] = []
            for cam_id, cam in cfg.cameras.items():
                verts = resolved.get(cam_id)
                if verts is not None:
                    try:
                        zone = Zone.from_config(
                            name=self.DRAWN_ZONE_NAME,
                            vertices=scale_vertices(verts, cam.frame_width,
                                                    cam.frame_height))
                    except (TypeError, ValueError, KeyError) as exc:
                        logger.warning("zone for %s ignored: %s", cam_id, exc)
                        rebuilt[cam_id] = cam
                        continue
                    if cam.zone.name != self.DRAWN_ZONE_NAME \
                            or list(cam.zone.polygon) != list(zone.polygon):
                        zone_changes.append(f"{cam_id}:drawn({len(verts)} pts)")
                    cam = CameraZone(camera_id=cam_id, zone=zone,
                                     frame_width=cam.frame_width,
                                     frame_height=cam.frame_height,
                                     max_occupancy=cam.max_occupancy,
                                     min_occupancy=cam.min_occupancy,
                                     entry_line=cam.entry_line)
                elif cam.zone.name == self.DRAWN_ZONE_NAME:
                    # The operator erased the drawn zone → back to the
                    # whole frame, exactly as if it had never been drawn.
                    line = cam.entry_line
                    cam = _auto_camera_zone(cam_id, cam.max_occupancy,
                                            cam.min_occupancy)
                    cam.entry_line = line
                    zone_changes.append(f"{cam_id}:full-frame")
                rebuilt[cam_id] = cam
            if zone_changes:
                cfg.cameras = rebuilt
                changed.append("zones " + ", ".join(zone_changes))

        if changed:
            logger.info("config applied live: %s", "; ".join(changed))

    # ── Spatial heatmap ───────────────────────────────────────────

    @staticmethod
    def heat_cell(bbox: dict[str, Any]) -> int | None:
        """Grid index of a detection's FOOT point, or None for a box that
        cannot be placed. Foot point = bottom-centre of the normalised
        box: where the entity stands, which is what a floor heatmap
        shows — heads drift with height and camera pitch, feet don't."""
        try:
            x = float(bbox.get("x", 0.0)) + float(bbox.get("w", 0.0)) / 2.0
            y = float(bbox.get("y", 0.0)) + float(bbox.get("h", 0.0))
        except (TypeError, ValueError):
            return None
        if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
            # Clamp a foot just past the frame edge (a box clipped at the
            # bottom still has a foot) — but discard garbage far outside.
            if x < -0.05 or x > 1.05 or y < -0.05 or y > 1.05:
                return None
            x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
        col = min(HEATMAP_COLS - 1, int(x * HEATMAP_COLS))
        row = min(HEATMAP_ROWS - 1, int(y * HEATMAP_ROWS))
        return row * HEATMAP_COLS + col

    def accumulate_heat(self, camera_id: str, detections: list[Any]) -> int:
        """Bin this frame's watched detections into the camera's grid.
        Whole frame, not just the zone — the heatmap answers "where do
        they go", which the zone was drawn to ask. Returns cells hit."""
        if not self._config.heatmap_enabled:
            return 0
        grid = self._heat.get(camera_id)
        if grid is None:
            grid = self._heat[camera_id] = [0] * (HEATMAP_COLS * HEATMAP_ROWS)
        hits = 0
        for det in detections:
            if not isinstance(det, dict):
                continue
            if str(det.get("label", "")).lower() not in self._config.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            idx = self.heat_cell(bbox)
            if idx is None:
                continue
            grid[idx] += 1
            hits += 1
        self._heat_frames[camera_id] = self._heat_frames.get(camera_id, 0) + 1
        return hits

    def flush_heatmaps(self) -> int:
        """Publish every camera's accrued grid as one sparse
        ``occupancy.heatmap.v1`` delta and reset. Cameras whose grid
        stayed empty publish nothing (an empty room costs no bytes).
        Returns how many events were published."""
        published = 0
        for camera_id in list(self._heat):
            grid = self._heat.get(camera_id) or []
            frames = self._heat_frames.pop(camera_id, 0)
            cells = [[i, n] for i, n in enumerate(grid) if n]
            self._heat[camera_id] = [0] * (HEATMAP_COLS * HEATMAP_ROWS)
            if not cells:
                continue
            ok = self._occupancy_publisher.publish(
                HEATMAP_SCHEMA,
                camera_id=camera_id,
                payload={
                    "cols": HEATMAP_COLS,
                    "rows": HEATMAP_ROWS,
                    "cells": cells,
                    "frames": frames,
                    "period_seconds": self._config.heatmap_publish_seconds,
                    "labels": list(self._config.watch_labels),
                },
            )
            if ok:
                published += 1
                self._heatmaps_published += 1
        return published

    async def _heatmap_flush_loop(self) -> None:
        """Ship the accrued grids on a fixed cadence. Best-effort: a bus
        hiccup drops one period's deltas, never the counter."""
        while True:
            await asyncio.sleep(self._config.heatmap_publish_seconds)
            try:
                self.flush_heatmaps()
            except Exception:
                logger.warning("heatmap flush failed", exc_info=True)
            try:
                self.flush_footfall()
            except Exception:
                logger.warning("footfall flush failed", exc_info=True)

    # ── Pure helpers (testable without NATS) ──────────────────────

    def count_in_zone(self, camera: CameraZone, detections: list[Any]) -> int:
        """Count detections whose label is watched and whose bbox
        center falls inside the camera's zone."""
        count = 0
        for det in detections:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label", "")).lower()
            if label not in self._config.watch_labels:
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            center = bbox_center(bbox, camera.frame_width, camera.frame_height)
            if camera.zone.contains(center):
                count += 1
        return count

    def _classify(self, camera: CameraZone, count: int) -> str:
        """Map a raw count to an alerting band."""
        if count > camera.max_occupancy:
            return "over"
        if camera.min_occupancy is not None and count < camera.min_occupancy:
            return "under"
        return "normal"

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        """The occupancy rule for one event. Returns the alerts to fire
        (the SDK base dispatches them and ``handle_event`` returns
        them, which is what the tests assert on)."""
        camera = self._config.cameras.get(camera_id)
        if camera is None:
            # Another monitoring app may be watching this camera; we're not.
            # But an id that matches NOTHING configured is the difference
            # between "not my camera" and "this app counts nothing, forever"
            # — and the two look identical from outside. Say it once per
            # unknown id so a config/id mismatch (the classic ``cam-1`` vs
            # ``cam1``) is one log line instead of a silent no-op.
            if camera_id not in self._unknown_cameras:
                self._unknown_cameras.add(camera_id)
                logger.warning(
                    "receiving events for camera %r, which is not in my config "
                    "— nothing will be counted for it. Configured cameras: %s",
                    camera_id, sorted(self._config.cameras) or "(none)",
                )
            return []

        self.accumulate_heat(camera_id, detections)
        dwell_alerts = self.track_visitors(
            camera_id, camera, detections,
            self.parse_event_ts(event.get("completed_at")), event)
        count = self.count_in_zone(camera, detections)
        candidate = self._classify(camera, count)
        state = self._states.setdefault(camera_id, _ZoneState())
        state.last_count = count
        # History feed (EVENT_CONTRACTS.md occupancy.changed.v1) —
        # sampled here, forced below on committed transitions.
        self._publish_occupancy(camera_id, camera, count, state.level,
                                force=False)

        # Already in the candidate band → nothing to commit; clear any
        # half-formed pending transition (the level is stable).
        if candidate == state.level:
            state.pending = None
            state.pending_count = 0
            return dwell_alerts

        # Debounce: the candidate must persist for N consecutive frames
        # before we commit the transition and fire.
        if state.pending == candidate:
            state.pending_count += 1
        else:
            state.pending = candidate
            state.pending_count = 1

        if state.pending_count < self._config.debounce_frames:
            return dwell_alerts

        previous = state.level
        state.level = candidate
        state.pending = None
        state.pending_count = 0
        self._publish_occupancy(camera_id, camera, count, candidate,
                                force=True)

        # Returning to normal → only fire if clear_alerts is on.
        if candidate == "normal" and not self._config.clear_alerts:
            return dwell_alerts

        return dwell_alerts + [self._build_alert(
            camera=camera, count=count, level=candidate,
            previous=previous, event=event,
        )]

    # ── Footfall + dwell ──────────────────────────────────────────

    def _tally(self, camera_id: str) -> tuple[dict[str, float], dict[str, float]]:
        blank = {"entries": 0, "exits": 0, "dwell_count": 0,
                 "dwell_seconds": 0.0, "dwell_max": 0.0}
        delta = self._footfall.setdefault(camera_id, dict(blank))
        total = self._footfall_total.setdefault(camera_id, dict(blank))
        return delta, total

    def _finalise_dwell(self, camera_id: str, record: Any) -> None:
        """A track has left the zone (or expired inside it): record how
        long it stayed. Sub-second stays are noise from a box flickering
        across the edge, not a visit."""
        entered = record.data.get("entered_at")
        if entered is None:
            return
        seconds = max(0.0, float(record.data.get("last_ts", entered)) - float(entered))
        record.data["entered_at"] = None
        if seconds < 1.0:
            return
        for tally in self._tally(camera_id):
            tally["dwell_count"] += 1
            tally["dwell_seconds"] += seconds
            tally["dwell_max"] = max(tally["dwell_max"], seconds)

    def track_visitors(
        self, camera_id: str, camera: CameraZone, detections: list[Any],
        now: float, event: dict[str, Any],
    ) -> list[Alert]:
        """Per-track bookkeeping for one frame: entry-line crossings and
        zone dwell. Detections without a ``track_id`` contribute nothing
        here (they still count and heat). Returns dwell alerts."""
        # Expire tracks first: a visitor unseen for the TTL has left.
        for (cam, _track), record in self._tracks.gc(now):
            self._finalise_dwell(cam, record)
        alerts: list[Alert] = []
        for det in detections:
            if not isinstance(det, dict):
                continue
            if str(det.get("label", "")).lower() not in self._config.watch_labels:
                continue
            track_id = det.get("track_id")
            if track_id is None:
                if not self._warned_missing_track:
                    self._warned_missing_track = True
                    logger.info("detections carry no track_id — footfall and "
                                "dwell need a tracking producer (Tier-0 has one); "
                                "counts and heat still work")
                continue
            bbox = det.get("bbox")
            if not isinstance(bbox, dict):
                continue
            point = bbox_center(bbox, camera.frame_width, camera.frame_height)
            key = (camera_id, str(track_id))
            record = self._tracks.get(key)
            prev: Point | None = record.data.get("last_point") if record else None
            record = self._tracks.touch(key, at=now)
            record.data["last_point"] = point
            record.data["last_ts"] = now
            delta, total = self._tally(camera_id)
            # Entry line: a→b is in, b→a is out.
            if camera.entry_line is not None and prev is not None:
                direction = camera.entry_line.crossing(prev, point)
                if direction == "a_to_b":
                    delta["entries"] += 1
                    total["entries"] += 1
                elif direction == "b_to_a":
                    delta["exits"] += 1
                    total["exits"] += 1
            # Dwell: time between entering and leaving the zone.
            inside = camera.zone.contains(point)
            if inside and record.data.get("entered_at") is None:
                record.data["entered_at"] = now
                record.data["dwell_alerted"] = False
            elif not inside and record.data.get("entered_at") is not None:
                self._finalise_dwell(camera_id, record)
            if inside and self._config.max_dwell_seconds > 0 \
                    and not record.data.get("dwell_alerted"):
                stayed = now - float(record.data["entered_at"])
                if stayed > self._config.max_dwell_seconds:
                    record.data["dwell_alerted"] = True
                    alerts.append(self._build_dwell_alert(
                        camera=camera, track_id=str(track_id),
                        seconds=stayed, event=event))
        return alerts

    def current_dwell(self, camera_id: str, now: float | None = None) -> dict[str, float]:
        """Live view: how many tracks are inside the zone right now and
        the longest current stay."""
        now = time.time() if now is None else now
        inside = 0
        longest = 0.0
        for (cam, _t), record in self._tracks.items():
            if cam != camera_id or record.data.get("entered_at") is None:
                continue
            inside += 1
            longest = max(longest, now - float(record.data["entered_at"]))
        return {"inside_now": inside, "longest_current_dwell_s": round(longest, 1)}

    def flush_footfall(self) -> int:
        """Publish every camera's footfall/dwell delta and reset. A
        period with no entries, exits or finished dwells publishes
        nothing. Returns events published."""
        published = 0
        for camera_id in list(self._footfall):
            delta = self._footfall.get(camera_id) or {}
            if not any((delta.get("entries"), delta.get("exits"),
                        delta.get("dwell_count"))):
                continue
            payload = {
                "entries": int(delta["entries"]),
                "exits": int(delta["exits"]),
                "dwell_count": int(delta["dwell_count"]),
                "dwell_seconds": round(float(delta["dwell_seconds"]), 1),
                "dwell_max_seconds": round(float(delta["dwell_max"]), 1),
                "period_seconds": self._config.heatmap_publish_seconds,
                "labels": list(self._config.watch_labels),
            }
            self._footfall[camera_id] = {
                "entries": 0, "exits": 0, "dwell_count": 0,
                "dwell_seconds": 0.0, "dwell_max": 0.0}
            if self._occupancy_publisher.publish(
                    FOOTFALL_SCHEMA, camera_id=camera_id, payload=payload):
                published += 1
                self._footfall_published += 1
        return published

    def _build_dwell_alert(self, *, camera: CameraZone, track_id: str,
                           seconds: float, event: dict[str, Any]) -> Alert:
        return Alert(
            title=f"Long stay in zone {camera.zone.name!r}",
            description=(
                f"A tracked {'/'.join(self._config.watch_labels)} has been inside "
                f"{camera.zone.name!r} on {camera.camera_id} for "
                f"{int(seconds)} s (limit {self._config.max_dwell_seconds} s)."
            ),
            camera_id=camera.camera_id,
            severity="medium",
            correlation_id=str(event.get("correlation_id") or ""),
            evidence={
                "track_id": track_id,
                "dwell_seconds": round(seconds, 1),
                "max_dwell_seconds": self._config.max_dwell_seconds,
                "zone_name": camera.zone.name,
            },
            tags=["occupancy", "dwell", camera.zone.name],
        )

    def _publish_occupancy(self, camera_id: str, camera: Any,
                           count: int, level: str, *, force: bool) -> None:
        """Sampled occupancy.changed.v1 publish: always on a committed
        level transition (``force``), otherwise only when the count
        changed and the per-camera interval has passed. Bus trouble is
        the channel's problem (log-never-raise), never the counter's."""
        last = self._last_published.get(camera_id)
        now = time.monotonic()
        if not force:
            if last is not None and last[0] == count:
                return
            if last is not None and (now - last[1]) < PUBLISH_MIN_INTERVAL_SECONDS:
                return
        self._last_published[camera_id] = (count, now)
        ok = self._occupancy_publisher.publish(
            OCCUPANCY_SCHEMA,
            camera_id=camera_id,
            payload={
                "count": count,
                "level": level,
                "max_occupancy": getattr(camera, "max_occupancy", None),
                "min_occupancy": getattr(camera, "min_occupancy", None),
            },
        )
        if ok:
            self._history_events_published += 1

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — live occupancy per configured camera plus
        two roll-ups (total people, zones over limit) for the app's
        dashboard summary chips."""
        return {
            "total_people": sum(s.last_count for s in self._states.values()),
            "zones_over": sum(1 for s in self._states.values() if s.level == "over"),
            "history_events_published": self._history_events_published,
            "heatmaps_published": self._heatmaps_published,
            "heatmap_enabled": self._config.heatmap_enabled,
            "footfall_published": self._footfall_published,
            "cameras": {
                camera_id: {
                    "level": state.level,
                    "last_count": state.last_count,
                    "pending": state.pending,
                    "has_entry_line": (
                        camera_id in self._config.cameras
                        and self._config.cameras[camera_id].entry_line is not None),
                    # Since app start; the platform keeps the history.
                    "entries": int(self._footfall_total.get(camera_id, {}).get("entries", 0)),
                    "exits": int(self._footfall_total.get(camera_id, {}).get("exits", 0)),
                    "dwell_avg_s": round(
                        self._footfall_total[camera_id]["dwell_seconds"]
                        / self._footfall_total[camera_id]["dwell_count"], 1)
                        if self._footfall_total.get(camera_id, {}).get("dwell_count") else None,
                    "dwell_max_s": round(
                        self._footfall_total.get(camera_id, {}).get("dwell_max", 0.0), 1),
                    **self.current_dwell(camera_id),
                }
                for camera_id, state in self._states.items()
            },
        }

    def _build_alert(
        self,
        *,
        camera: CameraZone,
        count: int,
        level: str,
        previous: str,
        event: dict[str, Any],
    ) -> Alert:
        correlation_id = str(event.get("correlation_id") or "")
        if level == "over":
            title = f"Over-occupancy in zone {camera.zone.name!r}"
            description = (
                f"{count} entities in zone {camera.zone.name!r} on camera "
                f"{camera.camera_id} (limit {camera.max_occupancy})."
            )
            severity = "high"
        elif level == "under":
            title = f"Under-occupancy in zone {camera.zone.name!r}"
            description = (
                f"Only {count} entities in zone {camera.zone.name!r} on "
                f"camera {camera.camera_id} (minimum {camera.min_occupancy})."
            )
            severity = "medium"
        else:  # cleared
            title = f"Occupancy back to normal in zone {camera.zone.name!r}"
            description = (
                f"Zone {camera.zone.name!r} on camera {camera.camera_id} "
                f"returned to normal occupancy ({count}) from {previous!r}."
            )
            severity = "low"
        return Alert(
            title=title,
            description=description,
            camera_id=camera.camera_id,
            severity=severity,
            correlation_id=correlation_id,
            evidence={
                "count": count,
                "level": level,
                "previous_level": previous,
                "max_occupancy": camera.max_occupancy,
                "min_occupancy": camera.min_occupancy,
                "zone_name": camera.zone.name,
                "watch_labels": self._config.watch_labels,
                "adapter": event.get("adapter"),
                "adapter_version": event.get("adapter_version"),
                "model_fingerprint": event.get("model_fingerprint"),
            },
            tags=["occupancy", level, camera.zone.name],
        )


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the dispatcher."""
    return app(OccupancyCounter, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
