"""
Copyright (c) 2026 OpenNVR
SPDX-License-Identifier: AGPL-3.0-or-later

Search metrics — is it fast, is it finding the thing, and can it see
enough of the footage to have a chance.

Prometheus exposition text, built in-process with no new dependency, in
the same dependency-free style the detect-pipeline exports and
``tier0_metrics`` already parses. Scraped from
``GET /api/v1/search/metrics`` with the site key.

Three families, because search fails in three unrelated ways and a
single number hides all of them.

**Latency, by the SHAPE of the query.** A structured search (class,
camera, window) is an index seek; a ranked text search over a common
word has to score the whole match set before it can name the best 24.
Those differ by two orders of magnitude, so one histogram over all
searches would have a bimodal distribution and a meaningless p95. The
count query is timed separately from the page for the same reason: it is
the one cost this API added over the old app store, and separating it is
how an operator decides whether the total is worth keeping exact.

**Whether the answer was any good.** There is no ground truth here —
nobody labels footage to score a search engine — so accuracy is measured
by what the operator did next, which is the only honest signal
available:

* ``ignored`` words per query: what the parser did not understand. The
  one number that says the sentence was read badly, and it needs no
  human to say so.
* refinements: a search arriving with the chips edited means the first
  parse was wrong, at least in the operator's view.
* zero-result rate: the top-line "search does not work" number.
* the RANK of the result actually opened. If people open the first three
  results, ranking works; if they open the eleventh, it does not; if they
  open nothing, neither does anything else. This is as close to precision
  as a system without labels can get.

**What search can even see.** Recall is capped by enrichment: colours
cannot be searched at a site with no captioner, and no amount of query
tuning changes that. The coverage gauges say what fraction of retained
visits carry text and which descriptor kinds exist at all, so a recall
complaint can be answered with the reason rather than a guess. They also
report what KAI-C makes available right now — skills registered, healthy,
and usable — because that is the input the coverage is produced from.

Cardinality is deliberately bounded: label values are drawn from fixed
vocabularies (query shapes, outcomes, descriptor kinds, journey methods)
or from the installed adapter set. Nothing here is labelled by camera,
by user, or by query text.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

# The buckets the detect-pipeline uses, so latency panels can be built
# the same way across components.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
#: Result-set sizes: the interesting boundaries are "none", "a handful",
#: "a page", and "too many to look at".
COUNT_BUCKETS = (0, 1, 5, 25, 100, 1000, 10000)
#: Ranks: did they open the top of the list, the first page, or did they
#: have to go hunting.
RANK_BUCKETS = (1, 3, 5, 10, 25, 100)


class _Metric:
    __slots__ = ("_lock", "_values", "help", "labelnames", "name")

    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help
        self.labelnames = labelnames
        self._values: dict[tuple[str, ...], Any] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        if not self.labelnames:
            return ()
        labels = labels or {}
        return tuple(str(labels.get(n, "")) for n in self.labelnames)

    def _label_str(self, key: tuple[str, ...]) -> str:
        if not key:
            return ""
        inner = ",".join(
            f'{n}="{_escape(v)}"' for n, v in zip(self.labelnames, key, strict=False)
        )
        return "{" + inner + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Counter(_Metric):
    def inc(self, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        out += [f"{self.name}{self._label_str(k)} {_num(v)}" for k, v in items]
        return out


class Gauge(_Metric):
    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def clear(self) -> None:
        """Drop every series — for a gauge family rebuilt from a snapshot,
        so a descriptor kind that no longer exists stops being reported."""
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        out += [f"{self.name}{self._label_str(k)} {_num(v)}" for k, v in items]
        return out


class Histogram(_Metric):
    __slots__ = ("buckets",)

    def __init__(self, name: str, help: str, buckets: Iterable[float],
                 labelnames: tuple[str, ...] = ()) -> None:
        super().__init__(name, help, labelnames)
        self.buckets = tuple(sorted(buckets))

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(labels)
        with self._lock:
            st = self._values.get(key)
            if st is None:
                st = {"counts": [0] * len(self.buckets), "sum": 0.0, "n": 0}
                self._values[key] = st
            st["sum"] += float(value)
            st["n"] += 1
            for i, b in enumerate(self.buckets):
                if value <= b:
                    st["counts"][i] += 1

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
            snapshot = [(k, list(v["counts"]), v["sum"], v["n"]) for k, v in items]
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, counts, total, n in snapshot:
            base = dict(zip(self.labelnames, key, strict=False))
            # counts are cumulative by construction: an observation
            # increments every bucket it fits in, not just the first.
            for b, c in zip(self.buckets, counts, strict=False):
                labels = {**base, "le": _num(b)}
                inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
                out.append(f"{self.name}_bucket{{{inner}}} {c}")
            inf = {**base, "le": "+Inf"}
            inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in inf.items())
            out.append(f"{self.name}_bucket{{{inner}}} {n}")
            out.append(f"{self.name}_sum{self._label_str(key)} {_num(total)}")
            out.append(f"{self.name}_count{self._label_str(key)} {n}")
        return out


def _num(v: float) -> str:
    """Prometheus wants a plain number; keep integers integral so a
    counter does not read as 17.0."""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))


# ── latency and load ─────────────────────────────────────────────────
SEARCH_SECONDS = Histogram(
    "opennvr_search_seconds",
    "Time to fetch one page of search results, by query shape.",
    LATENCY_BUCKETS, ("shape",),
)
COUNT_SECONDS = Histogram(
    "opennvr_search_count_seconds",
    "Time to compute the exact result total, by query shape. Separate "
    "from the page because it is the one cost this API adds over a "
    "bare page query, and the first thing to make approximate if it hurts.",
    LATENCY_BUCKETS, ("shape",),
)
JOURNEY_SECONDS = Histogram(
    "opennvr_search_journey_seconds",
    "Time to follow one object across cameras.",
    LATENCY_BUCKETS, ("method",),
)

# ── did it answer ────────────────────────────────────────────────────
QUERIES = Counter(
    "opennvr_search_queries_total",
    "Searches served, by query shape and whether anything matched.",
    ("shape", "outcome"),
)
RESULT_COUNT = Histogram(
    "opennvr_search_results",
    "How many visits matched (the total, not the page).",
    COUNT_BUCKETS, ("shape",),
)
QUERY_WORDS = Counter(
    "opennvr_search_query_words_total",
    "Words in natural-language queries, by whether the parser made "
    "something of them. The ignored share is how often the sentence was "
    "read badly, with nobody having to say so.",
    ("state",),
)
IGNORED_WORDS = Histogram(
    "opennvr_search_ignored_words",
    "Words per query the parser could not use.",
    (0, 1, 2, 3, 5, 8), (),
)
REFINEMENTS = Counter(
    "opennvr_search_refinements_total",
    "Searches arriving with the interpretation edited: 'corrected' still "
    "parses the sentence, 'explicit' drives entirely from chips. Both mean "
    "the first answer was not what the operator wanted.",
    ("kind",),
)
OPENED = Counter(
    "opennvr_search_opened_total",
    "Search results opened in the player — the closest thing to a "
    "relevance judgement this system gets without labelled footage.",
    (),
)
OPEN_RANK = Histogram(
    "opennvr_search_open_rank",
    "1-based position of the opened result. Low is ranking working.",
    RANK_BUCKETS, (),
)

# ── what search can see ──────────────────────────────────────────────
VISITS = Gauge(
    "opennvr_search_visits",
    "Visits in the event store — the searchable population.",
)
ENRICHED = Gauge(
    "opennvr_search_enriched_visits",
    "Visits carrying searchable text. The gap to opennvr_search_visits is "
    "the recall ceiling for any word-based query.",
)
DESCRIBED = Gauge(
    "opennvr_search_described_visits",
    "Visits carrying at least one claim of this kind. A filter for a kind "
    "at zero here can never match, however the query is worded.",
    ("kind",),
)
COVERAGE_AGE = Gauge(
    "opennvr_search_coverage_age_seconds",
    "Age of the coverage snapshot above (it is sampled, not live).",
)

# ── the skills behind it (KAI-C) ─────────────────────────────────────
SKILLS = Gauge(
    "opennvr_search_skills",
    "Skills KAI-C reports: registered, and of those healthy. Enrichment "
    "can only produce what is in the healthy set, so a drop here becomes "
    "a coverage drop later and is worth alerting on before it does.",
    ("state",),
)
REGISTRY_UNREACHABLE = Counter(
    "opennvr_search_registry_unreachable_total",
    "Times KAI-C could not be asked what this deployment can do. Not an "
    "error for the caller — enrichment is additive — but the reason "
    "coverage stops growing.",
    (),
)
DESCRIPTORS_WRITTEN = Counter(
    "opennvr_search_descriptors_written_total",
    "Claims written, by kind, the KAI-C task/adapter that produced them, "
    "and how the claim's SUBJECT was bound (RFC-0003). Attribution in "
    "two directions: which skill is contributing, and whether what it "
    "contributed was attached to a known visit or a guessed one. A "
    "`face_id` written against a `nearest` binding is a name on a "
    "guessed subject, and this is where that becomes countable.",
    ("kind", "task", "adapter", "binding"),
)
DESCRIPTOR_CONFLICTS = Counter(
    "opennvr_search_descriptor_conflicts_total",
    "Two different tasks claiming different values of the same kind for "
    "one visit. Kept rather than resolved — a rising conflict rate is a "
    "skill going wrong, and is invisible if disagreement is overwritten.",
    ("kind",),
)

# ── how a claim's subject was decided (RFC-0003) ─────────────────────
#
# `binding` records whether a claim's subject was known, looked up, or
# guessed. Recording it on the row was half the job; the other half is
# being able to see the MIX change without reading rows.
#
# The tolerance that admits a `nearest` binding is a tuning constant
# (timeline_service.DEFAULT_BIND_TOLERANCE_S, 5s). Set too wide it
# attaches names to the wrong visitor, and the way anyone finds out
# today is that somebody notices a wrong name — which is late, rare, and
# depends on an operator knowing the visitor. These counters make it a
# number instead: a rising `nearest` share means frames are arriving
# further from the visits they belong to, and it says so long before a
# name lands on the wrong person.
BINDINGS = Counter(
    "opennvr_visit_bindings_total",
    "Attempts to bind a frame to a visit, by outcome. `window` is a "
    "lookup (a visit's own span contained the instant), `nearest` is an "
    "admitted guess, `ambiguous` is a deliberate refusal, `none` found "
    "nothing. A rising `nearest` share is the bind tolerance being asked "
    "to do too much; a rising `ambiguous` share is cameras seeing more "
    "than one thing at once, which is information, not a fault.",
    ("outcome",),
)
BIND_GAP = Histogram(
    "opennvr_visit_bind_gap_seconds",
    "For `nearest` bindings only: how far the instant fell outside the "
    "visit it was attached to. Clustering near the tolerance means the "
    "next frame along will fall outside it and bind nothing — or, worse, "
    "reach the visit after.",
    (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0), (),
)

# ── journeys ─────────────────────────────────────────────────────────
JOURNEYS = Counter(
    "opennvr_search_journeys_total",
    "Cross-camera routes answered, by method. The MIX is the honesty "
    "signal: mostly 'identity' means plates and faces are carrying it; "
    "mostly 'time-only' means the deployment is guessing and the answers "
    "should be read that way.",
    ("method",),
)
JOURNEY_HOPS = Histogram(
    "opennvr_search_journey_hops",
    "Hops per route.",
    (0, 1, 2, 3, 5, 8, 12), (),
)
TRANSITION_EDGES = Gauge(
    "opennvr_camera_transition_edges",
    "Learned camera-to-camera edges. Zero means no topology yet, so "
    "journeys for objects without an exact identity cannot be narrowed.",
)
TRANSITION_SAMPLES = Gauge(
    "opennvr_camera_transition_samples",
    "Confirmed trips those edges are built from. An edge with one sample "
    "is a coincidence; the graph is trustworthy in proportion to this.",
)

_ALL: tuple[_Metric, ...] = (
    SEARCH_SECONDS, COUNT_SECONDS, JOURNEY_SECONDS,
    QUERIES, RESULT_COUNT, QUERY_WORDS, IGNORED_WORDS, REFINEMENTS,
    OPENED, OPEN_RANK,
    VISITS, ENRICHED, DESCRIBED, COVERAGE_AGE,
    SKILLS, REGISTRY_UNREACHABLE, DESCRIPTORS_WRITTEN, DESCRIPTOR_CONFLICTS,
    JOURNEYS, JOURNEY_HOPS, TRANSITION_EDGES, TRANSITION_SAMPLES,
    BINDINGS, BIND_GAP,
)


def query_shape(*, labels, camera_ids, text: str, attrs, plate: str = "",
                from_=None, to=None) -> str:
    """The cost class of a query, which is what latency has to be split by.

    ``text`` is the expensive axis (a ranked match set), structure is the
    cheap one (index seeks); a query with both is usually cheap because
    the structure collapses the match set before ranking. ``plate`` rides
    with structure — it is an equality on an indexed column.
    """
    structured = bool(labels or camera_ids or plate or from_ or to)
    if text and structured:
        return "text+structured"
    if text:
        return "text"
    if attrs and not structured:
        return "attr"
    if structured or attrs:
        return "structured"
    return "unfiltered"


class Timer:
    """``with Timer() as t: ...`` then ``t.seconds``."""

    __slots__ = ("_start", "seconds")

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.seconds = time.perf_counter() - self._start


class _Coverage:
    """The coverage gauges, sampled rather than computed per scrape.

    Counting visits and their enrichment is three aggregate queries. They
    are cheap next to the retention window they cover and ruinous at
    scrape frequency, so they are refreshed at most every ``ttl`` seconds
    and the age of the sample is exported beside them — a number that is
    two minutes old is fine as long as it says so.
    """

    def __init__(self, ttl: float = 120.0) -> None:
        self._ttl = ttl
        self._at = 0.0
        self._lock = threading.Lock()

    def maybe_refresh(self, db) -> None:
        with self._lock:
            if self._at and (time.time() - self._at) < self._ttl:
                COVERAGE_AGE.set(time.time() - self._at)
                return
            self._at = time.time()
        try:
            _sample_coverage(db)
            COVERAGE_AGE.set(0.0)
        except Exception:  # pragma: no cover - a metrics scrape never fails a page
            from core.logging_config import main_logger

            main_logger.warning("search coverage sample failed", exc_info=True)


COVERAGE = _Coverage()


def _sample_coverage(db) -> None:
    """Population, text coverage, and per-kind claim coverage."""
    from sqlalchemy import distinct, func

    from models import CameraTransition, EventText, TimelineEvent, VisitDescriptor

    VISITS.set(db.query(func.count(TimelineEvent.id)).scalar() or 0)
    ENRICHED.set(db.query(func.count(EventText.event_id)).scalar() or 0)

    # Rebuilt wholesale: a kind that stopped being produced should stop
    # being reported, not sit at its last value forever.
    DESCRIBED.clear()
    rows = (
        db.query(VisitDescriptor.kind, func.count(distinct(VisitDescriptor.event_id)))
        .group_by(VisitDescriptor.kind)
        .all()
    )
    for kind, n in rows:
        DESCRIBED.set(n or 0, {"kind": str(kind)})

    TRANSITION_EDGES.set(db.query(func.count(CameraTransition.id)).scalar() or 0)
    TRANSITION_SAMPLES.set(db.query(func.coalesce(func.sum(CameraTransition.samples), 0)).scalar() or 0)


def render() -> str:
    """The whole exposition, in a stable order."""
    lines: list[str] = []
    for m in _ALL:
        lines.extend(m.render())
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    """Drop every series. Tests only — metrics are process-lifetime."""
    for m in _ALL:
        with m._lock:
            m._values.clear()
    COVERAGE._at = 0.0
