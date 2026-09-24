#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the app-facing store costs, per query, at realistic row counts.

The consolidation moved footage search from an app's private SQLite
index to the platform's event store. That swapped an in-process file
read for an HTTP call, which sounds like a straight latency loss — and
the camera-agent asks this question mid-conversation, where a person
can feel it.

Two things make that reasoning incomplete, and this measures both:

* the hop is added, but the DATASET SHRANK. The old index was one row
  per analyzed FRAME; the store is one row per VISIT. On a camera at
  2 fps that is a factor of thousands.
* the query is scoped and indexed server-side, which the flat index
  was not.

Numbers from this script are an ORDER OF MAGNITUDE, not a promise:
SQLite here, Postgres in production, and a container's CPU is not a
mini-PC's. Run it on the box you care about.

    python bench/app_search_latency.py --visits 50000 --runs 50
"""
from __future__ import annotations

import argparse
import os
import secrets
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_bench.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (Camera, EventText, Role, TimelineEvent,  # noqa: E402
                    User, VisitDescriptor)
from routers import app_platform  # noqa: E402

CAPTIONS = [
    "a red truck at the loading dock", "a white van by the gate",
    "a person in a yellow jacket", "a blue car leaving the yard",
    "two people walking past the shutter", "a motorcycle at the barrier",
]


def seed(db, *, visits: int, cameras: int) -> None:
    db.add(Role(id=1, name="admin", description="bench"))
    db.commit()
    db.add(User(id=1, username="bench", email="b@x.test",
                hashed_password="x", is_active=True, role_id=1))
    db.commit()
    for cid in range(1, cameras + 1):
        db.add(Camera(id=cid, name=f"Camera {cid}", ip_address=f"10.0.0.{cid}",
                      rtsp_url=f"rtsp://x/{cid}", owner_id=1))
    db.commit()

    now = datetime.now(UTC)
    rows, texts, claims = [], [], []
    for i in range(visits):
        cam = (i % cameras) + 1
        started = now - timedelta(seconds=i * 20)
        rows.append(TimelineEvent(
            id=i + 1, camera_id=cam, label="truck" if i % 3 else "person",
            event_type="visit", source="tier0", started_at=started,
            ended_at=started + timedelta(seconds=25),
            plate_text=f"KA{i % 100:02d}AB{i % 10000:04d}" if i % 5 == 0 else None))
        texts.append(EventText(event_id=i + 1, caption=CAPTIONS[i % len(CAPTIONS)],
                               attributes=CAPTIONS[i % len(CAPTIONS)],
                               source="bench"))
        if i % 4 == 0:
            claims.append(VisitDescriptor(
                event_id=i + 1, kind="colour", value="red",
                confidence=0.8, source_task="vqa", binding="direct"))
    db.bulk_save_objects(rows)
    db.bulk_save_objects(texts)
    db.bulk_save_objects(claims)
    db.commit()


def percentiles(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {
        "p50": statistics.median(s),
        "p95": s[min(len(s) - 1, int(len(s) * 0.95))],
        "max": s[-1],
    }


def time_it(fn, runs: int) -> dict[str, float]:
    fn()  # warm
    out = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000.0)
    return percentiles(out)


def concurrent(cases, *, readers: int, runs: int) -> None:
    """The same queries again, with N callers asking at once.

    THE NUMBER THAT WAS MISSING. Everything above times one caller
    against an idle store, which is the least interesting condition the
    store is ever in. Consolidating the apps onto the platform moved
    every app's reads onto ONE database — a dozen installed apps, the
    operator's Search page and the agent all now arrive at the same
    connection pool — and nothing measured what that costs.

    Read it as a RATIO, not as absolute milliseconds. Threads here share
    one in-process SQLite handle, which serialises more aggressively
    than Postgres will; that makes this pessimistic, and pessimistic in
    the useful direction. What transfers is the SHAPE: a query whose p50
    is flat from 1 to 8 readers is not contending, and one whose p50
    tracks the reader count is queueing behind something.

    A flat query does not need a cache. That is the point of measuring
    before adding one — a cache in front of a query that was never
    contended is a second source of truth bought for nothing.
    """
    import threading

    # THE CONTROL, and the benchmark is dishonest without it.
    #
    # This harness runs SQLite through a StaticPool — ONE connection,
    # shared. Everything serialises on it by construction, so every
    # query will show a ratio near the reader count whether or not it
    # actually contends, and a reader could take that for a finding. It
    # is not one; it is a property of the fixture.
    #
    # So the control goes first: a primary-key fetch, the cheapest read
    # the store can serve. If IT scales with the reader count too, the
    # harness is the bottleneck and every ratio below says nothing about
    # the query. Only ratios that exceed the control's mean anything.
    print(f"\n--- {readers} concurrent readers ---")
    print(f"{'query':<40} {'p50':>9} {'p95':>9} {'vs 1':>8}")
    print("-" * 70)
    control = _ratio(_control_case[0][1], readers=readers, runs=runs)
    print(f"{'(control) primary-key fetch':<40} {control[1]:>7.1f}ms "
          f"{control[2]:>7.1f}ms {control[0]:>7.1f}x")
    for name, fn in cases:
        ratio, p50, p95 = _ratio(fn, readers=readers, runs=runs)
        print(f"{name:<40} {p50:>7.1f}ms {p95:>7.1f}ms {ratio:>7.1f}x")

    if control[0] > readers * 0.6:
        print(f"\n  READ NOTHING INTO THE RATIOS ABOVE. The control also "
              f"scaled {control[0]:.1f}x,\n  so this harness serialises "
              f"every read — one shared SQLite connection —\n  and the "
              f"numbers describe the fixture, not the queries. Point this "
              f"at\n  Postgres (DATABASE_URL) to measure contention for "
              f"real.")
    else:
        print(f"\n  The control scaled {control[0]:.1f}x, so the harness "
              f"is not the limit.\n  A query near that is not contending, "
              f"and a cache in front of it would\n  buy nothing. A query "
              f"well above it is queueing — that is where a cache,\n  or "
              f"an index, is worth adding.")


def _ratio(fn, *, readers: int, runs: int) -> tuple[float, float, float]:
    """(concurrent p50 / single p50, concurrent p50, concurrent p95)."""
    import threading

    single = time_it(fn, max(5, runs // 5))["p50"]
    samples: list[float] = []
    lock = threading.Lock()

    def work():
        mine = []
        for _ in range(max(1, runs // readers)):
            t = time.perf_counter()
            fn()
            mine.append((time.perf_counter() - t) * 1000.0)
        with lock:
            samples.extend(mine)

    threads = [threading.Thread(target=work) for _ in range(readers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    p = percentiles(samples)
    return (p["p50"] / single if single else float("nan"), p["p50"], p["p95"])


#: Filled in by main() — the control query, as a one-item list so the
#: closure above can reach it without a global rebind.
_control_case: list[tuple[str, Any]] = []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--visits", type=int, default=50_000)
    ap.add_argument("--cameras", type=int, default=8)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--readers", type=int, default=1,
                    help="Concurrent callers. >1 adds the contention pass.")
    args = ap.parse_args()

    # Postgres when DATABASE_URL points at one — which is the only way
    # the concurrency pass below means anything, since the SQLite
    # fixture shares a single connection and serialises by construction.
    url = os.environ.get("BENCH_DATABASE_URL", "")
    if url.startswith("postgres"):
        engine = create_engine(url, future=True, pool_size=max(8, args.readers),
                               max_overflow=args.readers)
        # Only the seed tables are dropped. The schema itself must come
        # from `alembic upgrade head`, because the indexes that matter
        # most here — the GIN expression index on the search text, the
        # BRIN on started_at — exist ONLY in migrations. Benchmarking a
        # create_all() schema measures Postgres without its indexes and
        # reports a number three times worse than production, which is a
        # worse lie than no number.
        from sqlalchemy import inspect as _inspect
        if not _inspect(engine).has_table("alembic_version"):
            raise SystemExit(
                "Run `alembic upgrade head` against this database first — "
                "the GIN and BRIN indexes live in migrations, and without "
                "them this measures a schema nobody runs.")
        with engine.begin() as conn:
            for t in ("visit_descriptors", "event_text", "event_embeddings",
                      "events", "cameras", "users", "roles"):
                conn.exec_driver_sql(f"TRUNCATE {t} CASCADE")
        print(f"using Postgres at {url.rsplit('@', 1)[-1]} (migrated)")
    else:
        engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
    if not url.startswith("postgres"):
        Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    print(f"seeding {args.visits:,} visits across {args.cameras} cameras…")
    t = time.perf_counter()
    seed(db, visits=args.visits, cameras=args.cameras)
    print(f"  seeded in {time.perf_counter() - t:.1f}s")
    if url.startswith("postgres"):
        # WITHOUT THIS THE NUMBERS ARE FICTION. A freshly seeded table
        # has no statistics, so the planner guesses at selectivity and
        # picks plans production would never pick — it measured free-text
        # search at 329ms where the analyzed table does it in 45ms, and
        # the difference is entirely the planner flying blind. Autovacuum
        # would get there eventually; a benchmark that starts before it
        # does is measuring the wait.
        with engine.begin() as conn:
            conn.exec_driver_sql("ANALYZE")
        print("  analyzed")
    print()

    api = FastAPI()
    api.include_router(app_platform.router, prefix="/api/v1")
    api.dependency_overrides[get_db] = lambda: db
    client = TestClient(api)
    headers = {"X-Internal-Api-Key": settings.internal_api_key}

    def get(path, **params):
        r = client.get(path, params=params, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    at = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
    cases = [
        ("find: free text",
         lambda: get("/api/v1/internal/app/search", text="red truck", limit=25)),
        ("find: text + label + window",
         lambda: get("/api/v1/internal/app/search", text="red", label="truck",
                     limit=25, **{"from": (datetime.now(UTC) - timedelta(hours=6)).isoformat()})),
        ("find: one camera",
         lambda: get("/api/v1/internal/app/search", text="red truck",
                     camera_id=1, limit=25)),
        ("visits/at: bind a frame (RFC-0003)",
         lambda: get("/api/v1/internal/app/visits/at", camera=1, at=at)),
        ("plates/inside: gate occupancy",
         lambda: get("/api/v1/internal/app/plates/inside",
                     in_cameras="1", out_cameras="2", hours=24)),
    ]

    print(f"{'query':<40} {'p50':>9} {'p95':>9} {'max':>9}")
    print("-" * 70)
    for name, fn in cases:
        p = time_it(fn, args.runs)
        print(f"{name:<40} {p['p50']:>7.1f}ms {p['p95']:>7.1f}ms {p['max']:>7.1f}ms")

    if args.readers > 1:
        _control_case.append(
            ("control", lambda: db.get(TimelineEvent, 1)))
        concurrent(cases, readers=args.readers, runs=args.runs)

    print("\nFor comparison, the shape the old private index had to scan:")
    frames = args.visits * 25 * 2      # 25s visits at 2 fps, one row per frame
    print(f"  one row per analyzed FRAME ≈ {frames:,} rows "
          f"for the same {args.visits:,} visits ({frames // args.visits}x)")
    print("  (that index had no camera scoping and no server-side filter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
