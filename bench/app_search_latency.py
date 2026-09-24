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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--visits", type=int, default=50_000)
    ap.add_argument("--cameras", type=int, default=8)
    ap.add_argument("--runs", type=int, default=50)
    args = ap.parse_args()

    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    print(f"seeding {args.visits:,} visits across {args.cameras} cameras…")
    t = time.perf_counter()
    seed(db, visits=args.visits, cameras=args.cameras)
    print(f"  seeded in {time.perf_counter() - t:.1f}s\n")

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

    print("\nFor comparison, the shape the old private index had to scan:")
    frames = args.visits * 25 * 2      # 25s visits at 2 fps, one row per frame
    print(f"  one row per analyzed FRAME ≈ {frames:,} rows "
          f"for the same {args.visits:,} visits ({frames // args.visits}x)")
    print("  (that index had no camera scoping and no server-side filter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
