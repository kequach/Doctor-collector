from __future__ import annotations

import logging

import httpx
import pytest

from doctor_collector.clients.therapie import (
    TherapieClient,
    TherapieRateLimitError,
    TherapieRequestError,
)
from doctor_collector.config import AppConfig, TherapieConfig


async def _replace_transport(
    client: TherapieClient,
    transport: httpx.MockTransport,
) -> None:
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_get_retries_after_429_retry_after_header():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0)),
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        response = await client._get("https://www.therapie.de/test")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.text == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_get_raises_rate_limit_error_after_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0)),
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        with pytest.raises(TherapieRateLimitError, match="HTTP 429"):
            await client._get("https://www.therapie.de/test")
    finally:
        await client.aclose()

    assert calls == 4


@pytest.mark.asyncio
async def test_get_retries_request_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("TLS handshake failed", request=request)
        return httpx.Response(200, text="ok", request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0)),
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        response = await client._get("https://www.therapie.de/test")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.text == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_get_raises_request_error_after_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("TLS handshake failed", request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0)),
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        with pytest.raises(TherapieRequestError, match="failed after 3 retries"):
            await client._get("https://www.therapie.de/test")
    finally:
        await client.aclose()

    assert calls == 4


@pytest.mark.asyncio
async def test_profile_batch_logs_rate_limit_without_traceback(monkeypatch, caplog):
    async def rate_limited_profile(url: str):
        raise TherapieRateLimitError(f"therapie.de returned HTTP 429 for {url}")

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0)),
    )
    monkeypatch.setattr(client, "_extract_profile", rate_limited_profile)

    try:
        with caplog.at_level(logging.WARNING):
            profiles = await client._fetch_profiles_batch(["https://www.therapie.de/profil/test/"])
    finally:
        await client.aclose()

    assert profiles == []
    assert "Skipping profile after rate limit" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
