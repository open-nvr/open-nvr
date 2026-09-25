# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Footage-search example app — natural-language search over recorded
footage, answered from the platform's canonical event store.

    $ python footage_search.py --config config.yml search \
          "red truck at the dock yesterday"

    2 match(es):
      [Dock] 2026-06-13 14:22:08  truck
        "a red truck parked near a loading dock"
        event #8140 · plate KA01AB1234 · photo kept
      [Dock] 2026-06-13 09:05:41  truck
        "a red delivery truck with a person beside it"
        event #8017

What changed, and why it is most of this file
---------------------------------------------

This app used to keep its own database. It subscribed to KAI-C's NATS
inference broadcast and wrote every searchable keyframe into a local
SQLite index, then searched that. It worked, and it was the wrong
shape, for reasons that only became clear once the platform had a
canonical store of its own:

* The index was one row per analyzed FRAME. Tier-0 publishes
  continuously, so a person sitting still was thousands of identical
  rows — which is why the old store carried a 60-second coalescing hack
  and why a search could return 25 consecutive frames instead of 25
  distinct episodes. The canonical store is one row per VISIT.
* It had no camera scoping. It indexed whatever came past on the bus,
  and an operator's view of it was whatever the app chose to show.
  ``timeline.find`` is scoped server-side, by the same predicate as
  everything else in the platform.
* It was a SECOND retention policy on a SECOND store. Footage deleted
  from OpenNVR stayed described here for up to 30 more days, in a file
  nobody was auditing.
* It carried no evidence photo, no plate, and none of the claims that
  enrichment skills make about a visit — because none of that exists on
  the bus at the moment a frame is analyzed. It is all on the visit.

So the index is gone, and ``store.py`` with it, along with the indexer
daemon, the NATS subscription, the retention loop and the coalescing
window. What remains is the part that was always this app's own: the
natural-language parser, pointed at core.

``query.py`` stays app-side deliberately. The app-facing search route
does no sentence parsing — an app has usually already decided what it
is looking for, and two parsers disagreeing about one query is a bug
that is very hard to see. Here a HUMAN typed the sentence, so the
parsing belongs to whoever took the human's input, which is this app.

Run::

    python footage_search.py --config config.yml search "red truck yesterday"
    python footage_search.py --config config.yml serve
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import Action, AppManifest, ContractApp, Param, StateView
from opennvr_app_sdk.client import OpenNVR
from opennvr_app_sdk.config import load_yaml

from query import DEFAULT_LABELS, parse_heuristic, parse_with_ollama

logger = logging.getLogger("footage-search")


