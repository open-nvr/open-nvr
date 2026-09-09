# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The live-event stream must not leak other people's cameras.

Reported by Kamal Sentassi (S9S Security Research), coordinated
disclosure, 2026: ``/events/ws`` authenticated the CONNECTION but never
the SUBSCRIPTION. ``camera_id`` came from the query string unchecked,
and omitting it made ``_Subscriber.matches()`` true for every event — so
any active account could stream every camera's detections and alerts.

These pin the fix at the bus, which is where it belongs: entitlement is
matched per event, so no caller can widen its own scope by leaving a
query parameter off.
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services.event_bus_service import _Subscriber  # noqa: E402


def _sub(camera_id=None, tasks=None, allowed=None):
    return _Subscriber(
        queue_size=8,
        camera_id=camera_id,
        tasks=frozenset(tasks) if tasks else None,
        allowed_camera_ids=None if allowed is None else frozenset(allowed),
    )


def _ev(camera_id, task="person_detection"):
    return {"event_type": "inference_result", "camera_id": camera_id, "task": task}


def test_omitting_camera_id_no_longer_means_every_camera():
    """THE reported vector: subscribe with no camera_id and receive the
    whole site. Entitlement is now applied even when no filter is set."""
    sub = _sub(camera_id=None, allowed={1, 2})
    assert sub.matches(_ev(1)) is True
    assert sub.matches(_ev(2)) is True
    assert sub.matches(_ev(3)) is False


def test_asking_for_someone_elses_camera_matches_nothing():
    """Belt and braces: the route refuses this with a 1008 close, but if
    a future caller forgets that check the bus must still not serve it."""
    sub = _sub(camera_id=3, allowed={1, 2})
    assert sub.matches(_ev(3)) is False


def test_entitlement_cannot_be_widened_by_the_client_filter():
    sub = _sub(camera_id=None, allowed={1})
    for cam in (2, 3, 99):
        assert sub.matches(_ev(cam)) is False


def test_an_event_with_no_camera_is_not_leaked_to_a_scoped_subscriber():
    """A system-wide event carries no camera_id. It must not fall through
    the entitlement check just because there is nothing to compare."""
    sub = _sub(allowed={1})
    assert sub.matches({"event_type": "system_alert"}) is False
    assert sub.matches(_ev(None)) is False


def test_superuser_is_unrestricted():
    """None means 'no restriction' — visible_camera_ids returns that for
    a superuser, and internal callers that authorized themselves."""
    sub = _sub(allowed=None)
    assert sub.matches(_ev(1)) is True
    assert sub.matches(_ev(999)) is True
    assert sub.matches({"event_type": "system_alert"}) is True


def test_empty_entitlement_matches_nothing():
    """A user with no cameras is not a user with all cameras."""
    sub = _sub(allowed=set())
    assert sub.matches(_ev(1)) is False
    assert sub.matches(_ev(None)) is False


def test_task_filter_still_applies_within_the_entitlement():
    sub = _sub(tasks=["person_detection"], allowed={1, 2})
    assert sub.matches(_ev(1, "person_detection")) is True
    assert sub.matches(_ev(1, "plate_recognition")) is False
    assert sub.matches(_ev(3, "person_detection")) is False
