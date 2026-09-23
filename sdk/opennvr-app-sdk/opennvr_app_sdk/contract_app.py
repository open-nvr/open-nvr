# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ContractApp`` — an app that is only an operator surface.

The three original archetypes are all driven by something arriving:
``Detector`` and ``AlertSubscriber`` wait on NATS, ``FrameApp`` polls
cameras on a timer. Each owns a loop, and the contract server
(``/health``, ``/manifest``, ``/state``, ``POST /actions``) is a
passenger on it.

Some apps have no such loop. They hold no state of their own and
consume no stream; they exist so an operator has somewhere to ask a
question, and everything they answer with they read from core when
asked. The first of them is footage-search, which used to index every
inference event into its own SQLite database and now queries the
canonical store through ``timeline.find``. Having deleted its index it
has nothing left to subscribe to — and subscribing anyway, to keep a
loop it does not need, would have meant a NATS connection whose only
purpose was to satisfy a base class.

So the loop here is the absence of one: start the contract server,
register with the catalog, poll for config, and wait until stopped.
The run/stop lifecycle is deliberately identical to ``Detector.run``
minus the NATS drain, because an app author moving between the two
should not have to learn a second shape.

An app riding this base still gets everything the catalog needs from it
— the manifest, live state, actions, camera picking, the enable switch
— which is the whole surface an operator sees. What it gives up is the
ability to react to anything on its own; if it needs that, it wants one
of the other three.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .contract import ContractMixin
from .manifest import AppManifest

logger = logging.getLogger(__name__)


class ContractApp(ContractMixin):
    """Base class for apps whose entire job is answering operator
    actions.

    Subclasses set a class-level ``manifest``, optionally override
    :meth:`setup` to allocate state, and implement :meth:`on_action`.
    ``cfg`` is the app-parsed config object; ``cfg.contract_port`` and
    ``cfg.opennvr_url`` are read by the contract lifecycle exactly as
    they are for the other bases.

    There is no ``nats_url`` and no ``subject_pattern``: an app that
    needs either is not this archetype.
    """

    manifest: AppManifest | None = None

    def __init__(self, config: Any) -> None:
        self.cfg = config
        # Compat alias, matching the other bases so shared helpers and
        # older app code that reaches for ``_config`` keep working.
        self._config = config
        self._stop_event = asyncio.Event()
        self._contract_init()
        self.setup()

    # ── App surface ────────────────────────────────────────────────

    def setup(self) -> None:
        """Optional hook — allocate per-app state. Runs once at
        construction, after ``cfg`` is set."""

    # ``state_snapshot``, ``not_ready_reason``, ``on_action`` and
    # ``camera_picked`` all come from :class:`ContractMixin`, so this
    # class adds no surface of its own beyond the lifecycle below.

    def stop(self) -> None:
        """Ask :meth:`run` to return. Safe from a signal handler via
        ``loop.call_soon_threadsafe``, which is how the app runners
        drive it."""
        self._stop_event.set()

    async def run(self, *, once: bool = False) -> None:
        """Serve the contract until stopped.

        ``once`` returns immediately after the server is up rather than
        waiting, so a test can assert that an app starts, registers and
        tears down without needing to arrange a stop. It is the same
        flag the other bases take, meaning the same thing: do the
        smallest amount of work that proves the loop ran.
        """
        self.start_contract_server()
        self.register_with_opennvr()
        self.start_config_poll()
        try:
            if once:
                return
            await self._stop_event.wait()
        finally:
            self.stop_config_poll()
            self.stop_contract_server()
