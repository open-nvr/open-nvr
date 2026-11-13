# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A single self-contained HTML report for a run.

The markdown report that already exists reads well in a terminal, but it
cannot show you what the browser saw. This writes ``index.html`` next to it:
one file, inline CSS, screenshots embedded as data URIs, no CDN and no server.

That matters more than it sounds. A report with linked assets breaks the moment
it is attached to a CI artifact, emailed, or opened from a different directory
-- which is exactly when someone needs to read it.

It extends the existing evidence bundle rather than replacing it. Provenance
comes from the same data the markdown report uses, so the two can never
disagree about which image a run used, and each failure links to its
``ISSUE.md``, logs and Playwright trace on disk.

Written only after ``Evidence.__init__`` has cleared the artifacts root --
writing earlier would put the report in the path of its own rmtree.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Deliberately small and dependency-free. No CDN: the report has to open from
#: disk on a machine with no network. Honours the reader's colour scheme.
_CSS = """
:root { color-scheme: light dark; --pass:#1a7f37; --fail:#cf222e; --skip:#9a6700;
        --xfail:#8250df; --line:#d0d7de; --muted:#57606a; --bg:#ffffff;
        --fg:#1f2328; --panel:#f6f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --line:#30363d; --muted:#8b949e; --bg:#0d1117; --fg:#e6edf3;
          --panel:#161b22; --pass:#3fb950; --fail:#f85149; --skip:#d29922;
          --xfail:#a371f7; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.5rem; background:var(--bg); color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }
.wrap { max-width:1100px; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; }
h2 { font-size:1.05rem; margin:2rem 0 .75rem; padding-bottom:.35rem;
     border-bottom:1px solid var(--line); }
.sub { color:var(--muted); margin:0 0 1.5rem; }
.tally { display:flex; gap:.5rem; flex-wrap:wrap; margin:0 0 1.5rem; }
.chip { padding:.3rem .7rem; border-radius:2rem; font-weight:600;
        font-size:.85rem; border:1px solid var(--line); background:var(--panel); }
.chip.pass{color:var(--pass)} .chip.fail{color:var(--fail)}
.chip.skip{color:var(--skip)} .chip.xfail{color:var(--xfail)}
table { border-collapse:collapse; width:100%; font-size:.88rem; }
th,td { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { color:var(--muted); font-weight:600; }
code,.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
.status { font-weight:600; white-space:nowrap; }
.status.passed{color:var(--pass)} .status.failed,.status.error{color:var(--fail)}
.status.skipped{color:var(--skip)} .status.xfailed{color:var(--xfail)}
details { border:1px solid var(--line); border-radius:6px; margin:.6rem 0;
          background:var(--panel); }
summary { cursor:pointer; padding:.6rem .8rem; font-weight:600; }
details > div { padding:0 .8rem .8rem; }
figure { margin:.75rem 0; }
figure img { max-width:100%; border:1px solid var(--line); border-radius:6px;
             display:block; }
figcaption { color:var(--muted); font-size:.8rem; margin-top:.35rem; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px;
      padding:.7rem; overflow-x:auto; font-size:.8rem; }
.files li { margin:.15rem 0; }
"""


