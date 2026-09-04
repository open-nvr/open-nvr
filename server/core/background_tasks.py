# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Keep background tasks alive — the asyncio.create_task() GC footgun.

``asyncio.create_task()`` returns a Task the event loop holds only
WEAKLY. A task spawned fire-and-forget style — ``asyncio.create_task(
background_x())`` with the return value dropped — can be garbage
collected mid-flight; the collector closes its coroutine, which raises
``GeneratorExit`` at the current await point. This is not theoretical:
in the field the alerts-inbox consumer subscribed to ``opennvr.alerts.>``
and was destroyed ONE SECOND later (``Alerts inbox consumer failed:
coroutine ignored GeneratorExit``), silently unsubscribing the whole
site's alarm chain while every producer kept publishing.

Two rules, one module:

* :func:`spawn_background` — every long-lived fire-and-forget task goes
  through here. It keeps a strong reference in a module-level set until
  the task finishes (the pattern the asyncio docs themselves prescribe).
* :func:`run_consumer_forever` — a supervisor for tasks that must
  OUTLIVE their own bugs. A clean return means "deliberately disabled"
  and stops; an exception is logged and the consumer is restarted after
  a delay; cancellation propagates (shutdown stays prompt).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine

logger = logging.getLogger(__name__)

#: Restart delay for supervised consumers. Slow on purpose: a consumer
#: that dies instantly on every attempt must not melt the log.
RESTART_DELAY_SECONDS = 60.0

#: Strong references to every in-flight background task. The event loop
#: only holds tasks weakly; this set is what stops the GC from closing a
#: running consumer coroutine out from under the site.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_background(
    coro: Coroutine[object, object, object], *, name: str | None = None
) -> asyncio.Task:
    """``asyncio.create_task`` that the garbage collector cannot kill.

    The task is held in :data:`_BACKGROUND_TASKS` until it completes,
    then discarded via done-callback so finished tasks don't accumulate.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def run_consumer_forever(
    name: str,
    loop_factory: Callable[[], Awaitable[object]],
    *,
    restart_delay: float = RESTART_DELAY_SECONDS,
) -> None:
    """Supervise a consumer loop for the process lifetime.

    * clean return  → the loop chose to stop (no NATS URL, no nats-py);
      respected, no restart.
    * CancelledError → shutdown; re-raised untouched.
    * anything else → logged with traceback, restarted after
      ``restart_delay``. A one-off crash (or a GC kill, should one ever
      slip past the keeper again) costs the site one minute of alarms,
      not the rest of the uptime.
    """
    while True:
        try:
            await loop_factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "%s crashed; restarting in %.0fs", name, restart_delay,
                exc_info=True,
            )
            await asyncio.sleep(restart_delay)
