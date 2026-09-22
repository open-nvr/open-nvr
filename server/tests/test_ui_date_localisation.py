# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dates, times AND numbers in the language the operator picked.

Every ``toLocale…String`` call without a locale asks the BROWSER, not
the operator. Select Français on an American Chrome and you read
``02:02 PM``, a format France does not use; the language switcher
translates the words around the timestamp and not the timestamp.

A correction to how this file first described itself: it counted 84
"timestamps", and about fifteen of those were never timestamps.
``n.toLocaleString()`` on a NUMBER is the thousands separator — 12,000
in English, 12 000 in French — which has exactly the same defect and a
different fix. ``AIAdapters.tsx`` was the worst offender in the list at
16 sites, and fifteen of them were counts, not dates. Left as it was,
this file would have sent somebody to format a frame count with a date
formatter.

So the rule the baseline encodes is the simple one: do not call
``toLocale…String`` directly. Use ``useDateFormat()`` in a component or
``dateFormatters(language)`` in a module-level helper, and take
``.time`` / ``.date`` / ``.dateTime`` / ``.number`` from it. That needs
no guess about what the receiver is.

43 sites remain across 25 files. Converting them blind in one change
would be a large edit nobody could review against a rendered page, so
this is a ratchet rather than a gate: the count may go DOWN freely and
may not go up, and the per-file baseline says where the work is.

A file that reaches zero leaves the baseline in the same commit, which
is what makes the number fall. The test fails either way — too many is
unfinished work, too few is a stale baseline — so the list cannot
quietly stop describing the code.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app" / "src"

_CALL = re.compile(r"\.toLocale(?:Time|Date)?String\s*\(")

#: Files that still format a date or time without the operator's
#: language, and how many places each does it. Shrink this; do not grow
#: it. Use `useDateFormat()` in a component, or `dateFormatters(lang)`
#: in a module-level helper that cannot call a hook.
_BASELINE: dict[str, int] = {
    # The formatters themselves. These are the ONE correct place for a
    # raw call, and they are why the number can never reach zero.
    "i18n.tsx": 5,
    # Local-day maths, not display: these build day keys and boundaries
    # in the viewer's own timezone, which is deliberate and documented
    # at the top of the file. Localising them would change what a "day"
    # means in the recordings UI. Do not migrate without reading it.
    "lib/time.ts": 3,

    "components/JourneyPanel.tsx": 2,
    "components/MultiCamTimeline.tsx": 2,
    "components/PlaybackTimeline.tsx": 2,
    "services/alertsInboxService.ts": 1,
    "shell/AppShell.tsx": 1,
    "views/Alarms.tsx": 1,
    "views/AlertsIncidents.tsx": 2,
    "views/Cameras.tsx": 1,
    "views/Compliance.tsx": 1,
    "views/Dashboard.tsx": 2,
    "views/Loitering.tsx": 2,
    "views/PlaybackView.tsx": 2,
    "views/Support.tsx": 1,
    "views/SyncPlayback.tsx": 1,
    "views/SystemNetworkMonitoring.tsx": 2,
    "views/Tripwires.tsx": 2,
    "views/guardscan/ScreeningReport.tsx": 1,
    "views/settings/ApiTokens.tsx": 2,
    "views/settings/CameraSettings.tsx": 2,
    "views/settings/DeletedCameras.tsx": 1,
    "views/settings/DeviceFirewall.tsx": 1,
    "views/settings/RecordingPauseSetting.tsx": 2,
    "views/settings/RecordingSettings.tsx": 1,
}

#: Files whose raw calls are correct and must stay — asserted separately
#: so nobody "finishes the migration" by breaking them.
_INTENTIONAL = {"i18n.tsx", "lib/time.ts"}


def _actual() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(_APP.rglob("*.ts*")):
        if "node_modules" in path.parts:
            continue
        n = len(_CALL.findall(path.read_text(errors="ignore")))
        if n:
            found[path.relative_to(_APP).as_posix()] = n
    return found


def test_no_new_unlocalised_timestamps():
    actual = _actual()
    grew = {
        f: (n, _BASELINE.get(f, 0))
        for f, n in actual.items() if n > _BASELINE.get(f, 0)
    }
    assert grew == {}, (
        "these files format a date or time without the operator's "
        f"language (file: found, allowed): {grew}. Use useDateFormat() "
        "in a component, or dateFormatters(language) in a module-level "
        "helper — a French operator should not be reading AM/PM."
    )


def test_the_baseline_is_not_stale():
    """A file that got fixed has to leave the list, or the list stops
    being a to-do and becomes decoration."""
    actual = _actual()
    shrank = {
        f: (actual.get(f, 0), n)
        for f, n in _BASELINE.items() if actual.get(f, 0) < n
    }
    assert shrank == {}, (
        "these files now have FEWER unlocalised timestamps than the "
        f"baseline claims (file: found, recorded): {shrank}. Lower the "
        "number, or drop the entry when it reaches zero — that is the "
        "migration making progress and it belongs in the same commit."
    )


def test_the_formatters_exist_and_are_actually_used():
    """The whole point. getDateLocale was exported and never called for
    a year; a helper nobody imports fixes nothing."""
    i18n = (_APP / "i18n.tsx").read_text()
    assert "export function useDateFormat" in i18n
    assert "export function dateFormatters" in i18n
    assert "number:" in i18n, (
        "numbers need the operator's locale too — 12,000 is 12 000 in "
        "French, and without a formatter for it the migration sends "
        "people to format counts with a date helper")

    importers = [
        p.relative_to(_APP).as_posix()
        for p in sorted(_APP.rglob("*.ts*"))
        if "node_modules" not in p.parts
        and p.name != "i18n.tsx"
        and "useDateFormat" in p.read_text(errors="ignore")
    ]
    assert len(importers) >= 10, (
        f"only {importers} use the formatters; the migration has stalled")


def test_the_places_that_must_not_be_migrated_are_named():
    for f in _INTENTIONAL:
        assert f in _BASELINE, (
            f"{f} formats deliberately without a locale and must stay "
            "recorded, with the reason, so nobody 'fixes' it")


def test_no_locale_is_hardcoded_in_a_view():
    """RecordingBrowser printed dates with a literal 'en-US', so a French
    operator got American dates no matter what — worse than the browser
    default, because it ignored them too."""
    offenders = []
    for path in sorted(_APP.rglob("*.ts*")):
        if "node_modules" in path.parts or path.name == "i18n.tsx":
            continue
        text = path.read_text(errors="ignore")
        for m in re.finditer(r"toLocale\w*String\s*\(\s*'([a-z]{2}-[A-Z]{2})'", text):
            offenders.append(f"{path.relative_to(_APP).as_posix()}: {m.group(1)}")
    assert offenders == [], (
        f"a locale is hardcoded in {offenders}; the operator's language "
        "is the only thing that should decide this")
