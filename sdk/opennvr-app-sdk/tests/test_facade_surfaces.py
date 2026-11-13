# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Everything an app needs, without leaving the facade.

An app is more than its rule. All fourteen example apps in this
repository declare a dashboard; two declare operator actions; one serves
its own HTML; three apply config changes live. Those used to be method
overrides on a base class, which meant the facade could *declare* them
and never implement them. These tests pin the decorators that close
that gap — each one registers the manifest entry and the implementation
together, which is the facade's whole thesis.
"""
from __future__ import annotations

import datetime as dt

import pytest

from opennvr_app_sdk import App, Entitlement, Param, setting
from opennvr_app_sdk.testing import (
    FakeCore, RecorderChannel, app_config, detection, feed, inference_event,
)


def at(seconds: float) -> str:
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def build(app: App, **cfg):
    recorder = RecorderChannel()
    return app.build(app_config(**cfg), recorder.dispatcher()), recorder


# ── The dashboard ───────────────────────────────────────────────────


def test_store_alone_is_a_complete_dashboard():
    """Declare a tile, keep a number in the store, and the catalog has
    something to render — with no /state implementation at all."""
    app = App("demo").metric("alerted", label="Alerts fired")

    @app.on_setup()
    def prepare(cfg):
        app.store["alerted"] = 0

    @app.on_detection("person")
    def rule(event):
        event.alert("x")
        app.store["alerted"] += 1

    det, _ = build(app)
    (view,) = app.manifest().state_schema
    assert (view.kind, view.path, view.label) == ("metric", "alerted", "Alerts fired")
    det.handle_event(inference_event(detection("person"), detection("person")))
    assert det.state_snapshot() == {"alerted": 2}


def test_state_adds_computed_keys_on_top_of_the_store():
    app = App("demo").metric("seen")

    @app.on_setup()
    def prepare(cfg):
        app.store["seen"] = 3

    @app.on_detection("person")
    def rule(event):
        pass

    @app.state()
    def extra():
        return {"ready": True}

    det, _ = build(app)
    assert det.state_snapshot() == {"seen": 3, "ready": True}


def test_a_raising_state_function_does_not_break_the_endpoint():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        pass

    @app.on_setup()
    def prepare(cfg):
        app.store["ok"] = 1

    @app.state()
    def broken():
        raise RuntimeError("boom")

    det, _ = build(app)
    assert det.state_snapshot() == {"ok": 1}


@pytest.mark.parametrize("declare,kind,extra", [
    (lambda a: a.metric("n"), "metric", {}),
    (lambda a: a.gauge("n", min=0, max=10, warn=5), "gauge", {"max": 10, "warn": 5}),
    (lambda a: a.table("n", columns=["a", "b"]), "table", {"columns": ["a", "b"]}),
    (lambda a: a.log("n", limit=5), "log", {"limit": 5}),
    (lambda a: a.gallery("n", limit=4), "gallery", {"limit": 4}),
])
def test_every_tile_kind_declares_a_view(declare, kind, extra):
    app = App("demo")
    declare(app)
    (view,) = app.manifest().state_schema
    assert view.kind == kind and view.path == "n"
    for key, value in extra.items():
        assert getattr(view, key) == value


# ── Actions ─────────────────────────────────────────────────────────


def test_an_action_is_declared_and_implemented_in_one_place():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        pass

    @app.action("search", label="Search footage", confirm=True,
                params=[Param("query", str, required=True),
                        Param("hours", int, default=24)])
    def search(query: str = "", hours: int = 24) -> dict:
        """Find clips matching a description."""
        if not query.strip():
            raise ValueError("'query' is required")
        return {"hits": [query, hours]}

    (action,) = app.manifest().actions
    assert (action.name, action.label, action.confirm) == (
        "search", "Search footage", True)
    assert action.description == "Find clips matching a description."
    assert [p.name for p in action.params] == ["query", "hours"]

    det, _ = build(app)
    # Declared params arrive as keyword arguments, defaults filled in.
    assert det.on_action("search", {"query": "red car"}) == {"hits": ["red car", 24]}
    assert det.on_action("search", {"query": "van", "hours": 2}) == \
        {"hits": ["van", 2]}
    # The error contract the catalog relies on.
    with pytest.raises(ValueError):
        det.on_action("search", {"query": " "})
    with pytest.raises(KeyError):
        det.on_action("nope", {})


def test_action_names_must_be_usable_as_a_url_path():
    app = App("demo")
    with pytest.raises(ValueError, match="snake_case"):
        app.action("Search Footage")(lambda: None)
    app.action("search")(lambda: None)
    with pytest.raises(ValueError, match="already declared"):
        app.action("search")(lambda: None)


# ── The embedded UI ─────────────────────────────────────────────────


def test_declaring_a_ui_serves_one():
    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        pass

    @app.ui()
    def dashboard() -> str:
        return f"<h3>{app.store.get('seen', 0)} seen</h3>"

    assert app.manifest().has_ui is True
    det, _ = build(app)
    det.store["seen"] = 7
    assert det.ui_html() == "<h3>7 seen</h3>"


def test_an_app_without_a_ui_declares_none():
    app = App("demo")
    app.on_detection("person")(lambda event: None)
    assert app.manifest().has_ui is False


# ── Selling it ──────────────────────────────────────────────────────


def test_a_licence_handler_declares_the_gate_and_answers_it():
    """`validate` errors on entitlement="license_key" without a
    verifier, so before this a facade app could never be a paid app."""
    app = App("demo", pricing="paid", price_note="$29/camera/year")

    @app.on_detection("person")
    def rule(event):
        pass

    @app.on_license()
    def check(key: str):
        if key == "good-key":
            return Entitlement(valid=True, plan="pro", limits={"cameras": 10})
        return Entitlement(valid=False, message="This key is not for Demo.")

    assert app.manifest().entitlement == "license_key"
    det, _ = build(app)
    good = det.verify_license("good-key")
    assert good.valid and good.plan == "pro"
    assert det.verify_license("nope").message == "This key is not for Demo."


def test_a_licence_handler_may_just_return_a_bool():
    app = App("demo")
    app.on_detection("person")(lambda event: None)

    @app.on_license()
    def check(key: str) -> bool:
        return key.startswith("ok-")

    det, _ = build(app)
    assert det.verify_license("ok-1").valid is True
    verdict = det.verify_license("bad")
    assert verdict.valid is False and verdict.message


# ── Lifecycle ───────────────────────────────────────────────────────


def test_live_config_reaches_the_app():
    app = App("demo").param("threshold", float, default=0.5)
    seen = []

    @app.on_detection("person")
    def rule(event):
        pass

    @app.on_config()
    def changed(config):
        seen.append(app.config.threshold)

    det, _ = build(app, threshold=0.5)
    det.on_config_update({"threshold": 0.9})
    assert seen == [0.9]
    assert det.cfg.threshold == 0.9


def test_a_raising_config_hook_does_not_take_the_app_down():
    app = App("demo")
    app.on_detection("person")(lambda event: None)

    @app.on_config()
    def changed(config):
        raise RuntimeError("boom")

    det, _ = build(app)
    det.on_config_update({})               # no exception


def test_shutdown_hooks_run_and_sdk_resources_are_closed():
    app = App("demo")
    closed = []

    @app.on_detection("person")
    def rule(event):
        pass

    @app.on_shutdown()
    def cleanup():
        closed.append("app")

    det, _ = build(app)

    class _Closable:
        def close(self):
            closed.append("sdk")

    det._publisher = _Closable()
    det._facade_close()
    assert closed == ["app", "sdk"]


def test_a_raising_shutdown_hook_does_not_stop_the_others():
    app = App("demo")
    ran = []
    app.on_detection("person")(lambda event: None)
    app.on_shutdown()(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    app.on_shutdown()(lambda: ran.append("second"))
    det, _ = build(app)
    det._facade_close()
    assert ran == ["second"]


# ── The platform, from a rule ───────────────────────────────────────


def test_a_rule_can_reach_the_platform():
    app = App("demo")
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append([c.name for c in event.nvr.cameras()])
        event.nvr.state.set("last_camera", event.camera)

    with FakeCore(cameras=[{"name": "Front door"}]) as core:
        det, _ = build(app, opennvr_url=core.url, opennvr_token="test-key")
        det.handle_event(inference_event(detection("person")))
        assert seen == [["Front door"]]
        assert det.nvr.state.get("last_camera") == "cam-1"


def test_the_platform_client_is_built_once_and_closed_at_shutdown():
    app = App("demo")
    app.on_detection("person")(lambda event: None)
    with FakeCore() as core:
        det, _ = build(app, opennvr_url=core.url, opennvr_token="k")
        assert det.nvr is det.nvr
        det._facade_close()
        assert det._nvr is None


def test_publishing_a_domain_event_fills_in_the_envelope():
    app = App("demo").publishes("occupancy.changed.v1")
    published = []

    @app.on_detection("person")
    def rule(event):
        event.publish("occupancy.changed.v1", {"count": event.count("person"),
                                               "level": "normal"})

    det, _ = build(app)

    class _Recorder:
        def publish(self, schema, *, camera_id, payload, correlation_id=None):
            published.append((schema, camera_id, payload, correlation_id))
            return True

        def close(self):
            pass

    det._publisher = _Recorder()
    det.handle_event(inference_event(detection("person"), camera_id="cam-7",
                                     correlation_id="corr-9"))
    assert published == [("occupancy.changed.v1", "cam-7",
                          {"count": 1, "level": "normal"}, "corr-9")]


def test_the_publisher_carries_the_apps_identity():
    app = App("demo")
    app.on_detection("person")(lambda event: None)
    det, _ = build(app, nats_url="nats://test:4222")
    try:
        assert det.publisher._producer == "app:demo"
    finally:
        det.publisher.close()


# ── Config-bound filters ────────────────────────────────────────────


def test_a_filter_can_read_an_operator_setting():
    """A literal in a decorator is fixed at import. The whole point of a
    config form is that the operator moves the number."""
    app = App("demo").param("dwell_s", float, default=30.0)

    @app.on_detection("person", dwell="$dwell_s")
    def loitering(event):
        event.alert(f"after {event.dwell_s:.0f}s")

    det, _ = build(app, dwell_s=5.0)
    titles = []
    for second in (0, 3, 6):
        titles += [a.title for a in det.handle_event(inference_event(
            detection("person", track_id="t1"), completed_at=at(second)))]
    assert titles == ["after 6s"]


def test_setting_and_the_dollar_shorthand_are_the_same_thing():
    app = App("demo").param("floor", float, default=0.8)
    seen = []

    @app.on_detection("person", min_confidence=setting("floor"))
    def rule(event):
        seen.append(event.confidence)

    det, _ = build(app, floor=0.8)
    det.handle_event(inference_event(detection("person", confidence=0.5),
                                     detection("person", confidence=0.9)))
    assert seen == [0.9]


def test_a_filter_referring_to_an_undeclared_setting_says_so():
    app = App("demo")

    @app.on_detection("person", dwell="$nope")
    def rule(event):
        pass

    with pytest.raises(ValueError, match=r"app\.param\('nope'"):
        build(app)


def test_there_is_no_hidden_confidence_floor():
    """A rule that declares no min_confidence sees everything, so the
    only threshold in an app is the one it wrote down."""
    app = App("demo")
    seen = []

    @app.on_detection("person")
    def rule(event):
        seen.append(event.confidence)

    det, _ = build(app)
    det.handle_event(inference_event(detection("person", confidence=0.05)))
    assert seen == [0.05]


# ── Seeing anything at all ──────────────────────────────────────────


def test_tier0_is_consumed_by_default():
    """On a stock install the always-on Tier-0 detector is the ONLY
    stream on the bus. An app that ignores it registers, shows a green
    dot in the catalog and fires nothing, forever."""
    from opennvr_app_sdk.testing import tier0_event, tier0_track

    app = App("demo")

    @app.on_detection("person")
    def rule(event):
        event.alert(f"person on {event.camera}")

    assert app.config_class()().consume_tier0 is True
    det = app.build(app.config_class()(), RecorderChannel().dispatcher())
    fired = feed(det, tier0_event(tier0_track("person"), camera_id="cam-1"))
    assert [a.title for a in fired] == ["person on cam-1"]


def test_an_app_that_drives_its_own_adapter_can_opt_out():
    app = App("demo", consume_tier0=False)
    app.on_detection("person")(lambda event: None)
    assert app.config_class()().consume_tier0 is False


def test_published_events_appear_in_the_apps_asyncapi_document():
    app = App("demo").publishes("occupancy.changed.v1")
    app.on_detection("person")(lambda event: None)
    det, _ = build(app)
    spec = det.asyncapi_snapshot()
    channel = spec["channels"]["publishOccupancyChangedV1"]
    assert channel["address"] == "opennvr.events.occupancy.changed.v1.{camera_id}"
    assert spec["operations"]["sendOccupancyChangedV1"]["action"] == "send"
