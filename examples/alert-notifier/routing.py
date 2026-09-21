# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which alerts deserve a human's attention right now, and whose.

This is the half of a notifier that is not "POST a string". Everything
in here exists to send FEWER notifications, because that is the actual
problem. Two numbers set the design:

* 94-98% of burglar-alarm activations are false (US police data,
  Arizona State University's *False Burglar Alarms* problem guide);
* in the far better studied clinical literature, 80-99% of patient
  monitor alarms are false or clinically insignificant — and tuning
  thresholds cut alarm volume by over 80% without missing events.

An alerting product whose job description is "deliver alerts" builds
alarm fatigue. The job is suppression: deliver the few that matter, once
each, with enough context to judge them in a second.

Five mechanisms, in the order an alert meets them:

``RuleSet``      a flat, ordered, first-match-wins list with a pinned
                 catch-all. Not a nested policy tree: Grafana shipped
                 one, found users "not knowing where those alerts were
                 going", and added a flat path in 2024. Prometheus ships
                 a separate web app whose only purpose is simulating
                 which receiver an alert hits. We are an NVR with a
                 dozen cameras; a numbered list people can read top to
                 bottom wins.
``Inhibitor``    while a person alert is live on a camera, the motion
                 alert behind it is noise. No NVR has this concept.
``QuietHours``   a mute window that holds alerts rather than dropping
                 them, with a severity that always breaks through.
``Muting``       an explicit, always-expiring pause. A mute with no
                 expiry is a coverage gap nobody remembers creating.
``Grouper``      hold briefly, then collapse siblings into one message.
                 Alertmanager's ``group_wait``, which the whole NVR
                 field substitutes a blunt cooldown for.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from channels import SEVERITY_RANK, Message, severity_name, severity_rank

logger = logging.getLogger("alert-notifier.routing")

try:  # stdlib since 3.9; a missing tzdata must not stop the app booting
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Ceiling on the hold before a grouped message goes out. Past this the
#: feature stops being "collapse the burst" and becomes "your alerts are
#: late", which is worse than duplicates.
MAX_GROUP_WAIT_SECONDS = 120.0

#: Ceiling on any mute. There is no "mute for ever" in a security
#: product: an operator who forgets is otherwise unprotected silently.
MAX_MUTE_MINUTES = 24 * 60


# ── The normalised alert ────────────────────────────────────────────


@dataclass
class Incident:
    """One alert off the bus, in the shape the rules match against.

    Built once per message so a rule list of any length costs one parse.
    ``key`` is what makes an alert "the same alert" for dedup, grouping
    and in-place editing.
    """

    title: str = "Alert"
    description: str = ""
    severity: str = "high"
    camera_id: str = ""
    camera_name: str = ""
    alert_type: str = ""
    source: str = ""
    zones: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    fired_at: str = ""
    alert_id: str = ""
    correlation_id: str = ""
    image: bytes | None = None
    at: float = 0.0

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)

    @property
    def key(self) -> str:
        """The dedup key.

        ``correlation_id`` when the producer set one — that is the whole
        point of the field, and it ties an alert's updates together
        across restarts. Otherwise camera + title, which is as close as
        we can get without inventing an identity."""
        if self.correlation_id:
            return f"corr:{self.correlation_id}"
        return f"ct:{self.camera_id}|{self.title.lower().strip()}"

    @property
    def where(self) -> str:
        return self.camera_name or (f"camera {self.camera_id}"
                                    if self.camera_id else "")


def parse_incident(envelope: dict[str, Any], *,
                   camera_names: dict[str, str] | None = None,
                   now: float | None = None) -> Incident:
    """One bus message → an :class:`Incident`.

    Tolerates both wire shapes. Alerts travel on
    ``opennvr.alerts.{kind}.{name}.{camera}`` as a bare alert dict, and
    the contracted domain envelope wraps that same dict as ``payload``.
    Reading both costs three lines and means a future domain publisher
    does not need a new app.
    """
    body = envelope.get("payload") if isinstance(
        envelope.get("payload"), dict) else envelope
    evidence = body.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    source = body.get("source")
    source_name = ""
    if isinstance(source, dict):
        source_name = str(source.get("name") or "")
    elif isinstance(source, str):
        source_name = source

    camera_id = str(body.get("camera_id") or envelope.get("camera_id") or "")
    tags = [str(t) for t in (body.get("tags") or []) if isinstance(t, str)]
    alert_type = str(body.get("alert_type") or "") or _type_from_tags(tags)

    return Incident(
        title=str(body.get("title") or "Alert")[:200],
        description=str(body.get("description") or "")[:2000],
        severity=severity_name(body.get("severity")),
        camera_id=camera_id,
        camera_name=(camera_names or {}).get(camera_id, ""),
        alert_type=alert_type,
        source=source_name,
        zones=_zones_from(evidence),
        tags=tags,
        fired_at=str(body.get("fired_at") or ""),
        alert_id=str(body.get("alert_id") or ""),
        correlation_id=str(body.get("correlation_id") or ""),
        image=_inline_image(evidence),
        at=time.time() if now is None else now,
    )


