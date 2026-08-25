

# ── retries must bridge a warming adapter, not a missing one ────────

def _client_with(status: int, calls: dict, retries: int = 2):
    import httpx

    from adapter_clients import KaicAdapterClient as _C

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] = calls.get("n", 0) + 1
        return httpx.Response(status, json={"detail": "x"})

    c = _C(kaic_url="http://kaic", api_key="k", adapter_name="insightface",
           retries=retries, retry_backoff_s=0.0)
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def test_a_404_is_not_retried():
    """The retry exists for an adapter that is still warming up. A 404 means
    the route does not exist — not deployed, or a typo'd recognition_adapter —
    and cannot become success by waiting. Seen in the field as insightface
    404s repeating three times on every person visit, on a stack with no face
    adapter installed."""
    import asyncio

    import httpx
    import pytest

    calls: dict = {}
    c = _client_with(404, calls)
    with pytest.raises(httpx.HTTPError):
        asyncio.run(c.infer(frame_jpeg=b"x"))
    assert calls.get("n") == 1, f"404 attempted {calls.get('n')} times"


def test_a_503_is_still_retried():
    """A warming adapter is exactly what the retry is for."""
    import asyncio

    import httpx
    import pytest

    calls: dict = {}
    c = _client_with(503, calls)
    with pytest.raises(httpx.HTTPError):
        asyncio.run(c.infer(frame_jpeg=b"x"))
    assert calls.get("n") == 3, f"503 attempted only {calls.get('n')} times"
