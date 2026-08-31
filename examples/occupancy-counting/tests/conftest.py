

import pytest


@pytest.fixture(autouse=True)
def _stub_domain_event_publisher(monkeypatch):
    """Unit tests must never dial NATS: the occupancy.changed.v1
    publisher is stubbed with a recorder. Tests that care read
    ``app._occupancy_publisher.calls``."""
    import occupancy_counting as oc

    class _StubPublisher:
        def __init__(self, url, *, token=None, producer="app"):
            self.url = url
            self.token = token
            self.producer = producer
            self.calls = []

        def publish(self, schema, *, camera_id, payload, correlation_id=None):
            self.calls.append({"schema": schema, "camera_id": camera_id,
                               "payload": payload})
            return True

        def close(self):  # pragma: no cover - symmetry
            pass

    monkeypatch.setattr(oc, "DomainEventPublisher", _StubPublisher)
