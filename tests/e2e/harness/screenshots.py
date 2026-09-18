# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capturing what the browser actually showed.

A GUI failure is the one kind this suite could not previously explain. The API
tiers have logs, metrics and an audit trail; a browser test that says
"expected visible" tells you nothing about whether the page was blank, still
loading, showing an error, or simply laid out differently than the selector
expected.

So every GUI test leaves a screenshot -- pass or fail -- and every failure
additionally leaves a Playwright trace. The asymmetry is deliberate: a
screenshot is ~50-200 KB and takes milliseconds, so having one for green runs
is worth it (it is how you notice the page rendered but rendered *wrong*).
Tracing records DOM snapshots, network and a timeline for every action; that is
far too heavy to keep for passing tests, and exactly what you want for a
failing one.

Images are embedded in the HTML report as base64 data URIs rather than linked,
so the report is a single file that survives being emailed, attached to a CI
artifact, or opened from disk with no server.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Full-page rather than viewport: a table that scrolled out of view is
#: usually the thing you need to see.
_FULL_PAGE = True


@dataclass
class Shot:
    """One captured screenshot."""

    #: Where it landed on disk, for people who want the raw file.
    path: Path
    #: What was happening. Shown as the caption in the report.
    caption: str
    #: Raw PNG bytes, kept so the report can inline them without a re-read.
    data: bytes = b""

    def data_uri(self) -> str:
        """The image as a data: URI, for embedding in a self-contained report."""
        if not self.data:
            try:
                self.data = self.path.read_bytes()
            except OSError:
                return ""
        return "data:image/png;base64," + base64.b64encode(self.data).decode("ascii")


def capture(page, target_dir: Path, caption: str, name: str = "screen") -> Shot | None:
    """Screenshot ``page`` into ``target_dir``. Returns None if it could not.

    Never raises. This runs during teardown, often while a test is already
    failing, and a screenshot that throws would replace a real diagnosis with a
    complaint about the diagnostic machinery. A closed page (the browser
    crashed, or the test navigated away and Playwright tore it down) is the
    common case and is simply reported as "no screenshot".
    """
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.png"
        data = page.screenshot(path=str(path), full_page=_FULL_PAGE)
        return Shot(path=path, caption=caption, data=data)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see docstring
        log.info("could not screenshot (%s): %s", caption, exc)
        return None


def start_tracing(context) -> bool:
    """Begin recording a Playwright trace. Returns whether it started.

    Started for every GUI test and thrown away unless the test fails --
    stopping without a path discards the buffer, which is much cheaper than
    deciding up front whether a test is going to fail.
    """
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("could not start tracing: %s", exc)
        return False


def stop_tracing(context, target_dir: Path | None) -> Path | None:
    """Stop tracing. Writes a trace.zip when ``target_dir`` is given, else discards.

    Returns the trace path, or None. View one with::

        playwright show-trace trace.zip
    """
    try:
        if target_dir is None:
            context.tracing.stop()
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "trace.zip"
        context.tracing.stop(path=str(path))
        return path
    except Exception as exc:  # noqa: BLE001
        log.info("could not stop tracing: %s", exc)
        return None


__all__ = ["Shot", "capture", "start_tracing", "stop_tracing"]
