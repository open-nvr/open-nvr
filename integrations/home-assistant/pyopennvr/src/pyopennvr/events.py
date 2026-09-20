# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The OpenNVR events websocket, protocol v2 (contract 1.x).

``EventStream`` keeps one connection open for as long as it runs:

* each connection mints a single-use ticket (``POST /events/ws-ticket``),
  then opens ``/events/ws?v=2``;
* it remembers the server's ``epoch`` and the last ``seq`` it saw, and on a
  reconnect asks to resume (``since`` + ``epoch``). The server replays what
  was missed (up to 5 minutes) or, when it can't, sends a ``state_snapshot``
  with ``resync: true``; the consumer rebuilds from that;
* reconnects back off exponentially (1 s .. 60 s, with jitter) and reset
  once a connection is established;
* the server sends a heartbeat after 25 s of silence, so a connection
  quiet for ``silence_timeout`` is treated as dead and reopened;
* close code 4401 means "reconnect": the server closes with it when the
  token was revoked AND when what the token may see changed (a camera
  shared, a permission changed). Reconnecting mints a new ticket, and only
  a ticket refused with 401/403 stops the stream as ``auth_failed``: a dead
  token can't mint one. A ticket refused for the caller's ADDRESS
  (``X-OpenNVR-Error: token_address``) is retried: a new token wouldn't
  help, fixing the token's allowed addresses will;
* a consumer that raises on a frame is logged and the stream goes on.

Tolerant: frames of unknown types are passed to the consumer unchanged, and
malformed text frames are skipped.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .client import OpenNVRClient
from .exceptions import OpenNVRAuthError, OpenNVRError

_LOGGER = logging.getLogger(__name__)

#: Close code the server uses when a token was revoked or what it may see
#: changed; either way: reconnect (a dead token then fails to mint a ticket).
CLOSE_TOKEN_REVOKED = 4401

Consumer = Callable[[dict[str, Any]], Awaitable[None] | None]
StateListener = Callable[[str], Awaitable[None] | None]


async def _call(fn, *args) -> None:
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


class EventStream:
    """Run with ``await stream.run()`` (usually as a background task)."""

    def __init__(
        self,
        client: OpenNVRClient,
        session: aiohttp.ClientSession,
        on_frame: Consumer,
        *,
        on_state: StateListener | None = None,
        types: list[str] | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        silence_timeout: float = 90.0,
    ) -> None:
        self._client = client
        self._session = session
        self._on_frame = on_frame
        self._on_state = on_state
        self._types = types
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._silence = silence_timeout
        self._stopping = False
        #: Set by ``stop()``: wakes the reconnect sleep, which can otherwise be
        #: a minute long, so stopping never waits for the next attempt.
        self._stop_event = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        #: Whether the CURRENT connection got as far as ``subscribed``. An
        #: instance attribute, not a local of ``_connect_once``: a connection
        #: that lived for an hour and then died by exception (a half-open TCP
        #: connection surfaces as ``asyncio.TimeoutError`` from the receive
        #: timeout) must still reset the backoff, and a local would be lost
        #: with the exception.
        self._established = False
        #: The server epoch and last seq seen: what a reconnect resumes from.
        self.epoch: str | None = None
        self.last_seq: int | None = None
        #: "connecting" | "connected" | "disconnected" | "auth_failed" | "stopped"
        self.state = "disconnected"

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await _call(self._on_state, state)

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def run(self) -> None:
        backoff = self._min_backoff
        while not self._stopping:
            await self._set_state("connecting")
            self._established = False
            try:
                await self._connect_once()
            except OpenNVRAuthError as exc:
                if exc.code != "token_address":
                    await self._set_state("auth_failed")
                    return
                _LOGGER.debug("OpenNVR refuses this address: %s", exc)
            except (OpenNVRError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.debug("OpenNVR events connection failed: %s", _redact(exc))
            if self.state == "auth_failed" or self._stopping:
                break
            await self._set_state("disconnected")
            backoff = (self._min_backoff if self._established
                       else min(backoff * 2, self._max_backoff))
            # Waiting on the stop event rather than sleeping: ``stop()`` must
            # interrupt a backoff that can be a minute long.
            try:
                await asyncio.wait_for(self._stop_event.wait(),
                                       backoff * random.uniform(0.8, 1.2))
            except asyncio.TimeoutError:
                pass
        if self.state != "auth_failed":
            await self._set_state("stopped")

    async def _connect_once(self) -> None:
        """One connection, until it closes. Sets ``_established`` once the
        server's ``subscribed`` frame arrives (so the backoff resets)."""
        ticket = await self._client.ws_ticket()
        # Minting the ticket is a round trip during which ``stop()`` finds no
        # socket to close: without this check a socket would be opened after
        # the stop and live until the server closed it.
        if self._stopping:
            return
        url = self._client.ws_url(ticket, since=self.last_seq, epoch=self.epoch,
                                  types=self._types)
        async with self._session.ws_connect(url, ssl=self._client.ssl,
                                            receive_timeout=self._silence,
                                            heartbeat=None) as ws:
            self._ws = ws
            if self._stopping:
                # Stopped while the handshake was in flight, so the socket
                # was not there for ``stop()`` to close.
                await ws.close()
                return
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("event_type") == "subscribed":
                    self._established = True
                    await self._set_state("connected")
                    if frame.get("epoch") != self.epoch:
                        self.last_seq = None      # a new server process
                    self.epoch = frame.get("epoch")
                elif (isinstance(frame.get("seq"), int)
                      and frame.get("event_type") not in ("heartbeat", "lagged")):
                    # Events and the snapshot only. A heartbeat carries the
                    # server's CURRENT seq, and an event for us can still be
                    # queued behind it: resuming from the heartbeat's seq
                    # would skip that event.
                    self.last_seq = frame["seq"]
                try:
                    await _call(self._on_frame, frame)
                except Exception:  # noqa: BLE001 - one bad frame must not end push
                    _LOGGER.exception("OpenNVR event consumer failed on %s",
                                      frame.get("event_type"))
            if ws.close_code == CLOSE_TOKEN_REVOKED:
                _LOGGER.debug("OpenNVR closed the events socket (4401): reconnecting")
        self._ws = None


_QUERY = re.compile(r"\?[^\s'\"]*")


def _redact(exc: BaseException) -> str:
    """``str(exc)`` with any URL query removed. aiohttp's handshake errors
    (``WSServerHandshakeError``) print the URL they failed on, and ours
    carries the single-use ticket: it must not end up in a log."""
    return _QUERY.sub("?<redacted>", str(exc))
