# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Before/after evidence for UI refactors: screenshots plus the API contract.

Not a pass/fail test. Run it on the commit before a UI change and on the
commit after, then compare the two with ``tests/e2e/tools/sweep_diff.py``:

    python tests/e2e/run.py -m sweep            # writes .artifacts/sweep/<label>/
    python tests/e2e/tools/sweep_diff.py <before-dir> <after-dir>

Two kinds of evidence, for two kinds of regression:

* **Screenshots** of every route, dark and light, at desktop, laptop, tablet
  and phone sizes -- for the regressions a person sees.
* **The API contract**: every request each page makes on load, reduced to
  ``METHOD /path/{id}?sorted,query,keys {sorted body keys} -> status``. A
  presentation-only change must leave this identical. A difference means the
  refactor changed what the page *does*, which no screenshot shows -- a field
  that no longer reaches the payload, a fetch that stopped firing, a query
  parameter renamed in passing.

The label defaults to a timestamp; set ``E2E_SWEEP_LABEL`` (e.g. ``before``)
to name the run. It lives under the ``sweep`` marker and never runs in the
normal tiers.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from pages.app_routes import ROUTES

pytestmark = pytest.mark.sweep

SIZES = {
    "desktop": {"width": 1920, "height": 1080},
    "laptop": {"width": 1366, "height": 768},
    "tablet": {"width": 820, "height": 1180},
    "phone": {"width": 390, "height": 844},
}
THEMES = ("dark", "light")
# The contract is recorded once per route, at one size and theme: requests do
# not depend on either, and a single recording keeps the diff free of noise.
CONTRACT_SIZE, CONTRACT_THEME = "desktop", "dark"
SETTLE_MS = 3_000

_ID = re.compile(r"/(\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{24,})(?=/|$)", re.I)


def _shape(request, status: int | None) -> str | None:
    """One request, reduced to what a refactor must not change."""
    url = urlparse(request.url)
    if not url.path.startswith("/api/"):
        return None
    path = _ID.sub("/{id}", url.path)
    query = ",".join(sorted(parse_qs(url.query, keep_blank_values=True)))
    body = ""
    if request.method not in ("GET", "HEAD"):
        try:
            payload = request.post_data_json
            if isinstance(payload, dict):
                body = " {" + ",".join(sorted(payload)) + "}"
        except Exception:  # noqa: BLE001 -- not JSON: shape unknown, keep method+path
            body = " {?}"
    q = f"?{query}" if query else ""
    return f"{request.method} {path}{q}{body} -> {status}"


def _seed_script(admin, theme: str) -> str:
    seed = json.dumps(
        {
            "access": admin.access_token,
            "refresh": admin.refresh_token or "",
            "device": admin.device_token or "",
            "theme": theme,
        }
    )
    return f"""(() => {{
        const t = {seed};
        localStorage.setItem('opennvr.token', t.access);
        if (t.refresh) localStorage.setItem('opennvr.refresh_token', t.refresh);
        if (t.device) localStorage.setItem('opennvr.device_token', t.device);
        localStorage.setItem('opennvr.theme', t.theme);
        localStorage.setItem('opennvr.language', 'en');
    }})()"""


def test_sweep_every_route(browser, config, admin):
    label = os.environ.get("E2E_SWEEP_LABEL") or time.strftime("%Y%m%d-%H%M%S")
    out = Path(config.artifacts) / "sweep" / label
    (out / "shots").mkdir(parents=True, exist_ok=True)

    contract: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    for theme in THEMES:
        for size_name, viewport in SIZES.items():
            context = browser.new_context(
                base_url=config.nginx,
                ignore_https_errors=True,
                viewport=viewport,
                service_workers="block",
            )
            context.add_init_script(_seed_script(admin, theme))
            page = context.new_page()
            try:
                for route in ROUTES:
                    record = theme == CONTRACT_THEME and size_name == CONTRACT_SIZE
                    seen: set[str] = set()
                    if record:
                        def on_response(resp, seen=seen):
                            shape = _shape(resp.request, resp.status)
                            if shape:
                                seen.add(shape)

                        page.on("response", on_response)
                    page.goto(route)
                    page.wait_for_timeout(SETTLE_MS)
                    if record:
                        page.remove_listener("response", on_response)
                        contract[route] = sorted(seen)

                    widths = page.evaluate(
                        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
                    )
                    if widths[0] > widths[1] + 1:
                        overflow.setdefault(route, []).append(f"{theme}/{size_name}: {widths[0]}>{widths[1]}")

                    slug = route.strip("/").replace("/", "__") or "dashboard"
                    page.screenshot(path=str(out / "shots" / f"{slug}--{theme}--{size_name}.png"))
            finally:
                context.close()

    (out / "contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True))
    (out / "overflow.json").write_text(json.dumps(overflow, indent=2, sort_keys=True))
    print(f"\nsweep written to {out}")