def _type_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("type:") and tag[5:].strip():
            return tag[5:].strip()[:40]
    return ""


def _zones_from(evidence: dict[str, Any]) -> list[str]:
    for key in ("zones", "zone_names", "zone"):
        raw = evidence.get(key)
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        if isinstance(raw, list):
            out = [str(z).strip() for z in raw if str(z).strip()]
            if out:
                return out
    return []


#: Evidence keys that may carry a base64 JPEG inline.
_INLINE_IMAGE_KEYS = ("snapshot_b64", "image_b64", "face_b64", "scene_b64")

#: Guard on an inline photo. The bus caps a message at 1 MB, so anything
#: past this is not a snapshot, it is a bug or an attack.
MAX_INLINE_IMAGE_BYTES = 2_000_000


def _inline_image(evidence: dict[str, Any]) -> bytes | None:
    """The detection frame, when the producer inlined one.

    Most apps upload their JPEG to the evidence store and send a PATH,
    which this app cannot resolve — it has no route to the store. Those
    alerts travel without a photo and say so. Producers that inline a
    base64 frame give us the real detection frame for free, so we take
    it.
    """
    import base64
    import binascii

    for key in _INLINE_IMAGE_KEYS:
        raw = evidence.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        if len(payload) > MAX_INLINE_IMAGE_BYTES * 2:
            logger.warning("ignoring oversized inline image on %r", key)
            continue
        try:
            data = base64.b64decode(payload, validate=False)
        except (ValueError, binascii.Error):
            continue
        # A JPEG starts FF D8 FF. Checking beats trusting a field name:
        # a channel that uploads a non-image gets a 400 at 3am.
        if data[:3] == b"\xff\xd8\xff" and len(data) <= MAX_INLINE_IMAGE_BYTES:
            return data
    return None


# ── Matching ────────────────────────────────────────────────────────


@dataclass
class Matcher:
    """What a rule matches. Every field left empty means "don't care" —
    a matcher with nothing set matches everything, which is exactly what
    the catch-all needs."""

    cameras: list[str] = field(default_factory=list)
    min_severity: str = ""
    alert_types: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    title_contains: str = ""
    days: list[str] = field(default_factory=list)
    from_time: str = ""
    to_time: str = ""

    def matches(self, inc: Incident, *, local: datetime | None = None) -> bool:
        if self.min_severity and inc.rank < severity_rank(self.min_severity):
            return False
        if self.cameras and not _any_glob(
                self.cameras, (inc.camera_id, inc.camera_name)):
            return False
        if self.alert_types and not _any_glob(self.alert_types,
                                              (inc.alert_type,)):
            return False
        if self.sources and not _any_glob(self.sources, (inc.source,)):
            return False
        if self.zones and not any(
                _any_glob(self.zones, (z,)) for z in inc.zones):
            return False
        if self.title_contains and self.title_contains.lower() not in (
                f"{inc.title} {inc.description}".lower()):
            return False
        if self.days or self.from_time or self.to_time:
            # Derive the wall clock when the caller did not supply one,
            # rather than skipping the predicate: silently ignoring a
            # night-only rule at noon is a rule that fires when it was
            # configured not to.
            when = local if local is not None else datetime.fromtimestamp(
                inc.at or time.time(), timezone.utc)
            if not self._time_matches(when):
                return False
        return True

    def _time_matches(self, local: datetime) -> bool:
        window = TimeWindow(self.days, self.from_time, self.to_time)
        return window.contains(local)

    def is_catch_all(self) -> bool:
        return not any((self.cameras, self.min_severity, self.alert_types,
                        self.sources, self.zones, self.title_contains,
                        self.days, self.from_time, self.to_time))

    def covers(self, other: "Matcher") -> bool:
        """True when everything ``other`` matches, this matches too.

        Used for the shadowing warning: in a first-match list, a rule
        below a broader one can never fire, and a rule that never fires
        is a silent coverage gap. Deliberately conservative — it reports
        only relationships it can prove, so a "shadowed" badge is never
        wrong, at the cost of missing some real ones.
        """
        # A time-scoped rule above proves nothing about a rule below,
        # because the rule below still fires outside its hours.
        if self.days or self.from_time or self.to_time:
            return False
        if self.min_severity and (
                not other.min_severity
                or severity_rank(other.min_severity) < severity_rank(
                    self.min_severity)):
            return False
        for mine, theirs in ((self.cameras, other.cameras),
                             (self.alert_types, other.alert_types),
                             (self.sources, other.sources),
                             (self.zones, other.zones)):
            if not mine:
                continue
            if not theirs:
                return False
            if not all(_any_glob(mine, (t,)) for t in theirs):
                return False
        if self.title_contains and (
                self.title_contains.lower() not in
                (other.title_contains or "").lower()):
            return False
        return True