MANIFEST = AppManifest(
    id="footage-search",
    name="Footage Search",
    version="2.0.0",
    category="forensics",
    summary=(
        "Answers natural-language footage queries like 'red truck at "
        "the dock yesterday' against the platform's event store."
    ),
    requires_tasks=[],   # reads remembered visits; drives no inference itself
    # …but the visits it searches are worth more described and embedded.
    # Picking a camera for Footage Search puts these in the camera's skill
    # set, which is what turns the platform's caption and embedding
    # enrichers on for that camera. Soft: a box with neither still
    # searches by class, camera, time and plate.
    enrich_tasks=["image_captioning", "embed"],
    subscribes=None,     # no stream at all — see ContractApp
    params=[
        Param("extra_labels", list, default=[],
              description="Extra label vocabulary for the query parser."),
        Param("camera_aliases", dict, default={},
              description="word -> camera name or id ('dock' -> 'Dock')."),
        Param("result_limit", int, default=25),
    ],
    emits=[],            # answers questions; fires no alerts
    state_schema=[
        StateView(name="searches", label="Searches this session",
                  kind="metric", path="searches"),
        StateView(name="store", label="Event store",
                  kind="text", path="store_status",
                  description="Whether core answered the last query."),
        StateView(name="recent", label="Recent searches",
                  kind="log", path="recent", limit=10,
                  description="Operator queries run from the search action."),
    ],
    actions=[
        Action(
            "search", "Search footage",
            params=[
                Param("query", str, required=True,
                      description="Natural-language query, e.g. 'red truck at the dock yesterday'."),
                Param("limit", int, default=10,
                      description="Max results (1-200)."),
            ],
            description="Parse the query and search recorded footage.",
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
    extra_labels: list[str] = field(default_factory=list)
    camera_aliases: dict[str, str] = field(default_factory=dict)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    result_limit: int = 25

    # App contract (spec §03). ``contract_port`` serves /health
    # /manifest /state AND POST /actions (the catalog's search form).
    # ``opennvr_url`` is BOTH the registry this app self-registers with
    # and the store it searches — so unlike every other example app it
    # is not optional here: there is nothing to search without it.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str) -> AppConfig:
    raw = load_yaml(path)

    extra_labels = [str(s).lower() for s in (raw.get("extra_labels") or [])]

    aliases_raw = raw.get("camera_aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ValueError(
            "config: 'camera_aliases' must be a mapping of word -> camera")
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

    opennvr_url = str(raw["opennvr_url"]).strip() if raw.get("opennvr_url") else ""
    if not opennvr_url:
        raise ValueError(
            "config: 'opennvr_url' is required — this app searches the "
            "OpenNVR event store and has no index of its own")

    contract_port_raw = raw.get("contract_port")
    return AppConfig(
        extra_labels=extra_labels,
        camera_aliases=camera_aliases,
        ollama=ollama,
        result_limit=result_limit,
        contract_port=(
            int(contract_port_raw) if contract_port_raw is not None else None
        ),
        contract_bind_host=(
            str(raw["contract_bind_host"]) if raw.get("contract_bind_host") else None
        ),
        contract_host=(
            str(raw["contract_host"]) if raw.get("contract_host") else None
        ),
        opennvr_url=opennvr_url,
        opennvr_token=(
            str(raw["opennvr_token"]) if raw.get("opennvr_token") else None
        ),
    )


# ── Results ────────────────────────────────────────────────────────


@dataclass
class Hit:
    """One matching visit, flattened for display.

    Deliberately not the route's dict. What this app shows is a stable
    surface for its CLI and its action, and a new key appearing on the
    route should not silently change what an operator reads.
    """

    event_id: int
    camera_id: int | None
    camera_name: str | None
    when: str
    labels: list[str]
    caption: str
    plate: str | None
    has_evidence: bool

    @classmethod
    def from_result(cls, row: dict[str, Any]) -> "Hit":
        claims = row.get("claims") or []
        labels = [str(row["label"])] if row.get("label") else []
        labels += [str(c.get("value")) for c in claims
                   if c.get("kind") == "colour" and c.get("value")]
        return cls(
            event_id=int(row.get("id") or 0),
            camera_id=row.get("camera_id"),
            camera_name=row.get("camera_name"),
            when=str(row.get("started_at") or ""),
            labels=labels,
            caption=str(row.get("caption") or ""),
            plate=row.get("plate_text") or None,
            has_evidence=bool(row.get("has_evidence")),
        )


class StoreUnreachable(RuntimeError):
    """Core did not answer.

    Its own exception type so that it cannot be collapsed into "nothing
    matched" by accident. Telling an operator that no red truck came
    past, when the truth is that nobody was able to look, is the worst
    answer this app can give.
    """


class CameraNotHeld(RuntimeError):
    """The query named a camera this app has not been given.

    Also not "nothing matched": the operator asked about the loading
    dock and the honest reply is that this app cannot see the loading
    dock, not that the dock was quiet.
    """


# ── Search ─────────────────────────────────────────────────────────


def run_search(config: AppConfig, client: OpenNVR, query: str,
               *, limit: int | None = None) -> list[Hit]:
    """Parse the query and run it against the canonical store."""
    now = _dt.datetime.now(_dt.timezone.utc)
    vocab = set(DEFAULT_LABELS) | set(config.extra_labels)
    if config.ollama.enabled:
        qf = parse_with_ollama(
            query, now=now, ollama_url=config.ollama.url,
            model=config.ollama.model, label_vocab=vocab,
            camera_aliases=config.camera_aliases,
        )
    else:
        qf = parse_heuristic(
            query, now=now, label_vocab=vocab,
            camera_aliases=config.camera_aliases,
        )
    logger.debug(
        "parsed query → labels=%s keywords=%s camera=%s since=%s until=%s",
        qf.labels, qf.keywords, qf.camera_id, qf.since, qf.until,
    )

    cameras: list[int] | None = None
    if qf.camera_id:
        cameras = resolve_camera(client, qf.camera_id)
        if not cameras:
            raise CameraNotHeld(
                f"'{qf.camera_id}' is not a camera this app has been given "
                "access to")

    answer = client.timeline.find(
        " ".join(qf.keywords),
        label=qf.labels or None,
        camera=cameras,
        start=_ts(qf.since),
        end=_ts(qf.until),
        limit=limit or config.result_limit,
    )
    if answer is None:
        raise StoreUnreachable(
            "the OpenNVR event store did not answer, so it is not known "
            "whether anything matched")
    return [Hit.from_result(r) for r in (answer.get("results") or [])]


def _ts(value: float | None) -> _dt.datetime | None:
    return (_dt.datetime.fromtimestamp(value, _dt.timezone.utc)
            if value is not None else None)


def resolve_camera(client: OpenNVR, alias: str) -> list[int]:
    """An alias from the query parser → camera ids this app holds.

    The parser yields whatever the operator configured (``"dock"`` ->
    ``"Dock"``), which may be a name, an id, or a ``cam3`` handle. The
    handle is compared as the roster reports it rather than rebuilt from
    the id, so a change to how handles are formed cannot leave this
    matching a shape core no longer uses.

    Matching happens against the app's OWN roster, so an alias can never
    reach a camera the app was not given. The server would refuse it in
    any case; failing here just says so in words an operator can act on.
    """
    wanted = alias.strip().lower()
    return [c.id for c in client.cameras()
            if (c.name or "").strip().lower() == wanted
            or str(c.id) == wanted
            or (c.handle or "").strip().lower() == wanted
            or (c.handle or "").strip().lower().replace("cam", "cam-", 1) == wanted]


def format_results(results: list[Hit]) -> str:
    if not results:
        return "No matching footage found."
    lines = [f"{len(results)} match(es):"]
    for r in results:
        where = r.camera_name or (f"camera {r.camera_id}" if r.camera_id
                                  else "unknown camera")
        when = r.when.replace("T", " ")[:19] or "—"
        labels = " ".join(r.labels) or "—"
        lines.append(f"  [{where}] {when}  {labels}")
        if r.caption:
            lines.append(f'      "{r.caption}"')
        detail = [f"event #{r.event_id}"]
        if r.plate:
            detail.append(f"plate {r.plate}")
        if r.has_evidence:
            detail.append("photo kept")
        lines.append("      " + " · ".join(detail))
    return "\n".join(lines)


# ── App ────────────────────────────────────────────────────────────


class FootageSearch(ContractApp):
    """The operator surface: a manifest, live state, and one action.

    A :class:`~opennvr_app_sdk.ContractApp` rather than a ``Detector``
    because there is no longer anything to subscribe to. Everything it
    answers with, it reads from core at the moment it is asked.
    """

    manifest = MANIFEST

    def __init__(self, config: AppConfig, client: OpenNVR | None = None) -> None:
        self._client = client
        self._searches = 0
        self._store_ok: bool | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)
        super().__init__(config)

    @property
    def client(self) -> OpenNVR:
        """Built on first use, not at construction: an app that cannot
        reach core should still serve ``/health`` and say why."""
        if self._client is None:
            self._client = OpenNVR(self.cfg.opennvr_url,
                                   token=self.cfg.opennvr_token)
        return self._client

    def not_ready_reason(self) -> str | None:
        """Up, but unable to do the job. Shown as-is in the App Catalog.

        ``None`` while nothing has been asked yet: an app that has not
        been queried is not broken, and saying so would put a red mark
        on every freshly started deployment.
        """
        if self._store_ok is False:
            return ("The OpenNVR event store did not answer the last "
                    "search, so footage cannot be searched right now.")
        return None

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "searches": self._searches,
            "store_status": {None: "not queried yet", True: "answering",
                             False: "not answering"}[self._store_ok],
            "recent": list(self._recent),
        }

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
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

        try:
            results = run_search(self.cfg, self.client, query, limit=limit)
        except StoreUnreachable:
            self._store_ok = False
            self._note("search: store did not answer")
            raise
        self._store_ok = True
        self._searches += 1

        # Hit count only, never the words: state is shown to every
        # operator, and a query may have come from someone's voice
        # assistant.
        self._note(f"search: {len(results)} hit"
                   f"{'' if len(results) == 1 else 's'}")
        return {
            "query": query,
            "results": [
                {
                    "event_id": r.event_id,
                    "camera": r.camera_name or r.camera_id,
                    "when": r.when,
                    "labels": " ".join(r.labels),
                    "caption": r.caption,
                    "plate": r.plate,
                    "has_evidence": r.has_evidence,
                }
                for r in results
            ],
        }

    def _note(self, message: str) -> None:
        self._recent.append({
            "message": message,
            "time": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
                timespec="seconds"),
        })


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="footage-search",
        description="Search recorded footage in natural language.",
    )
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve",
                   help="Serve the app contract (manifest, state, actions).")
    p_search = sub.add_parser("search", help="Run one search and print it.")
    p_search.add_argument(
        "query", help="Natural-language query, e.g. 'red truck yesterday'.")

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

    if args.command == "search":
        client = OpenNVR(config.opennvr_url, token=config.opennvr_token)
        try:
            results = run_search(config, client, args.query)
        except (StoreUnreachable, CameraNotHeld) as exc:
            # Exit 3, not 0 with "no matches". A script reading this
            # output has to be able to tell an empty result from an
            # unanswered question.
            print(f"search unavailable: {exc}", file=sys.stderr)
            return 3
        print(format_results(results))
        return 0

    app = FootageSearch(config)
    loop = asyncio.new_event_loop()

    def _handle_signal(_signum, _frame):
        logger.info("signal received, stopping…")
        loop.call_soon_threadsafe(app.stop)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        loop.run_until_complete(app.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
