# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera tours: a display board that cycles through sets of cameras.

The schedule itself is unit-tested (app/tests/tourPlan.test.mjs) and the
API's filtering is server-tested (server/tests/test_tours.py). What only a
browser can say is that the wall really turns: the first page's cameras are
on screen, the second page's replace them when its time is up, Pause holds
the wall past that time and Next moves it on.

It must also leave Live View alone: a tour never touches the operator's own
saved grid.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

from harness import selectors as S
from harness.budgets import BUDGETS
from harness.waiting import eventually
from pages.base import fatal_errors
from pages.live import DISPLAY_ORDER_KEY
from pages.tour import TourPage

pytestmark = pytest.mark.ui

#: The shortest page the API accepts.
PAGE_S = 10


def _camera(client, sandbox, config, label):
    return client.create_camera(
        label=label,
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name(label)}",
        ip_address=config.fakecam_ip,
    )


@pytest.fixture
def two_page_tour(client, sandbox, config):
    north = [_camera(client, sandbox, config, f"north{i}") for i in (1, 2)]
    south = [_camera(client, sandbox, config, "south1")]
    tour = client.post("/tours", json_body={
        "name": sandbox.name("tour"),
        "pages": [
            {"label": "North", "layout_id": "2x2", "duration_s": PAGE_S,
             "camera_ids": [c["id"] for c in north]},
            {"label": "South", "layout_id": "1x1", "duration_s": PAGE_S,
             "camera_ids": [c["id"] for c in south]},
        ],
    }, expect=(201,)).json()
    sandbox.track(f"tour {tour['id']} ({tour['name']})",
                  lambda: client.delete(f"/tours/{tour['id']}", expect=None))
    return tour, north, south


def test_a_tour_turns_from_one_page_to_the_next(authed_page, two_page_tour):
    tour, north, south = two_page_tour
    authed_page.add_init_script(
        f"(() => {{ try {{ localStorage.setItem('{DISPLAY_ORDER_KEY}', '[7,0,9]') }} catch (e) {{}} }})()"
    )
    wall = TourPage(authed_page, tour["id"])
    errors = wall.console_errors()
    wall.open()

    expect(wall.find(S.TOUR_NAME).first).to_have_text(tour["name"], timeout=30_000)
    eventually(lambda: wall.tile_names() == [c["name"] for c in north],
               budget=BUDGETS.QUICK, describe="the North cameras on the wall")
    assert wall.page_label() == "North"

    eventually(lambda: wall.tile_names() == [c["name"] for c in south],
               budget=PAGE_S + BUDGETS.QUICK, describe="the wall turning to the South cameras")
    assert wall.page_label() == "South"

    assert not fatal_errors(errors), f"the tour threw: {errors[:3]}"
    # The tour never rewrote the operator's own Live View grid.
    saved = authed_page.evaluate(f"localStorage.getItem('{DISPLAY_ORDER_KEY}')")
    assert saved == "[7,0,9]"


def test_pause_holds_the_wall_and_next_moves_it_on(authed_page, two_page_tour):
    tour, north, south = two_page_tour
    wall = TourPage(authed_page, tour["id"])
    wall.open()
    eventually(lambda: wall.tile_names() == [c["name"] for c in north],
               budget=BUDGETS.QUICK, describe="the North cameras on the wall")

    wall.press(S.TOUR_PAUSE)
    expect(wall.find(S.TOUR_RESUME).first).to_be_visible()
    time.sleep(PAGE_S + 3)  # longer than the page: a running wall would have turned
    assert wall.tile_names() == [c["name"] for c in north]

    wall.press(S.TOUR_NEXT)
    eventually(lambda: wall.tile_names() == [c["name"] for c in south],
               budget=BUDGETS.QUICK, describe="Next moving the wall to the South cameras")