def _any_glob(patterns: Iterable[str], values: Iterable[str]) -> bool:
    """Case-insensitive match with ``*`` support.

    Globs because "every gate camera" is ``gate-*`` and typing eight
    camera names is how a ninth camera ends up unwatched."""
    vals = [v.lower() for v in values if v]
    if not vals:
        return False
    for raw in patterns:
        pattern = str(raw).lower().strip()
        if not pattern:
            continue
        for value in vals:
            if pattern == value or fnmatch.fnmatchcase(value, pattern):
                return True
    return False


# ── Time windows ────────────────────────────────────────────────────


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class TimeWindow:
    """A weekly window. Days plus a clock range, no cron anywhere.

    Crossing midnight is the first thing hand-rolled schedules get
    wrong, so it is explicit: ``22:00``-``07:00`` means the evening AND
    the following morning, and the DAY is the day the window STARTED —
    "mon 22:00-07:00" covers Tuesday's small hours, which is what a
    person means by "Monday night".
    """

    days: list[str] = field(default_factory=list)
    from_time: str = ""
    to_time: str = ""

    def __post_init__(self) -> None:
        self.days = [d.lower().strip()[:3] for d in self.days
                     if str(d).lower().strip()[:3] in DAYS]
        for value in (self.from_time, self.to_time):
            if value and not _TIME_RE.match(value):
                raise ValueError(
                    f"time must be HH:MM in 24-hour form, got {value!r}")

    @property
    def empty(self) -> bool:
        """``22:00``-``22:00`` is somebody typing the same time twice.

        Read as crossing midnight it would mean all 24 hours, which for
        a quiet-hours window is "silent for ever" — the opposite of the
        likely intent and the worst possible reading in a security
        product. It matches nothing instead."""
        return bool(self.from_time and self.to_time
                    and _minutes(self.to_time) == _minutes(self.from_time))

    @property
    def crosses_midnight(self) -> bool:
        return bool(self.from_time and self.to_time
                    and _minutes(self.to_time) < _minutes(self.from_time))

    def contains(self, local: datetime) -> bool:
        if self.empty:
            return False
        now = local.hour * 60 + local.minute
        today = DAYS[local.weekday()]
        if not self.from_time and not self.to_time:
            return not self.days or today in self.days

        start = _minutes(self.from_time) if self.from_time else 0
        end = _minutes(self.to_time) if self.to_time else 24 * 60

        if not self.crosses_midnight:
            in_clock = start <= now < end
            return in_clock and (not self.days or today in self.days)

        # Evening leg: today must be a selected day.
        if now >= start:
            return not self.days or today in self.days
        # Morning leg: YESTERDAY must have been a selected day.
        if now < end:
            yesterday = DAYS[(local.weekday() - 1) % 7]
            return not self.days or yesterday in self.days
        return False

    def describe(self) -> str:
        if self.empty:
            return f"{self.from_time}–{self.to_time} (empty — same start and end)"
        if not self.from_time and not self.to_time:
            return "all day " + (", ".join(self.days) if self.days else "every day")
        span = f"{self.from_time or '00:00'}–{self.to_time or '24:00'}"
        if self.crosses_midnight:
            span += " (overnight)"
        return f"{span} on " + (", ".join(self.days) if self.days else "every day")


