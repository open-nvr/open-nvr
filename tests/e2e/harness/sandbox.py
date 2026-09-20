# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-test namespace and guaranteed teardown.

The suite boots the stack **once** and shares it across every test, because a
fresh stack per test would cost minutes each. The price of a shared stack is
that one test leaking a camera, a user or a skill assignment silently changes
what later tests see — and that failure mode is miserable to debug, because the
test that breaks is never the test that leaked.

``Sandbox`` removes the possibility rather than relying on discipline:

* Every name a test invents is prefixed with a unique namespace, so two tests
  (or two concurrent runs against one stack) cannot collide.
* Every entity created through ``OpenNVRClient`` is registered automatically.
  **A test author writes no cleanup code and cannot forget to.**
* Teardown runs in reverse creation order inside a ``finally``, so it happens
  after a failure, an assertion error, or a Ctrl-C — the cases where cleanup
  matters most and hand-written teardown is least likely to run.

Reverse order matters: a camera permission references a user and a camera, so
it has to go before either. LIFO gets that right for free, whereas a fixed
teardown sequence has to be maintained as the schema grows.

Cleanup is also *best-effort and reported*: one failing deletion must not
prevent the rest, and anything that could not be removed is surfaced loudly at
the end of the run rather than quietly poisoning the next test.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: Every entity the suite creates carries this prefix. The isolation check at
#: the end of a run asserts that nothing with this prefix survives, which is
#: only meaningful because *all* creation goes through the sandbox.
E2E_PREFIX = "e2e-"


@dataclass
class _Cleanup:
    description: str
    undo: Callable[[], None]


@dataclass
class Sandbox:
    """A test's private namespace plus its undo stack.

    Obtained from the ``sandbox`` fixture; never constructed by a test.
    """

    #: Short unique namespace, e.g. ``e2e-3f9a1c2b``. Deliberately short: the
    #: camera API caps ``name`` at 100 characters and a pytest node id can be
    #: far longer than that.
    namespace: str
    #: The pytest node id, kept only for evidence reports.
    test_id: str
    _cleanups: list[_Cleanup] = field(default_factory=list)
    _closed: bool = False

    # -- naming ----------------------------------------------------------
    def name(self, label: str = "") -> str:
        """A collision-proof, greppable name for a new entity.

        ``sandbox.name("gate")`` -> ``e2e-3f9a1c2b-gate``. Use it for anything
        the API will persist, so a leaked row can be traced back to a run.
        """
        suffix = f"-{label}" if label else ""
        return f"{self.namespace}{suffix}"

    # -- registration ----------------------------------------------------
    def track(self, description: str, undo: Callable[[], None]) -> None:
        """Register an undo action, to run at teardown in reverse order.

        ``OpenNVRClient`` calls this for you on every create. Call it directly
        only for state the client does not model — a settings value you changed
        and must restore, for instance.

        Args:
            description: what is being undone, for the teardown log. Include
                the identifier: "camera 12 (e2e-3f9a1c2b-gate)".
            undo: idempotent callable. It may run against an entity another
                cleanup already removed, so it must tolerate a 404.
        """
        if self._closed:
            raise RuntimeError(
                f"sandbox {self.namespace} is already torn down; "
                f"cannot track {description!r}"
            )
        self._cleanups.append(_Cleanup(description, undo))

    # -- teardown --------------------------------------------------------
    def close(self) -> list[str]:
        """Run every undo in reverse order. Returns descriptions that failed.

        Never raises: teardown runs in a ``finally`` during a possibly-already
        failing test, and masking the real assertion with a cleanup error would
        be strictly worse than reporting the leak.
        """
        if self._closed:
            return []
        self._closed = True
        failures: list[str] = []

        for cleanup in reversed(self._cleanups):
            try:
                cleanup.undo()
            except Exception as exc:
                failures.append(f"{cleanup.description}: {type(exc).__name__}: {exc}")
                log.warning(
                    "sandbox %s: failed to clean up %s: %s",
                    self.namespace,
                    cleanup.description,
                    exc,
                )

        self._cleanups.clear()
        return failures

    @property
    def tracked(self) -> list[str]:
        """Descriptions of everything still awaiting cleanup."""
        return [c.description for c in self._cleanups]


def new_sandbox(test_id: str) -> Sandbox:
    """Build a sandbox for one test. Called by the ``sandbox`` fixture."""
    return Sandbox(namespace=f"{E2E_PREFIX}{uuid.uuid4().hex[:8]}", test_id=test_id)


__all__ = ["Sandbox", "new_sandbox", "E2E_PREFIX"]
