# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Footage-search example app — natural-language search over recorded
inference history, now on the ``opennvr-app-sdk``.

    $ python footage_search.py search "red truck at the dock yesterday"

    2 matches:
      [cam-dock] 2026-06-13 14:22:08  truck
        "a red truck parked near a loading dock"
        correlation_id=corr_8f1c… (use it to pull the recorded segment)
      [cam-dock] 2026-06-13 09:05:41  truck person
        "a red delivery truck with a person beside it"
        correlation_id=corr_2a90…

Two subcommands:

* ``index`` runs a daemon that subscribes to KAI-C's NATS inference
  broadcast surface and writes every searchable keyframe (object labels
  from a detector, scene captions from a BLIP-style captioner) into a
  local SQLite index. Run it alongside your detectors; it pays zero
  adapter cost (it rides the stream that's already flowing).

* ``search`` parses a natural-language query into a structured filter
  (object labels + descriptor keywords + time window + camera) and runs
  it against the index, printing matching keyframes newest-first. Each
  match carries the ``correlation_id`` that ties it to the exact
  recorded segment in OpenNVR.

What lives where after the migration
------------------------------------

The :class:`Indexer` is a :class:`~opennvr_app_sdk.Detector` (App SDK
spec §02): the SDK base owns the NATS connect / subscribe / drain
loop, per-message JSON decoding + exception isolation, and the §03
contract endpoints. Because the indexer also stores caption-only
events (no ``result.detections``), it hooks ``handle_event`` — the
whole-event stage above the base's detections walk — rather than
``on_detections``.

Deliberately app-side (the "don't force it" clause):

* ``store.py`` — the SQLite keyframe schema + FTS-ish search query,
  and ``keyframe_from_event`` (which result shapes are indexable is
  this app's business);
* ``query.py`` — the natural-language → structured-filter parsers
  (heuristic + optional Ollama);
* the two-subcommand CLI (``index`` / ``search``): the SDK's
  ``app(...)`` runner models single-loop daemons, so ``main`` keeps
  its own argparse and drives ``Indexer.run()`` itself for the
  ``index`` path.

Why this works without a special model: object *classes* come from the
detector's labels; *attributes* like "red" come from the captioner's
scene text. Searching across both is what lets "red truck" resolve. For
precise attribute detection, point the indexer at an open-vocabulary /
VLM adapter instead of (or alongside) BLIP — the index and search code
don't change.

Run::

    python footage_search.py index  --config config.yml
    python footage_search.py search --config config.yml "red truck yesterday"
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as _dt
import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from opennvr_app_sdk import Action, Alert, AppManifest, Detector, Param, StateView
from opennvr_app_sdk.config import load_yaml

from query import DEFAULT_LABELS, parse_heuristic, parse_with_ollama
from store import FootageStore, SearchResult, keyframe_from_event

logger = logging.getLogger("footage-search")


MANIFEST = AppManifest(
    id="footage-search",
    name="Footage Search",
    version="1.0.0",
    category="forensics",
    summary=(
        "Indexes inference events (labels + captions) into SQLite and "
        "answers natural-language footage queries like 'red truck at "
        "the dock yesterday'."
    ),
    requires_tasks=[],  # indexes whatever detector/captioner streams exist
    subscribes="opennvr.inference.>",
    params=[
        Param("db_path", str, default="footage_index.sqlite3"),
        Param("subject_pattern", str, default="opennvr.inference.>"),
        Param("extra_labels", list, default=[],
              description="Extra label vocabulary for the query parser."),
        Param("camera_aliases", dict, default={},
              description="word -> camera_id aliases ('dock' -> 'cam-dock')."),
        Param("result_limit", int, default=25),
        Param("retention_days", int, default=30,
              description="Delete indexed keyframes older than this. 0 keeps everything."),
        Param("coalesce_seconds", int, default=60,
              description="Merge caption-less repeats of the same label set on "
                          "a camera within this window into one row (Tier-0 "
                          "publishes per frame). 0 disables."),
    ],
    emits=[],  # writes an index; fires no alerts
    # Declarative live-state views — rendered generically by the catalog.
    state_schema=[
        StateView(name="rows_total", label="Indexed keyframes",
                  kind="metric", path="rows_total"),
        StateView(name="session", label="This session",
                  kind="metric", path="indexed_this_session"),
        StateView(name="recent", label="Recent searches",
                  kind="log", path="recent", limit=10,
                  description="Operator queries run from the search action."),
    ],
    # Operator actions (user-JWT-only through the server proxy): the UI
    # query path that replaces `docker compose exec … search "…"`.
    actions=[
        Action(
            "search", "Search footage",
            params=[
                Param("query", str, required=True,
                      description="Natural-language query, e.g. 'red truck at the dock yesterday'."),
                Param("limit", int, default=10,
                      description="Max results (1-200)."),
            ],
            description="Parse the query and search the footage index.",
        ),
    ],
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class OllamaConfig:
    enabled: bool = False
    url: str = "http://ollama:11434"
    model: str = "llama3.2"


@dataclass
class AppConfig:
    db_path: str
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    extra_labels: list[str]
    camera_aliases: dict[str, str]
    ollama: OllamaConfig
    result_limit: int
    # Days of index history to keep. The index grows with every
    # detection forever, and Tier-0 publishes continuously — so an
    # unbounded index is a slow disk leak on an active camera. 0
    # disables pruning (an operator who wants a permanent archive).
    retention_days: int = 30
    # Tier-0 publishes an event per analyzed frame with no correlation_id;
    # without coalescing a person sitting in frame is thousands of identical
    # rows and a search returns 25 consecutive frames instead of 25 distinct
    # episodes. Repeats of the same label set within this window advance the
    # existing row's timestamp instead of inserting. 0 disables.
    coalesce_seconds: int = 60

    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state AND the
    # POST /actions surface (the catalog's search form); ``opennvr_url``
    # triggers registry self-registration + live config delivery.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str) -> AppConfig:
    raw = load_yaml(path)

    db_path = str(raw.get("db_path") or "footage_index.sqlite3").strip()
    if not db_path:
        raise ValueError("config: 'db_path' must not be empty")

    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required (for `index`)")
    subject = str(raw.get("subject_pattern") or "opennvr.inference.>").strip()

    extra_labels = [str(s).lower() for s in (raw.get("extra_labels") or [])]

    aliases_raw = raw.get("camera_aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ValueError("config: 'camera_aliases' must be a mapping of word -> camera_id")
    camera_aliases = {str(k).lower(): str(v) for k, v in aliases_raw.items()}

    ollama_raw = raw.get("ollama") or {}
    ollama = OllamaConfig(
        enabled=bool(ollama_raw.get("enabled", False)),
        url=str(ollama_raw.get("url", "http://ollama:11434")),
        model=str(ollama_raw.get("model", "llama3.2")),
    )

    try:
        result_limit = int(raw.get("result_limit", 25))
    except (TypeError, ValueError) as exc:
        raise ValueError("config: 'result_limit' must be an integer") from exc
    if result_limit <= 0:
        raise ValueError("config: 'result_limit' must be > 0")

    contract_port_raw = raw.get("contract_port")
    return AppConfig(
        db_path=db_path,
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        extra_labels=extra_labels,
        camera_aliases=camera_aliases,
        ollama=ollama,
        result_limit=result_limit,
        retention_days=max(0, int(raw.get("retention_days", 30) or 0)),
        coalesce_seconds=max(0, int(raw.get("coalesce_seconds", 60) or 0)),
        contract_port=(
            int(contract_port_raw) if contract_port_raw is not None else None
        ),
        contract_bind_host=(
            str(raw["contract_bind_host"]) if raw.get("contract_bind_host") else None
        ),
        contract_host=(
            str(raw["contract_host"]) if raw.get("contract_host") else None
        ),
        opennvr_url=str(raw["opennvr_url"]) if raw.get("opennvr_url") else None,
        opennvr_token=(
            str(raw["opennvr_token"]) if raw.get("opennvr_token") else None
        ),
    )


# ── Indexer ────────────────────────────────────────────────────────


class Indexer(Detector):
    """Subscribes to NATS inference events (via the SDK's Detector
    loop) and writes searchable keyframes into the store.

    Hooks :meth:`handle_event` instead of ``on_detections`` because
    caption-only events (a BLIP result has ``result.caption`` and no
    detections list) are exactly as indexable as detection events —
    the base's detections walk would drop them.
    """

    manifest = MANIFEST

    def __init__(
        self,
        config: AppConfig,
        store: FootageStore,
        dispatcher: Any = None,
    ) -> None:
        # ``dispatcher`` is unused (this app fires no alerts); the
        # historical constructor is ``Indexer(config, store)``.
        self._store = store
        super().__init__(config, dispatcher)
        self._indexed = 0
        # Rolling feed of the most recent operator searches — powers the
        # "Recent searches" log on the app's dashboard.
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)

    def ingest(self, event: dict[str, Any]) -> bool:
        """Index one event. Returns True if a keyframe was stored."""
        kf = keyframe_from_event(event)
        if kf is None:
            return False
        self._store.add(kf)
        self._indexed += 1
        return True

    def handle_event(self, event: Any) -> list[Alert]:
        """Whole-event hook (decode + isolation live in the SDK's
        ``_handle_raw`` above this)."""
        self._contract_note_event()
        if isinstance(event, dict):
            self.ingest(event)
            if self._indexed and self._indexed % 100 == 0:
                logger.info("indexed %d keyframes", self._indexed)
        return []

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — session + total index counters."""
        return {
            "indexed_this_session": self._indexed,
            "rows_total": self._store.count(),
            "recent": list(self._recent),
        }

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """``search`` — the manifest-declared operator action, giving the
        catalog a UI query path (previously CLI-only via ``docker compose
        exec … search``). Runs on the contract server's thread, so it
        opens a FRESH read connection: the indexer's own sqlite
        connection belongs to the NATS loop thread and sqlite connections
        must not be used concurrently across threads."""
        if name != "search":
            raise KeyError(name)
        query = str(params.get("query") or "").strip()
        if not query:
            raise ValueError("'query' must be a non-empty string")
        raw_limit = params.get("limit")
        try:
            # `or 10` would silently turn an explicit 0 into the default
            # instead of rejecting it — only substitute when ABSENT.
            limit = 10 if raw_limit is None else int(raw_limit)
        except (TypeError, ValueError):
            raise ValueError("'limit' must be a whole number") from None
        if not 1 <= limit <= 200:
            raise ValueError("'limit' must be between 1 and 200")

        read_store = FootageStore(self.cfg.db_path)
        try:
            cfg = dataclasses.replace(self.cfg, result_limit=limit)
            results = run_search(cfg, read_store, query)
        finally:
            # One fresh connection PER ACTION — it must close with the
            # request or N searches leak N file descriptors (review H1).
            read_store.close()
        self._recent.append({
            "message": f"“{query}” — {len(results)} hit"
                       f"{'' if len(results) == 1 else 's'}",
            "time": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
                timespec="seconds"),
        })
        return {
            "query": query,
            "results": [
                {
                    "camera": r.camera_id,
                    "when": _dt.datetime.fromtimestamp(
                        r.ts, tz=_dt.timezone.utc
                    ).isoformat(timespec="seconds"),
                    "labels": " ".join(r.labels),
                    "caption": r.caption,
                }
                for r in results
            ],
        }

    def prune_now(self) -> int:
        """Apply the retention window once. Returns rows deleted (0 when
        retention is disabled). Separated from the timer so it is testable
        without a running loop."""
        days = int(getattr(self.cfg, "retention_days", 0) or 0)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86_400
        removed = self._store.prune(cutoff)
        if removed:
            logger.info("retention: pruned %d keyframes older than %d days",
                        removed, days)
        return removed

    async def _retention_loop(self, interval_s: float = 6 * 3600) -> None:
        """Prune on start and every 6h after. Best-effort: a failing prune
        (locked db, disk hiccup) must never take the indexer down."""
        while True:
            try:
                await asyncio.to_thread(self.prune_now)
            except Exception:
                logger.warning("retention pass failed", exc_info=True)
            await asyncio.sleep(interval_s)

    async def run(self, *, once: bool = False) -> None:
        logger.info(
            "footage-search indexer started: db=%s, subject=%r (%d rows already indexed)",
            self.cfg.db_path, self.cfg.subject_pattern, self._store.count(),
        )
        retention: asyncio.Task | None = None
        if not once and int(getattr(self.cfg, "retention_days", 0) or 0) > 0:
            retention = asyncio.create_task(self._retention_loop())
        try:
            await super().run(once=once)
        finally:
            if retention is not None:
                retention.cancel()
            logger.info("indexer stopped; %d keyframes this session", self._indexed)


# ── Search ─────────────────────────────────────────────────────────


def run_search(config: AppConfig, store: FootageStore, query: str) -> list[SearchResult]:
    """Parse the query and run it against the store."""
    now = _dt.datetime.now(_dt.timezone.utc)
    vocab = set(DEFAULT_LABELS) | set(config.extra_labels)
    if config.ollama.enabled:
        qf = parse_with_ollama(
            query, now=now, ollama_url=config.ollama.url, model=config.ollama.model,
            label_vocab=vocab, camera_aliases=config.camera_aliases,
        )
    else:
        qf = parse_heuristic(
            query, now=now, label_vocab=vocab, camera_aliases=config.camera_aliases,
        )
    logger.debug(
        "parsed query → labels=%s keywords=%s camera=%s since=%s until=%s",
        qf.labels, qf.keywords, qf.camera_id, qf.since, qf.until,
    )
    return store.search(
        labels=qf.labels, keywords=qf.keywords,
        since=qf.since, until=qf.until, camera_id=qf.camera_id,
        limit=config.result_limit,
    )


def format_results(results: list[SearchResult]) -> str:
    if not results:
        return "No matching footage found."
    lines = [f"{len(results)} match(es):"]
    for r in results:
        when = _dt.datetime.fromtimestamp(r.ts, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        labels = " ".join(r.labels) or "—"
        lines.append(f"  [{r.camera_id}] {when}  {labels}")
        if r.caption:
            lines.append(f"      \"{r.caption}\"")
        cid = r.correlation_id or "—"
        lines.append(
            f"      correlation_id={cid} (use it to pull the recorded segment)"
        )
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    # Two-subcommand CLI — kept app-side (the SDK's ``app(...)``
    # runner models single-loop daemons; ``search`` is a one-shot).
    parser = argparse.ArgumentParser(
        prog="footage-search",
        description="Index inference events and search recorded footage in natural language.",
    )
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index", help="Run the indexer daemon (subscribes to NATS).")
    p_search = sub.add_parser("search", help="Search the index.")
    p_search.add_argument("query", help="Natural-language query, e.g. 'red truck yesterday'.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    store = FootageStore(
        config.db_path, coalesce_seconds=float(config.coalesce_seconds)
    )
    try:
        if args.command == "search":
            results = run_search(config, store, args.query)
            print(format_results(results))
            return 0

        # index
        indexer = Indexer(config, store)
        loop = asyncio.new_event_loop()

        def _handle_signal(_signum, _frame):
            logger.info("signal received, stopping…")
            loop.call_soon_threadsafe(indexer.stop)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            loop.run_until_complete(indexer.run())
        finally:
            loop.close()
        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