def _minutes(value: str) -> int:
    match = _TIME_RE.match(value or "")
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def local_now(tz_name: str, now: float | None = None) -> datetime:
    """Wall-clock time in the site's zone.

    Falls back to UTC on an unknown zone rather than raising: a typo in
    a timezone name must not stop alerts going out, it must make the
    schedule visibly wrong on the page."""
    stamp = time.time() if now is None else now
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.fromtimestamp(stamp, ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 — unknown zone, missing tzdata
            logger.warning("unknown timezone %r — using UTC", tz_name)
    return datetime.fromtimestamp(stamp, timezone.utc)


# ── Rules ───────────────────────────────────────────────────────────


@dataclass
class Rule:
    """One row of the list: when THIS, send there."""

    name: str
    channels: list[str] = field(default_factory=list)
    matcher: Matcher = field(default_factory=Matcher)
    enabled: bool = True
    #: Skip the quiet-hours hold for this rule (a gate fault at 2am).
    ignore_quiet_hours: bool = False
    #: Per-rule override of the global collapse window.
    group_wait_seconds: float | None = None
    #: The pinned last row. Always matches, cannot be deleted, and may
    #: legitimately have no channels — "everything else: nothing".
    catch_all: bool = False

    def describe(self) -> str:
        m = self.matcher
        bits = []
        if m.min_severity:
            bits.append(f"{m.min_severity} or above")
        if m.alert_types:
            bits.append(" / ".join(m.alert_types))
        if m.cameras:
            bits.append("on " + ", ".join(m.cameras))
        if m.zones:
            bits.append("in " + ", ".join(m.zones))
        if m.sources:
            bits.append("from " + ", ".join(m.sources))
        if m.title_contains:
            bits.append(f"mentioning {m.title_contains!r}")
        if m.days or m.from_time or m.to_time:
            bits.append(TimeWindow(m.days, m.from_time, m.to_time).describe())
        when = " ".join(bits) if bits else "any alert"
        where = ", ".join(self.channels) if self.channels else "nothing"
        return f"When {when} → {where}"


class RuleSet:
    """The ordered list. First match wins; the last row always matches.

    Deliberately NOT a tree, and deliberately without Alertmanager's
    ``continue``. Fan-out is a rule with several channels, which reads
    as one sentence; ``continue`` is flow control, and flow control is
    what makes a routing config unreadable.
    """

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules: list[Rule] = []
        for rule in rules or []:
            self.rules.append(rule)
        self._ensure_catch_all()

    def _ensure_catch_all(self) -> None:
        """Exactly one catch-all, and it is last.

        A hidden default is how an operator ends up unable to answer
        "what happens to everything else?" — so it is a real row."""
        existing = [r for r in self.rules if r.catch_all]
        self.rules = [r for r in self.rules if not r.catch_all]
        if existing:
            tail = existing[0]
            tail.matcher = Matcher()
        else:
            # A name nothing else is using. The backtest and the
            # delivery log are keyed by name, so colliding with a rule
            # the operator wrote merges two rules' counts into one
            # number shown against both rows.
            name = "Everything else"
            taken = {r.name for r in self.rules}
            suffix = 2
            while name in taken:
                name = f"Everything else ({suffix})"
                suffix += 1
            tail = Rule(name=name, channels=[], matcher=Matcher(),
                        catch_all=True)
        self.rules.append(tail)

    def match(self, inc: Incident, *,
              local: datetime | None = None) -> Rule | None:
        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.matcher.matches(inc, local=local):
                return rule
        return None  # unreachable while a catch-all exists; defensive

    def shadowed(self) -> list[tuple[int, int]]:
        """``(index, shadowed_by)`` for every rule that can never fire.

        The one affordance a flat list has over a tree, and worth more
        than the tree's expressiveness: a rule that silently never fires
        is a coverage gap the operator believes they closed."""
        out: list[tuple[int, int]] = []
        active = [(i, r) for i, r in enumerate(self.rules) if r.enabled]
        for pos, (idx, rule) in enumerate(active):
            # The catch-all is deliberately exempt. It is pinned, it
            # cannot be deleted or reordered, and a sensible config with
            # one broad rule above it makes it unreachable — so a
            # "never fires" badge there would be a warning the operator
            # cannot act on, and un-actionable warnings teach people to
            # ignore warnings. Its zero match count says it plainly.
            if rule.catch_all:
                continue
            for above_idx, above in active[:pos]:
                if above.matcher.covers(rule.matcher):
                    out.append((idx, above_idx))
                    break
        return out

    def backtest(self, history: Iterable[Incident], *,
                 tz_name: str = "UTC") -> dict[str, int]:
        """How many of the recent alerts each rule would catch.

        The trust mechanism. An operator cannot reason about a predicate
        in the abstract; they can absolutely reason about "this rule
        would have fired 47 times yesterday". Runs the SAME matcher the
        live path runs — a preview that reimplements the engine
        eventually disagrees with it, and a lying preview is worse than
        none.
        """
        counts = {rule.name: 0 for rule in self.rules}
        for inc in history:
            local = local_now(tz_name, inc.at)
            rule = self.match(inc, local=local)
            if rule is not None:
                counts[rule.name] = counts.get(rule.name, 0) + 1
        return counts


# ── Inhibition ──────────────────────────────────────────────────────


class Inhibitor:
    """While a serious alert is live on a camera, hush the lesser ones.

    A person walks past the driveway camera: the intrusion app raises
    ``person`` at high, and motion, line-crossing and occupancy all
    raise their own at medium about the same thing. Four notifications,
    one event. Alertmanager calls this an inhibit rule; no NVR has the
    concept at all.

    Strictly one-directional and strictly downward: a HIGHER severity
    is never suppressed by a lower one, so this can only ever make a
    notification quieter, never lose the important one.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = max(0.0, float(window_seconds))
        self._live: dict[str, tuple[int, float]] = {}

    def observe(self, inc: Incident) -> None:
        if not inc.camera_id:
            return
        rank, at = self._live.get(inc.camera_id, (-1, 0.0))
        if inc.at - at > self.window or inc.rank >= rank:
            self._live[inc.camera_id] = (inc.rank, inc.at)

    def inhibits(self, inc: Incident) -> str:
        """The reason this alert is suppressed, or ``""``."""
        if self.window <= 0 or not inc.camera_id:
            return ""
        rank, at = self._live.get(inc.camera_id, (-1, 0.0))
        if rank <= inc.rank or (inc.at - at) > self.window:
            return ""
        higher = next((n for n, r in SEVERITY_RANK.items() if r == rank),
                      "a higher-severity")
        return (f"a {higher} alert is already live on this camera "
                f"({int(inc.at - at)}s ago)")

    def prune(self, now: float) -> None:
        for camera in [c for c, (_r, at) in self._live.items()
                       if now - at > max(self.window, 1.0) * 4]:
            self._live.pop(camera, None)


# ── Quiet hours ─────────────────────────────────────────────────────


@dataclass
class QuietHours:
    """When not to ring, and what to do with what arrives meanwhile.

    ``hold`` is the default and the one that respects a security
    product's contract: the alert is not lost, it is delivered as a
    summary when the window ends. ``silent`` delivers without a buzz.
    ``drop`` throws it away, which is offered but never the default.

    ``breakthrough`` always lets the top of the ladder through. Alarm
    fatigue is the danger being designed against here, but a fire at
    3am is the reason the system exists.
    """

    enabled: bool = False
    windows: list[TimeWindow] = field(default_factory=list)
    mode: str = "hold"
    breakthrough: str = "critical"

    MODES = ("hold", "silent", "drop")

    def __post_init__(self) -> None:
        if self.mode not in self.MODES:
            raise ValueError(
                f"quiet_hours.mode must be one of {', '.join(self.MODES)}")

    def active(self, local: datetime) -> bool:
        return self.enabled and any(w.contains(local) for w in self.windows)

    def verdict(self, inc: Incident, local: datetime) -> str:
        """``""`` to deliver normally, else ``hold`` / ``silent`` / ``drop``."""
        if not self.active(local):
            return ""
        if self.breakthrough and inc.rank >= severity_rank(self.breakthrough):
            return ""
        return self.mode

    def ends_at(self, local: datetime) -> str:
        for window in self.windows:
            if window.contains(local) and window.to_time:
                return window.to_time
        return ""


# ── Muting ──────────────────────────────────────────────────────────


class Muting:
    """An explicit pause, always with an end.

    Every mute expires: ``MAX_MUTE_MINUTES`` is a hard ceiling, and a
    request for longer is clamped rather than refused. A security
    product must not offer a switch that silently stays off, and the
    page shows a live countdown for whatever is muted — an invisible
    coverage gap is the actual dark pattern in this category.
    """

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    def mute(self, scope: str, minutes: float, now: float) -> float:
        span = max(1.0, min(float(minutes), float(MAX_MUTE_MINUTES)))
        until = now + span * 60.0
        self._until[scope or "*"] = until
        return until

    def unmute(self, scope: str) -> bool:
        return self._until.pop(scope or "*", None) is not None

    def muted(self, inc: Incident, now: float) -> str:
        """The scope silencing this alert, or ``""``."""
        for scope in ("*", inc.camera_id):
            if not scope:
                continue
            until = self._until.get(scope)
            if until is None:
                continue
            if until > now:
                return "everything" if scope == "*" else f"camera {scope}"
            self._until.pop(scope, None)
        return ""

    def active(self, now: float) -> dict[str, float]:
        """``{scope: minutes left}`` — what the page's banner shows."""
        return {scope: (until - now) / 60.0
                for scope, until in self._until.items() if until > now}

    def snapshot(self) -> dict[str, float]:
        return dict(self._until)

    def restore(self, saved: Any, now: float) -> None:
        """Reload across a restart, dropping anything already expired.

        1.0 kept its flood state in memory only, so a restart re-sent
        everything it had just suppressed."""
        if not isinstance(saved, dict):
            return
        for scope, until in saved.items():
            try:
                stamp = float(until)
            except (TypeError, ValueError):
                continue
            if stamp <= now:
                continue
            # Re-cap on restore, not only on create. A pause written
            # while the clock was wrong — an appliance with no RTC, a
            # container before NTP settles — is an absolute timestamp
            # that can sit months in the future, and the promise this
            # class makes is that there is no "mute for ever".
            self._until[str(scope)] = min(
                stamp, now + MAX_MUTE_MINUTES * 60.0)


# ── Grouping ────────────────────────────────────────────────────────


@dataclass
class Group:
    """Alerts held together, waiting to go out as one message."""

    key: str
    rule: str
    channels: list[str]
    members: list[Incident] = field(default_factory=list)
    opened_at: float = 0.0
    #: How long THIS group collapses for. Taken from the rule when it
    #: sets one, so a per-rule window is honoured at flush time and not
    #: merely at the "send immediately" shortcut.
    wait: float = 0.0
    #: Set once delivered, so a later member EDITS rather than re-sends.
    handles: dict[str, str] = field(default_factory=dict)
    sent_at: float = 0.0
    #: The severity this group was delivered AT. An edit does not
    #: re-alert anyone — Telegram's editMessageText changes the message
    #: in place with no buzz — so an alert more serious than this one
    #: must never be folded in as an update.
    sent_rank: int = -1

    @property
    def lead(self) -> Incident:
        """The alert the message is about: the most severe, earliest
        first among equals. Collapsing must never bury the worst one
        under whichever happened to arrive first."""
        return sorted(self.members, key=lambda i: (-i.rank, i.at))[0]


def _window(value: Any, default: float) -> float:
    """A collapse window, clamped, with NaN excluded.

    ``min(nan, cap)`` is ``nan`` and ``now - opened_at >= nan`` is always
    False, so an unchecked NaN does not produce a long window — it
    produces a group that is never flushed, i.e. alerts that are held
    for ever and delivered to nobody.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return max(0.0, min(num, MAX_GROUP_WAIT_SECONDS))


class Grouper:
    """Hold briefly; send once.

    Alertmanager's ``group_wait``, which the NVR field substitutes a
    blunt cooldown for. A cooldown either spams (too short) or drops the
    second, different event (too long); a hold merges only what actually
    arrives together and delays nothing else.

    ``flush_due`` returns groups whose window has closed. Members that
    arrive AFTER a group was sent update it in place where the channel
    can edit, which is how one notification counts up instead of nine
    stacking.
    """

    def __init__(self, wait_seconds: float = 20.0,
                 update_window_seconds: float = 300.0) -> None:
        self.wait = _window(wait_seconds, 0.0)
        self.update_window = max(0.0, float(update_window_seconds))
        self._open: dict[str, Group] = {}
        self._sent: dict[str, Group] = {}

    @staticmethod
    def group_key(inc: Incident, rule: Rule) -> str:
        """One group per (rule, camera).

        Not per alert: the whole point is merging DIFFERENT alerts that
        describe one situation. Not per rule alone: two cameras at once
        are two situations and deserve two notifications."""
        return f"{rule.name}|{inc.camera_id}"

    def add(self, inc: Incident, rule: Rule,
            channels: list[str]) -> tuple[Group | None, bool]:
        """Returns ``(group, is_update)``.

        A group is returned immediately when the wait is zero or when it
        updates an already-sent group; otherwise ``(None, False)`` and
        it comes back from :meth:`flush_due`."""
        key = self.group_key(inc, rule)

        sent = self._sent.get(key)
        if sent is not None:
            escalation = inc.rank > sent.sent_rank
            if inc.at - sent.sent_at <= self.update_window and not escalation:
                if any(m.key == inc.key for m in sent.members):
                    # The same alert re-firing inside the window: it is
                    # already represented, so nothing goes out.
                    return None, False
                sent.members.append(inc)
                return sent, True
            # Out of the window, or an ESCALATION. Escalation gets a new
            # message rather than an edit, because an edit buzzes
            # nobody: quietly folding a critical into the notification
            # raised for a medium one is how the alert that mattered
            # reaches a phone that never rings.
            self._sent.pop(key, None)
            self._open.pop(key, None)
            if escalation:
                # And it goes out NOW. Holding it for the collapse
                # window would re-impose the very silence the
                # escalation path exists to break — and it carries the
                # burst it escalated out of, so the message still reads
                # as one situation.
                group = Group(key=key, rule=rule.name,
                              channels=list(channels), opened_at=inc.at,
                              wait=0.0)
                group.members = list(sent.members) + [inc]
                self._mark_sent(group, inc.at)
                return group, False

        group = self._open.get(key)
        if group is None:
            wait = self.wait
            if rule.group_wait_seconds is not None:
                wait = _window(rule.group_wait_seconds, self.wait)
            group = Group(key=key, rule=rule.name, channels=list(channels),
                          opened_at=inc.at, wait=wait)
            group.members.append(inc)
            if wait <= 0:
                self._mark_sent(group, inc.at)
                return group, False
            self._open[key] = group
            return None, False

        if not any(m.key == inc.key for m in group.members):
            group.members.append(inc)
        return None, False

    def flush_due(self, now: float) -> list[Group]:
        due = []
        for key, group in list(self._open.items()):
            # The GROUP's window, not the global one — a rule that sets
            # its own collapse window is otherwise flushed on somebody
            # else's schedule and the setting silently does nothing.
            if now - group.opened_at >= group.wait:
                self._open.pop(key, None)
                self._mark_sent(group, now)
                due.append(group)
        return due

    def flush_all(self) -> list[Group]:
        """Everything still held — for shutdown, so a hold in flight is
        delivered rather than silently dropped."""
        out = list(self._open.values())
        self._open.clear()
        for group in out:
            self._mark_sent(group, time.time())
        return out

    def _mark_sent(self, group: Group, at: float) -> None:
        group.sent_at = at
        group.sent_rank = group.lead.rank
        self._sent[group.key] = group

    def adopt(self, other: "Grouper") -> None:
        """Take over another Grouper's in-flight work.

        Config is applied by rebuilding, and a rebuilt Grouper starts
        empty — which would silently DELETE every alert currently
        inside a collapse window, with no delivery, no suppression
        record and no log line. It would also lose the channel handles
        that make a later member an edit rather than a second
        notification.
        """
        self._open.update(other._open)
        self._sent.update(other._sent)

    def sent_group(self, key: str) -> Group | None:
        """An already-delivered group, for the channel handles that let
        a later member EDIT it rather than send again."""
        return self._sent.get(key)

    def prune(self, now: float) -> None:
        for key, group in list(self._sent.items()):
            if now - group.sent_at > max(self.update_window, 60.0) * 2:
                self._sent.pop(key, None)

    @property
    def pending(self) -> int:
        return len(self._open)


# ── Rendering ───────────────────────────────────────────────────────


def render(group: Group, *, base_url: str = "", tz_name: str = "UTC",
           actions_enabled: bool = False) -> Message:
    """A group → the message a channel will shape for itself.

    Title is object-and-place, because on a lock screen that is the only
    line that reads at a glance and the app name is already in the
    header. The body carries the discriminator — the thing that decides
    act-or-ignore — and the time.
    """
    lead = group.lead
    members = sorted(group.members, key=lambda i: (-i.rank, i.at))
    # The camera is NOT folded into the title here: ``Message.subject``
    # adds it for the channels with a title field, and ``Message.text``
    # puts it on the meta line. Doing it in both places is how a
    # notification ends up reading "Person at gate — Front Door" above
    # "Front Door · 02:14".
    title = lead.title

    body_bits = []
    if lead.description:
        body_bits.append(lead.description)
    if lead.zones:
        body_bits.append("zone " + ", ".join(lead.zones))
    body = " · ".join(body_bits)

    actions: list[Any] = []
    if base_url and actions_enabled:
        from channels import Action as ChannelAction

        actions.append(ChannelAction("View in OpenNVR",
                                     f"{base_url.rstrip('/')}/alerts"))

    return Message(
        title=title,
        body=body,
        severity=lead.severity,
        camera=lead.where,
        when=_when(lead, tz_name),
        url=f"{base_url.rstrip('/')}/alerts" if base_url else "",
        image=next((m.image for m in members if m.image), None),
        actions=actions,
        dedup_key=group.key,
        # Unique per MESSAGE, unlike dedup_key which names the
        # situation and is reused for the next alert on the same
        # camera. Matrix seeds its transaction id from this, and
        # reusing a transaction id makes the server silently discard a
        # brand-new alert as a replay.
        message_id=f"{group.key}:{group.sent_at or group.opened_at:.3f}",
        group=len(members),
        members=[(m.severity, m.title, m.where) for m in members],
    )


def _when(inc: Incident, tz_name: str = "UTC") -> str:
    """The alert's own timestamp where it has one, as HH:MM:SS.

    A wall-clock time beats "now": by the time a phone shows it, "now"
    is a lie, and at 3am the difference between 02:14 and 02:51 is the
    difference between two stories. In the SITE's zone, not the
    container's — ``astimezone()`` with no argument uses the process's
    local zone, which in a container with no ``TZ`` is UTC, so a site
    in Asia/Kolkata got a headline time five and a half hours out.
    """
    zone = _zone(tz_name)
    raw = inc.fired_at
    if raw:
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return raw[:19]
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(zone).strftime("%H:%M:%S")
    return datetime.fromtimestamp(inc.at, zone).strftime("%H:%M:%S")


def _zone(tz_name: str) -> Any:
    if tz_name and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            pass
    return timezone.utc


def clock(tz_name: str, stamp: float | None = None) -> str:
    """HH:MM in the site's zone — for "paused until", and the time on a
    summary. Shares ``_zone`` with :func:`_when` so the two can never
    disagree about which zone the site is in."""
    at = time.time() if stamp is None else stamp
    return datetime.fromtimestamp(at, _zone(tz_name)).strftime("%H:%M")


# ── Config parsing ──────────────────────────────────────────────────


def parse_matcher(raw: Any) -> Matcher:
    raw = raw if isinstance(raw, dict) else {}
    unknown = sorted(set(raw) - {
        "cameras", "min_severity", "alert_types", "sources", "zones",
        "title_contains", "days", "from", "to"})
    if unknown:
        raise ValueError(
            f"rule matcher: unknown setting(s) {', '.join(unknown)}")
    severity = str(raw.get("min_severity") or "").lower().strip()
    if severity and severity not in SEVERITY_RANK:
        raise ValueError(
            f"rule matcher: min_severity must be one of "
            f"{', '.join(SEVERITY_RANK)}, got {severity!r}")
    # Echo what the operator actually typed, not our truncation of it:
    # "unknown day(s) mun" for an input of "munday" reads like our bug.
    given = [str(d).strip() for d in (raw.get("days") or [])]
    days = [d.lower()[:3] for d in given]
    bad = [original for original, short in zip(given, days)
           if short not in DAYS]
    if bad:
        raise ValueError(
            f"rule matcher: unknown day(s) {', '.join(bad)} — "
            f"use {', '.join(DAYS)}")
    matcher = Matcher(
        cameras=_strlist(raw.get("cameras")),
        min_severity=severity,
        alert_types=_strlist(raw.get("alert_types")),
        sources=_strlist(raw.get("sources")),
        zones=_strlist(raw.get("zones")),
        title_contains=str(raw.get("title_contains") or "").strip(),
        days=days,
        from_time=str(raw.get("from") or "").strip(),
        to_time=str(raw.get("to") or "").strip(),
    )
    # Validate the clock strings now, not at 3am.
    TimeWindow(matcher.days, matcher.from_time, matcher.to_time)
    return matcher


def parse_rules(raw: Any, known_channels: Iterable[str]) -> RuleSet:
    """The ordered list from config, with the channel names checked.

    A rule naming a channel that does not exist is not a warning, it is
    a rule that delivers nowhere — so it fails the load rather than
    running silently."""
    known = set(known_channels)
    rules: list[Rule] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            raise ValueError(f"rules[{index}]: expected a mapping")
        unknown = sorted(set(item) - {
            "name", "to", "channels", "match", "enabled",
            "ignore_quiet_hours", "group_wait_seconds", "catch_all"})
        if unknown:
            raise ValueError(
                f"rules[{index}]: unknown setting(s) {', '.join(unknown)}")
        name = str(item.get("name") or f"Rule {index + 1}").strip()
        channels = _strlist(item.get("to") if item.get("to") is not None
                            else item.get("channels"))
        missing = [c for c in channels if c not in known]
        if missing:
            raise ValueError(
                f"rules[{index}] ({name}): no such channel(s) "
                f"{', '.join(missing)} — defined channels are "
                f"{', '.join(sorted(known)) or '(none)'}")
        wait = item.get("group_wait_seconds")
        rules.append(Rule(
            name=name,
            channels=channels,
            matcher=parse_matcher(item.get("match")),
            enabled=bool(item.get("enabled", True)),
            ignore_quiet_hours=bool(item.get("ignore_quiet_hours", False)),
            group_wait_seconds=None if wait is None else float(wait),
            catch_all=bool(item.get("catch_all", False)),
        ))
    names = [r.name for r in rules]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        # Names key the backtest counts and the delivery log; duplicates
        # would silently merge two rules' numbers into one.
        raise ValueError(f"rules: duplicate name(s) {', '.join(dupes)}")
    return RuleSet(rules)


def parse_quiet_hours(raw: Any) -> QuietHours:
    raw = raw if isinstance(raw, dict) else {}
    unknown = sorted(set(raw) - {"enabled", "windows", "mode", "breakthrough"})
    if unknown:
        raise ValueError(f"quiet_hours: unknown setting(s) {', '.join(unknown)}")
    windows = []
    for index, item in enumerate(raw.get("windows") or []):
        if not isinstance(item, dict):
            raise ValueError(f"quiet_hours.windows[{index}]: expected a mapping")
        windows.append(TimeWindow(
            days=[str(d) for d in (item.get("days") or [])],
            from_time=str(item.get("from") or "").strip(),
            to_time=str(item.get("to") or "").strip()))
    breakthrough = str(raw.get("breakthrough", "critical") or "").lower().strip()
    if breakthrough and breakthrough not in SEVERITY_RANK:
        raise ValueError(
            f"quiet_hours.breakthrough must be one of "
            f"{', '.join(SEVERITY_RANK)} (or empty for none)")
    return QuietHours(
        enabled=bool(raw.get("enabled", False)),
        windows=windows,
        mode=str(raw.get("mode") or "hold").lower().strip(),
        breakthrough=breakthrough)


def _strlist(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]
