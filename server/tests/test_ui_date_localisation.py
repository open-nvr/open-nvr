# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dates, times and numbers in the language the operator picked.

Every ``toLocale…String`` call without a locale asks the BROWSER, not
the operator. Select Français on an American Chrome and you read
``02:02 PM``, a format France does not use; the language switcher
translated the words around the timestamp and not the timestamp.

This started as a ratchet over 84 sites in 32 files, because converting
them blind would have been an edit nobody could review. It is now a
GATE: the only file allowed to call these methods is ``i18n.tsx``,
where the formatters are built. Everything else takes ``.time`` /
``.date`` / ``.dateTime`` / ``.number`` from ``useDateFormat()``, or is
handed a ``DateFormatters`` if it is a module-level helper that cannot
call a hook.

Two things the ratchet itself got wrong on the way, recorded because
both were found by reading the code it claimed to describe:

* It counted ``n.toLocaleString()`` on a NUMBER as a timestamp. That is
  the thousands separator — 12,000 in English, 12 000 in French — the
  same defect with a different fix, and fifteen of AIAdapters' sixteen
  "timestamps" were counts. The rule here needs no guess about what the
  receiver is, which is why it replaced the count.
* It excused ``lib/time.ts`` as "local-day maths, not display". Half
  true: ``formatSeenAt`` and ``seenAtTitle`` are display helpers, and
  they were giving every alarm row a browser-locale date. They take the
  formatters now. The genuine local-day maths in that file touches no
  locale API at all, so it never needed an exemption.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app" / "src"

_CALL = re.compile(r"\.toLocale(?:Time|Date)?String\s*\(")

#: The one file allowed to format directly: it is where the formatters
#: are built. Everywhere else goes through them.
_FORMATTER_MODULE = "i18n.tsx"


def _sources():
    for path in sorted(_APP.rglob("*.ts*")):
        if "node_modules" not in path.parts:
            yield path


def test_only_the_formatters_format():
    offenders = {}
    for path in _sources():
        if path.name == _FORMATTER_MODULE:
            continue
        text = path.read_text(errors="ignore")
        n = len(_CALL.findall(text))
        if n:
            offenders[path.relative_to(_APP).as_posix()] = n
    assert offenders == {}, (
        f"these call toLocale…String directly: {offenders}. Take .time / "
        ".date / .dateTime / .number from useDateFormat(), or accept a "
        "DateFormatters argument if you are a module-level helper — "
        "otherwise the browser decides the format, not the operator."
    )


def test_the_formatters_are_where_they_are_supposed_to_be():
    """If the raw calls ever leave i18n.tsx, the test above starts
    passing for the wrong reason: nothing formats anything."""
    i18n = (_APP / _FORMATTER_MODULE).read_text()
    assert len(_CALL.findall(i18n)) >= 4
    for expected in ("export function useDateFormat",
                     "export function dateFormatters"):
        assert expected in i18n, f"i18n.tsx has lost {expected!r}"
    # Twice: once in the DateFormatters type, once in the implementation.
    # A substring check passes against `numberX:`, which is exactly the
    # half-rename that would leave every count formatting itself.
    assert i18n.count("number:") >= 2, (
        "i18n.tsx must declare AND implement `number` — counts need the "
        "operator's locale as much as dates do")


def test_no_locale_is_hardcoded():
    """RecordingBrowser and RecordingTimeline printed dates with a
    literal 'en-US', so a French operator got American dates no matter
    what — worse than the browser default, because it ignored that too.
    """
    offenders = []
    for path in _sources():
        if path.name == _FORMATTER_MODULE:
            continue
        text = path.read_text(errors="ignore")
        for m in re.finditer(
                r"toLocale\w*String\s*\(\s*['\"]([a-z]{2}-[A-Z]{2})['\"]", text):
            offenders.append(f"{path.relative_to(_APP).as_posix()}: {m.group(1)}")
    assert offenders == [], (
        f"a locale is hardcoded in {offenders}; the operator's language "
        "is the only thing that should decide this")


def test_the_formatters_are_widely_used():
    """A helper nobody imports fixes nothing — which is exactly what
    getDateLocale was, for as long as it existed before this."""
    importers = [
        path.relative_to(_APP).as_posix()
        for path in _sources()
        if path.name != _FORMATTER_MODULE
        and "useDateFormat" in path.read_text(errors="ignore")
    ]
    assert len(importers) >= 25, (
        f"only {len(importers)} files use the formatters; the migration "
        "has been partly reverted")


def test_a_hook_is_never_called_from_a_plain_helper():
    """Migrating the last batch put useDateFormat() inside `formatDay`,
    a plain function called from a click handler. tsc was perfectly
    happy with it; a hook there breaks at runtime. So the rule is
    asserted rather than trusted: anything containing a useDateFormat()
    call has to be a component or another hook.
    """
    decl = re.compile(
        r"^(?:export\s+)?(?:default\s+)?function\s+(\w+)"
        r"|^(?:export\s+)?const\s+(\w+)\s*[:=]")
    offenders = []
    for path in _sources():
        if path.name == _FORMATTER_MODULE:
            continue
        lines = path.read_text(errors="ignore").split("\n")
        for i, line in enumerate(lines):
            if "useDateFormat()" not in line:
                continue
            for j in range(i, -1, -1):
                m = decl.match(lines[j])
                if not m:
                    continue
                name = m.group(1) or m.group(2)
                if not (name[0].isupper() or name.startswith("use")):
                    offenders.append(
                        f"{path.relative_to(_APP).as_posix()}:{i + 1} in {name}()")
                break
    assert offenders == [], (
        f"useDateFormat() is called outside a component in {offenders}; "
        "hand that helper a DateFormatters argument instead")
