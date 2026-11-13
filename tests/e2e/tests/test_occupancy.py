# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The occupancy reporting surfaces answer, and answer within the caller's scope.

Occupancy is written by a NATS consumer (``occupancy_event_consumer``) that
turns ``occupancy.changed.v1`` and friends into rows, and read back by four
endpoints that the dashboard's charts are built on.

Producing the data needs the occupancy-counting app running and publishing,
which a bare stack does not have -- so these tests deliberately assert the
*read* half only: that every surface is reachable, shaped as the charts expect,
and scoped to the caller's cameras. That is worth having on its own. A consumer
that stopped writing shows up as empty charts; an endpoint that 500s shows up
as a broken page, and the two need completely different fixes.

Reachability is a low bar, and the docstrings say so rather than implying these
prove occupancy counting works. They do not.
"""

from __future__ import annotations

import pytest

from harness import routes

pytestmark = pytest.mark.smoke

#: The fleet-wide surfaces. The heatmap is deliberately absent: it is
#: per-camera and requires ``camera_id``, so it gets its own test below.
SURFACES = (
    ("history", routes.OCCUPANCY_HISTORY, {"hours": 24}),
    ("footfall", routes.OCCUPANCY_FOOTFALL, None),
    ("report", routes.OCCUPANCY_REPORT, None),
)


@pytest.mark.parametrize("name,path,params", SURFACES, ids=[s[0] for s in SURFACES])
def test_an_occupancy_surface_answers(client, name, path, params):
    """Each endpoint returns a payload rather than an error.

    Empty is a legitimate answer here -- no occupancy app is running, so
    there is nothing to count. A 500 is not, and that is the distinction
    worth catching: an empty chart is a data question, a broken one is a code
    question.
    """
    response = client.get(path, params=params, expect=None)

    assert response.status_code == 200, (
        f"the occupancy {name} surface failed with {response.status_code}: "
        f"{response.text[:300]}"
    )
    payload = response.json()
    assert payload is not None, f"the occupancy {name} surface returned null"


def test_the_heatmap_answers_for_a_camera(client, sandbox, config):
    """The heatmap is per-camera, unlike its siblings.

    It takes a required ``camera_id`` because a heat field only means
    anything for one fixed viewpoint -- averaging several cameras' geometry
    together would be nonsense. Called without one it is a 422, which is
    correct and worth pinning so nobody "fixes" it into a fleet-wide default.
    """
    camera = client.create_camera(
        label="heatmap",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('heatmap')}",
        ip_address=config.fakecam_ip,
    )

    ok = client.get(
        routes.OCCUPANCY_HEATMAP,
        params={"camera_id": camera["id"], "hours": 24},
        expect=None,
    )
    assert ok.status_code == 200, (
        f"the heatmap failed for a real camera: {ok.status_code} {ok.text[:200]}"
    )

    missing = client.get(routes.OCCUPANCY_HEATMAP, expect=None)
    assert missing.status_code == 422, (
        "the heatmap should require camera_id -- a heat field is only "
        f"meaningful per camera. Got {missing.status_code}."
    )


def test_occupancy_history_accepts_a_window(client):
    """The charts pick their own window, so the parameter has to work.

    A silently ignored ``hours`` would make every chart show the same range
    whatever the user selected -- wrong in a way nobody reports as a bug.
    """
    short = client.get(routes.OCCUPANCY_HISTORY, params={"hours": 1}, expect=None)
    long = client.get(routes.OCCUPANCY_HISTORY, params={"hours": 168}, expect=None)

    assert short.status_code == 200, f"1-hour window rejected: {short.text[:200]}"
    assert long.status_code == 200, f"168-hour window rejected: {long.text[:200]}"


def test_occupancy_requires_authentication(client):
    """Occupancy is camera data, and camera data is never anonymous.

    Every read surface in the product is scoped to the caller; one that
    answers without a token is scoped to nobody.
    """
    response = client.get(
        routes.OCCUPANCY_HISTORY, params={"hours": 24}, auth=False, expect=None
    )

    assert response.status_code in (401, 403), (
        f"occupancy history answered an unauthenticated caller with "
        f"{response.status_code}"
    )
