# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""A tiny in-process OpenNVR for the tests: real HTTP, real aiohttp client."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []


@asynccontextmanager
async def fake_site(routes: dict[tuple[str, str], object]):
    """``routes``: (METHOD, path) -> a dict (JSON 200), a (status, body[,
    headers]) tuple, or an async handler. Yields (base_url, session, recorder)."""
    rec = Recorder()

    async def handle(request: web.Request):
        body = await request.read()
        rec.requests.append({"method": request.method, "path": request.path,
                             "query": dict(request.query), "headers": dict(request.headers),
                             "body": body})
        spec = routes.get((request.method, request.path))
        if spec is None:
            return web.json_response({"detail": "no route"}, status=404)
        if callable(spec):
            return await spec(request)
        if isinstance(spec, tuple):
            status, payload, *rest = spec
            headers = rest[0] if rest else None
            if isinstance(payload, (dict, list)):
                return web.json_response(payload, status=status, headers=headers)
            return web.Response(status=status, text=payload or "", headers=headers)
        return web.json_response(spec)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    async with TestServer(app) as ts, aiohttp.ClientSession() as session:
        yield str(ts.make_url("")).rstrip("/"), session, rec
