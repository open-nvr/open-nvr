# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-request overheads on the evidence-image path.

The Vehicles page mounts one auth-gated image per table row, so anything
this path does per request is multiplied by a screenful at a time and runs
on the single event loop that serves everything else. Three costs were
being paid for nothing, and all three are pinned here:

* **gzip** — Starlette compresses anything that is not text/event-stream,
  so every already-compressed JPEG was deflated at level 9 for a fraction
  of a percent, and lost its Content-Length into the bargain;
* **logging** — two structured records per request, one carrying a full
  header dump;
* **mkdir** — ``evidence_root()`` created the directory on every READ.

The load-bearing case in each is the near miss: the JSON siblings
(``/events/plate-stats``, ``/vehicle-report``) live under the same prefix
as the images and must keep their compression and their full logs. A
naive prefix rule silently swallows them, which is why these are suffix
rules and why each block tests the sibling as well as the image.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_imgpath_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from middleware.compression import SelectiveGZipMiddleware  # noqa: E402

# Big enough to clear the middleware's minimum_size, and incompressible
# enough to stand in for a JPEG.
_BODY = bytes(range(256)) * 40          # 10 KB, high entropy

IMAGE_PATHS = (
    "/api/v1/events/42/evidence",
    "/api/v1/events/42/plate-evidence",
    "/api/v1/events/42/scene-evidence",
    "/api/v1/events/42/plate-frame",
)
# The near misses: same prefix, JSON payloads, must still compress.
JSON_PATHS = (
    "/api/v1/events",
    "/api/v1/events/plate-stats",
    "/api/v1/events/vehicle-report",
)


def _app() -> Starlette:
    async def image(_request):
        return Response(_BODY, media_type="image/jpeg")

    async def streamed_image(_request):
        async def chunks():
            # The FileResponse shape: several body messages, more_body=True.
            for i in range(0, len(_BODY), 2048):
                yield _BODY[i:i + 2048]

        return StreamingResponse(chunks(), media_type="image/jpeg")

    async def js(_request):
        return Response(b'{"rows": []}' + b" " * 4096,
                        media_type="application/json")

    routes = [Route(p, image) for p in IMAGE_PATHS]
    routes += [Route(p, js) for p in JSON_PATHS]
    routes += [
        Route("/api/v1/events/99/scene-evidence", streamed_image),
        Route("/api/v1/recordings/playback/hls/x.m3u8", image),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    return app


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


@pytest.mark.parametrize("path", IMAGE_PATHS)
def test_evidence_images_are_not_gzipped(client, path):
    r = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in {k.lower() for k in r.headers}
    assert r.content == _BODY


def test_a_streamed_image_is_not_gzipped(client):
    """FileResponse sends the body in chunks; the bypass has to survive that
    shape, not just the single-message one."""
    r = client.get("/api/v1/events/99/scene-evidence",
                   headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in {k.lower() for k in r.headers}
    assert r.content == _BODY


@pytest.mark.parametrize("path", JSON_PATHS)
def test_the_json_siblings_are_still_gzipped(client, path):
    """The case a prefix rule fails. These share /api/v1/events/ with the
    images and are exactly what gzip is for."""
    r = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_the_media_prefix_bypass_still_works(client):
    r = client.get("/api/v1/recordings/playback/hls/x.m3u8",
                   headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in {k.lower() for k in r.headers}



# ── request logging ─────────────────────────────────────────────────


def test_evidence_images_log_one_slim_record_not_two():
    from middleware.request_logging import _is_slim_path

    for p in IMAGE_PATHS:
        assert _is_slim_path(p), f"{p} still writes a full pair of records"


def test_the_json_siblings_still_log_in_full():
    from middleware.request_logging import _is_slim_path

    for p in JSON_PATHS:
        assert not _is_slim_path(p), (
            f"{p} lost its request log — a suffix rule must not swallow the "
            "JSON endpoints that share the events prefix")


def test_slim_paths_are_not_confused_by_a_lookalike():
    from middleware.request_logging import _is_slim_path

    # Same suffix, different resource tree: must not match.
    assert not _is_slim_path("/api/v1/cameras/3/evidence")


# ── evidence_root ───────────────────────────────────────────────────


def test_evidence_root_creates_nothing(tmp_path, monkeypatch):
    """It is called once per image READ; creating a directory on that path
    was a syscall per request for something the write path guarantees."""
    from services import evidence_store

    monkeypatch.setattr(
        evidence_store, "settings",
        _types.SimpleNamespace(recordings_base_path=str(tmp_path / "rec")),
    )
    root = evidence_store.evidence_root()
    assert not root.exists()


def test_resolve_on_a_missing_root_answers_none_rather_than_raising(
    tmp_path, monkeypatch,
):
    from services import evidence_store

    monkeypatch.setattr(
        evidence_store, "settings",
        _types.SimpleNamespace(recordings_base_path=str(tmp_path / "rec")),
    )
    assert evidence_store.resolve_evidence("ab/nope.jpg") is None


def test_saving_still_creates_the_root_on_a_virgin_volume(tmp_path, monkeypatch):
    """The promise that makes dropping the read-path mkdir safe."""
    from services import evidence_store

    monkeypatch.setattr(
        evidence_store, "settings",
        _types.SimpleNamespace(recordings_base_path=str(tmp_path / "rec")),
    )
    rel = evidence_store.save_evidence_jpeg(b"\xff\xd8" + b"\x00" * 64)
    assert evidence_store.resolve_evidence(rel) is not None
