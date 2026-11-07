"""The RFC-0002 surface core probes, and the catalog registration."""
import asyncio

import httpx
import respx

from attendly.config import Settings
from attendly.manifest import app_manifest, app_ui_url


def test_health_manifest_state(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["ready"] is True and h["app"] == "attendly"
    m = client.get("/manifest").json()
    assert m["id"] == "attendly" and m["has_ui"] is True
    assert m["ui_mode"] == "external"
    assert m["ui_url"] == "http://{host}:9300/"
    assert m["website"] == m["ui_url"]
    assert m["camera_picker"] is False and "insightface" in m["requires_adapters"]
    s = client.get("/state").json()
    assert s["students"] == 0 and s["rooms"] == 0 and s["active_classes"] == 0 and s["ui_url"] == m["ui_url"]


def test_manifest_is_a_valid_sdk_manifest_and_public_url_wins():
    from opennvr_app_sdk import AppManifest

    settings = Settings(public_url="https://attendance.school.example", _env_file=None)
    m = app_manifest(settings)
    assert m["ui_url"] == "https://attendance.school.example/"
    assert app_ui_url(Settings(port=9301, _env_file=None)) == "http://{host}:9301/"
    # Round-trips through the SDK dataclass: the same validation core applies
    AppManifest(id=m["id"], name=m["name"], version=m["version"], category=m["category"], has_ui=True,
                ui_mode=m["ui_mode"], ui_url=m["ui_url"])


@respx.mock
def test_self_registration_uses_site_key_and_contract_url(env, monkeypatch):
    import attendly_app

    monkeypatch.setenv("ATTENDLY_CONTRACT_URL", "http://attendly:9300")
    from attendly.config import reset_settings_cache

    reset_settings_cache()
    settings = attendly_app.get_settings()
    route = respx.post("http://core.test/api/v1/apps/register").mock(return_value=httpx.Response(200, json={"ok": True}))
    ok = asyncio.run(attendly_app.register_with_app_catalog(settings, attempts=1, delay=0))
    assert ok and route.called
    import json

    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["url"] == "http://attendly:9300"
    assert body["manifest"]["id"] == "attendly" and body["manifest"]["ui_mode"] == "external"
    assert req.headers["X-Internal-Api-Key"] == "site-key"
    assert req.headers["Authorization"] == "Bearer site-key"


@respx.mock
def test_self_registration_gives_up_on_4xx_and_retries_on_outage(env):
    import attendly_app

    settings = attendly_app.get_settings()
    route = respx.post("http://core.test/api/v1/apps/register").mock(return_value=httpx.Response(400, json={"detail": "bad"}))
    assert asyncio.run(attendly_app.register_with_app_catalog(settings, attempts=3, delay=0)) is False
    assert route.call_count == 1
    route.mock(side_effect=httpx.ConnectError("down"))
    assert asyncio.run(attendly_app.register_with_app_catalog(settings, attempts=2, delay=0)) is False
    assert route.call_count == 3


def test_spa_fallback_serves_index_when_built(env, tmp_path, monkeypatch):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>attendly</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setenv("ATTENDLY_WEB_DIR", str(web))
    from fastapi.testclient import TestClient

    import attendly_app

    with TestClient(attendly_app.build_app(start_engine=False, register=False)) as c:
        assert c.get("/").text == "<html>attendly</html>"
        assert c.get("/manage-rooms").text == "<html>attendly</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/health").json()["status"] == "ok"
        # API paths never get the page: the slash-less spelling the UI uses
        # is redirected to the router, and an unknown API path is a 404
        r = c.get("/courses", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].endswith("/courses/")
        assert c.get("/courses").json() == []
        assert c.get("/courses/?x=1", follow_redirects=False).status_code == 200
        assert c.get("/students/nothing/here/").status_code == 404