@dataclass
class TestReport:
    """One test's contribution to the run report."""

    node_id: str
    outcome: str
    duration: float = 0.0
    slug: str = ""
    #: (caption, data-uri) pairs, already embedded so the report stays portable.
    screenshots: list[tuple[str, str]] = field(default_factory=list)
    trace: str = ""
    failure_text: str = ""
    #: (describe, elapsed, attempts, satisfied) taken from waiting.records()
    waits: list[tuple[str, float, int, bool]] = field(default_factory=list)
    guard_notes: list[str] = field(default_factory=list)

    @property
    def is_bad(self) -> bool:
        return self.outcome in ("failed", "error")


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def write_html_report(
    target: Path,
    reports: list[TestReport],
    provenance_rows: list[tuple[str, str]],
) -> Path:
    """Write ``index.html`` into ``target`` and return its path."""
    target.mkdir(parents=True, exist_ok=True)
    path = target / "index.html"

    counts: dict[str, int] = {}
    for report in reports:
        counts[report.outcome] = counts.get(report.outcome, 0) + 1

    parts: list[str] = [
        "<!doctype html><html lang=en><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        "<title>OpenNVR E2E report</title>",
        "<style>" + _CSS + "</style>",
        "<div class=wrap>",
        "<h1>OpenNVR end-to-end run</h1>",
        "<p class=sub>"
        + _e(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        + "</p>",
        _tally(counts),
        _provenance(provenance_rows),
    ]

    bad = [r for r in reports if r.is_bad]
    if bad:
        parts.append("<h2>Failures</h2>")
        parts.extend(_failure_block(r) for r in bad)

    with_shots = [r for r in reports if r.screenshots and not r.is_bad]
    if with_shots:
        parts.append("<h2>Screens</h2>")
        parts.append(
            "<p class=sub>Every GUI test leaves a screenshot, so a page that "
            "rendered but rendered <em>wrongly</em> is visible here even on a "
            "green run.</p>"
        )
        parts.extend(_screens_block(r) for r in with_shots)

    parts.append("<h2>All tests</h2>")
    parts.append(_results_table(reports))
    parts.append("</div></html>")

    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _tally(counts: dict[str, int]) -> str:
    order = [
        ("passed", "pass"),
        ("failed", "fail"),
        ("error", "fail"),
        ("skipped", "skip"),
        ("xfailed", "xfail"),
        ("xpassed", "xfail"),
    ]
    chips = [
        "<span class='chip " + css + "'>" + str(counts[name]) + " " + _e(name) + "</span>"
        for name, css in order
        if counts.get(name)
    ]
    return "<div class=tally>" + ("".join(chips) or "<span class=chip>no tests</span>") + "</div>"


def _provenance(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    body = "".join(
        "<tr><th>" + _e(key) + "</th><td class=mono>" + _e(value) + "</td></tr>"
        for key, value in rows
    )
    return (
        "<details><summary>Environment</summary><div><table>"
        + body
        + "</table></div></details>"
    )


def _results_table(reports: list[TestReport]) -> str:
    rows = "".join(
        "<tr><td class=mono>"
        + _e(r.node_id)
        + "</td><td class='status "
        + _e(r.outcome)
        + "'>"
        + _e(r.outcome)
        + "</td><td>"
        + "{:.1f}s".format(r.duration)
        + "</td><td>"
        + ("yes" if r.screenshots else "")
        + "</td></tr>"
        for r in reports
    )
    return (
        "<table><thead><tr><th>test</th><th>outcome</th><th>duration</th>"
        "<th>screenshot</th></tr></thead><tbody>" + rows + "</tbody></table>"
    )


def _figure(caption: str, uri: str) -> str:
    return (
        "<figure><img alt='"
        + _e(caption)
        + "' src='"
        + uri
        + "'><figcaption>"
        + _e(caption)
        + "</figcaption></figure>"
    )


def _failure_block(report: TestReport) -> str:
    inner: list[str] = []

    if report.guard_notes:
        items = "".join("<li>" + _e(note) + "</li>" for note in report.guard_notes)
        inner.append(
            "<p><strong>Verified before the failure</strong></p><ul>" + items + "</ul>"
        )

    failed_wait = next((w for w in report.waits if not w[3]), None)
    if failed_wait:
        describe, elapsed, attempts, _ok = failed_wait
        inner.append(
            "<p><strong>Timed out waiting for "
            + _e(describe)
            + "</strong> after {:.1f}s over {} attempts.</p>".format(elapsed, attempts)
        )

    for caption, uri in report.screenshots:
        if uri:
            inner.append(_figure(caption, uri))

    if report.failure_text:
        inner.append("<pre>" + _e(report.failure_text[:6000]) + "</pre>")

    files: list[str] = []
    if report.slug:
        slug = _e(report.slug)
        files.append("<li><a href='" + slug + "/ISSUE.md'>ISSUE.md</a></li>")
        files.append("<li><a href='" + slug + "/logs/'>logs/</a></li>")
        files.append("<li><a href='" + slug + "/metrics.txt'>metrics.txt</a></li>")
    if report.trace:
        files.append(
            "<li><code>"
            + _e(report.trace)
            + "</code> &mdash; <code>playwright show-trace &lt;file&gt;</code></li>"
        )
    if files:
        inner.append(
            "<p><strong>Evidence</strong></p><ul class=files>" + "".join(files) + "</ul>"
        )

    return (
        "<details open><summary class='status "
        + _e(report.outcome)
        + "'>"
        + _e(report.node_id)
        + "</summary><div>"
        + "".join(inner)
        + "</div></details>"
    )


def _screens_block(report: TestReport) -> str:
    figures = "".join(
        _figure(caption, uri) for caption, uri in report.screenshots if uri
    )
    if not figures:
        return ""
    return (
        "<details><summary>"
        + _e(report.node_id)
        + "</summary><div>"
        + figures
        + "</div></details>"
    )


__all__ = ["TestReport", "write_html_report"]
