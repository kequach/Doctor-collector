from __future__ import annotations

import asyncio
import logging
import threading

import httpx
import pytest

from doctor_collector.clients.therapie import (
    TherapieClient,
    TherapieRateLimitError,
    TherapieRequestError,
    TherapieStopRequested,
)
from doctor_collector.config import AppConfig, TherapieConfig
from doctor_collector.models.therapist import TherapistProfile


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
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
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
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        with pytest.raises(TherapieRateLimitError, match="HTTP 429"):
            await client._get("https://www.therapie.de/test")
    finally:
        await client.aclose()

    assert calls == 4


@pytest.mark.asyncio
async def test_rate_limit_wait_can_be_stopped_without_polling_sleep():
    calls = 0
    first_response_seen = asyncio.Event()
    stop_event = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        first_response_seen.set()
        return httpx.Response(429, headers={"Retry-After": "30"}, request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
        stop_requested=stop_event.is_set,
        stop_wait=stop_event.wait,
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        task = asyncio.create_task(client._get("https://www.therapie.de/test"))
        await asyncio.wait_for(first_response_seen.wait(), timeout=1)
        stop_event.set()
        with pytest.raises(TherapieStopRequested):
            await asyncio.wait_for(task, timeout=1)
    finally:
        await client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_request_delay_wait_can_be_stopped_before_next_request():
    stop_event = threading.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok", request=request)

    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(request_delay_seconds=30)),
        stop_requested=stop_event.is_set,
        stop_wait=stop_event.wait,
    )
    await _replace_transport(client, httpx.MockTransport(handler))

    try:
        response = await client._get("https://www.therapie.de/test")
        assert response.status_code == 200
        task = asyncio.create_task(client._get("https://www.therapie.de/test"))
        stop_event.set()
        with pytest.raises(TherapieStopRequested):
            await asyncio.wait_for(task, timeout=1)
    finally:
        await client.aclose()

    assert calls == 1


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
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
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
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
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
        AppConfig(therapie=TherapieConfig(request_delay_seconds=0.1)),
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


@pytest.mark.asyncio
async def test_fetch_listings_marks_crawl_incomplete_when_profiles_are_skipped(monkeypatch):
    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(post_code="10115", request_delay_seconds=0.1)),
    )

    async def parse_listing_page(_url: str):
        return ["https://www.therapie.de/profil/test/"], None

    async def fetch_profiles_batch(_urls: list[str]):
        return []

    monkeypatch.setattr(client, "_parse_listing_page", parse_listing_page)
    monkeypatch.setattr(client, "_fetch_profiles_batch", fetch_profiles_batch)

    try:
        profiles = await client.fetch_therapist_listings()
    finally:
        await client.aclose()

    assert profiles == []
    assert client.last_crawl_completed is False


@pytest.mark.asyncio
async def test_fetch_listings_marks_empty_first_page_as_incomplete(monkeypatch):
    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(post_code="10115", request_delay_seconds=0.1)),
    )

    async def parse_listing_page(_url: str):
        return [], None

    monkeypatch.setattr(client, "_parse_listing_page", parse_listing_page)

    try:
        profiles = await client.fetch_therapist_listings()
    finally:
        await client.aclose()

    assert profiles == []
    assert client.last_crawl_completed is False


@pytest.mark.asyncio
async def test_fetch_listings_limits_gathered_therapists(monkeypatch):
    parsed_pages: list[str] = []
    fetched_batches: list[list[str]] = []
    client = TherapieClient(
        AppConfig(
            therapie=TherapieConfig(
                post_code="10115",
                max_therapists=2,
                request_delay_seconds=0.1,
            ),
        ),
    )

    async def parse_listing_page(url: str):
        parsed_pages.append(url)
        return [
            "https://www.therapie.de/profil/ada/",
            "https://www.therapie.de/profil/grace/",
            "https://www.therapie.de/profil/katherine/",
        ], "https://www.therapie.de/page/2"

    async def fetch_profiles_batch(urls: list[str]):
        fetched_batches.append(urls)
        return [
            TherapistProfile(
                name=url.rstrip("/").rsplit("/", 1)[-1],
                email=f"{index}@example.com",
                therapist_type="Type",
                profile_url=url,
            )
            for index, url in enumerate(urls, start=1)
        ]

    monkeypatch.setattr(client, "_parse_listing_page", parse_listing_page)
    monkeypatch.setattr(client, "_fetch_profiles_batch", fetch_profiles_batch)

    try:
        profiles = await client.fetch_therapist_listings()
    finally:
        await client.aclose()

    assert [profile.name for profile in profiles] == ["ada", "grace"]
    assert len(parsed_pages) == 1
    assert fetched_batches == [
        [
            "https://www.therapie.de/profil/ada/",
            "https://www.therapie.de/profil/grace/",
        ],
    ]
    assert client.last_crawl_completed is True


@pytest.mark.asyncio
async def test_fetch_listings_stops_after_current_page_when_requested(monkeypatch):
    stop = False
    parsed_pages: list[str] = []
    client = TherapieClient(
        AppConfig(therapie=TherapieConfig(post_code="10115", request_delay_seconds=0.1)),
        stop_requested=lambda: stop,
    )

    async def parse_listing_page(url: str):
        parsed_pages.append(url)
        return ["https://www.therapie.de/profil/test/"], "https://www.therapie.de/page/2"

    async def fetch_profiles_batch(_urls: list[str]):
        nonlocal stop
        stop = True
        return [
            TherapistProfile(
                name="Ada",
                email="ada@example.com",
                therapist_type="Type",
                profile_url="https://www.therapie.de/profil/test/",
            )
        ]

    monkeypatch.setattr(client, "_parse_listing_page", parse_listing_page)
    monkeypatch.setattr(client, "_fetch_profiles_batch", fetch_profiles_batch)

    try:
        profiles = await client.fetch_therapist_listings()
    finally:
        await client.aclose()

    assert [profile.name for profile in profiles] == ["Ada"]
    assert len(parsed_pages) == 1
    assert client.last_crawl_completed is False
