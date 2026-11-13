from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from services import mediamtx_admin_service as admin_service
from routers.mediamtx_admin import mtx_pathdefaults_patch


DURATION_FIELDS = (
    "recordSegmentDuration",
    "recordPartDuration",
    "sourceOnDemandStartTimeout",
    "sourceOnDemandCloseAfter",
)


@pytest.mark.parametrize("field", DURATION_FIELDS)
@pytest.mark.parametrize("value", ["1h0m0s", "250ms", "1d", "", "0", "-2s"])
def test_valid_mediamtx_duration_values(field: str, value: str) -> None:
    admin_service._validate_pathdefaults_durations({field: value})


@pytest.mark.parametrize("field", DURATION_FIELDS)
@pytest.mark.parametrize("value", ["tomorrow", "10seconds", "1w", "1d2w", " ", "+", "-", 10, True])
def test_invalid_mediamtx_duration_values_identify_field(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        admin_service._validate_pathdefaults_durations({field: value})


def test_only_segment_duration_has_the_one_day_upper_bound() -> None:
    admin_service._validate_pathdefaults_durations({"recordSegmentDuration": "24h"})
    with pytest.raises(ValueError, match="maximum segment duration is 1 day"):
        admin_service._validate_pathdefaults_durations({"recordSegmentDuration": "24h1ns"})
    admin_service._validate_pathdefaults_durations({"recordSegmentDuration": "-25h"})
    admin_service._validate_pathdefaults_durations({"recordPartDuration": "7d"})


@pytest.mark.parametrize("value", ["\u0661s", "\uff11s"])
def test_duration_parser_rejects_non_ascii_digits(value: str) -> None:
    with pytest.raises(ValueError):
        admin_service._parse_mediamtx_duration_ns(value)


@pytest.mark.parametrize("value, expected", [("0.9ns", 0), ("1.9ns", 1), ("1.9us", 1_900)])
def test_fractional_nanoseconds_truncate_toward_zero(value: str, expected: int) -> None:
    assert admin_service._parse_mediamtx_duration_ns(value) == expected


def test_fractional_digit_limit_preserves_truncation_at_boundary() -> None:
    value = f"1.{'9' * admin_service._MAX_DURATION_FRACTION_DIGITS}ns"
    assert admin_service._parse_mediamtx_duration_ns(value) == 1


def test_fractional_digit_limit_rejects_values_above_boundary() -> None:
    value = f"1.{'9' * (admin_service._MAX_DURATION_FRACTION_DIGITS + 1)}ns"
    with pytest.raises(ValueError, match="fraction exceeds 1000 digits"):
        admin_service._parse_mediamtx_duration_ns(value)


@pytest.mark.parametrize("value, expected", [("1d2h", 26 * 3_600_000_000_000), ("-1d2h", -26 * 3_600_000_000_000), ("1d-2h", 22 * 3_600_000_000_000)])
def test_day_prefix_combinations(value: str, expected: int) -> None:
    assert admin_service._parse_mediamtx_duration_ns(value) == expected


@pytest.mark.parametrize("value", [" 1s", "1s ", "1 s", "1\ts"])
def test_duration_parser_rejects_whitespace(value: str) -> None:
    with pytest.raises(ValueError):
        admin_service._parse_mediamtx_duration_ns(value)


@pytest.mark.parametrize("value, expected", [("9223372036854775807ns", (1 << 63) - 1), ("-9223372036854775808ns", -(1 << 63))])
def test_duration_parser_accepts_signed_range_boundaries(value: str, expected: int) -> None:
    assert admin_service._parse_mediamtx_duration_ns(value) == expected


@pytest.mark.parametrize("value", ["9223372036854775808ns", "-9223372036854775809ns", "106752d", "-106752d"])
def test_duration_parser_rejects_signed_range_overflow(value: str) -> None:
    with pytest.raises(ValueError, match="range"):
        admin_service._parse_mediamtx_duration_ns(value)


@pytest.mark.asyncio
async def test_pathdefaults_duration_validation_precedes_upstream_request(monkeypatch) -> None:
    class UnexpectedRequest:
        def __init__(self, *args, **kwargs):
            raise AssertionError("invalid durations must be rejected before HTTP")

    monkeypatch.setattr(admin_service.httpx, "AsyncClient", UnexpectedRequest)
    with pytest.raises(ValueError, match="recordPartDuration"):
        await admin_service.MediaMtxAdminService.pathdefaults_patch(
            {"recordPartDuration": "3fortnights"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", DURATION_FIELDS)
async def test_valid_duration_patch_reaches_upstream(monkeypatch, field: str) -> None:
    payload = {field: "1h" if field == "recordSegmentDuration" else "10s"}
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def patch(self, url, *, json, headers):
            calls.append((url, json, headers))
            return httpx.Response(200, json={"updated": True})

    monkeypatch.setattr(admin_service.MediaMtxAdminService, "is_configured", lambda: True)
    monkeypatch.setattr(admin_service.MediaMtxAdminService, "_base", lambda: "http://mediamtx/v3")
    monkeypatch.setattr(admin_service.MediaMtxAdminService, "_headers", lambda: {})
    monkeypatch.setattr(admin_service.httpx, "AsyncClient", FakeClient)

    result = await admin_service.MediaMtxAdminService.pathdefaults_patch(payload)
    assert result["status"] == "ok"
    assert calls and calls[0][1] == payload


@pytest.mark.asyncio
async def test_pathdefaults_route_returns_422_without_upstream_request(monkeypatch) -> None:
    calls = []

    class UnexpectedRequest:
        def __init__(self, *args, **kwargs):
            calls.append("called")
            raise AssertionError("invalid durations must be rejected before HTTP")

    monkeypatch.setattr(admin_service.MediaMtxAdminService, "is_configured", lambda: True)
    monkeypatch.setattr(admin_service.httpx, "AsyncClient", UnexpectedRequest)
    with pytest.raises(HTTPException) as exc:
        await mtx_pathdefaults_patch({"recordPartDuration": "3fortnights"})
    assert exc.value.status_code == 422
    assert "recordPartDuration" in exc.value.detail
    assert calls == []


@pytest.mark.asyncio
async def test_pathdefaults_route_reports_upstream_rejection(monkeypatch) -> None:
    class RejectingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def patch(self, url, *, json, headers):
            return httpx.Response(400, json={"error": "record duration rejected"})

    monkeypatch.setattr(admin_service.MediaMtxAdminService, "is_configured", lambda: True)
    monkeypatch.setattr(admin_service.MediaMtxAdminService, "_base", lambda: "http://mediamtx/v3")
    monkeypatch.setattr(admin_service.MediaMtxAdminService, "_headers", lambda: {})
    monkeypatch.setattr(admin_service.httpx, "AsyncClient", RejectingClient)
    with pytest.raises(HTTPException) as exc:
        await mtx_pathdefaults_patch({"recordPartDuration": "1s"})
    assert exc.value.status_code == 502
    assert "record duration rejected" in exc.value.detail


def test_pathdefaults_patch_failure_reports_missing_admin_api() -> None:
    assert admin_service._pathdefaults_patch_failure({
        "status": "no_admin_api",
        "message": "mediamtx_admin_api not configured",
    }) == (
        503,
        "mediamtx_admin_api not configured",
    )
    assert admin_service._pathdefaults_patch_failure({"status": "ok"}) is None
