from __future__ import annotations

import json

import pytest

from doctor_collector.config import AppConfig, TherapieConfig
from doctor_collector.models.therapist import TherapistProfile
from doctor_collector.services.collector import TherapistCollector


@pytest.mark.asyncio
async def test_collect_overwrites_existing_csv_when_no_profiles_match(tmp_path):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Stale,old@example.com,Type,,https://example.test/old\n",
        encoding="utf-8",
    )

    class FakeClient:
        last_crawl_completed = True

        async def fetch_therapist_listings(self):
            return []

        async def aclose(self):
            pass

    collector = TherapistCollector(
        AppConfig(therapie=TherapieConfig(post_code="10115")),
        csv_file=csv_path,
        state_file=tmp_path / ".contacted_therapists.json",
    )
    await collector._client.aclose()
    collector._client = FakeClient()

    try:
        result = await collector.collect()
    finally:
        await collector.close()

    assert result.total_matching == 0
    assert collector.last_csv_saved is True
    assert csv_path.read_text(encoding="utf-8") == (
        "name,email,therapist_type,website,profile_url\n"
    )


@pytest.mark.asyncio
async def test_collect_preserves_existing_csv_when_empty_crawl_did_not_complete(tmp_path):
    csv_path = tmp_path / "therapists.csv"
    stale_csv = (
        "name,email,therapist_type,website,profile_url\n"
        "Stale,old@example.com,Type,,https://example.test/old\n"
    )
    csv_path.write_text(stale_csv, encoding="utf-8")

    class FakeClient:
        last_crawl_completed = False

        async def fetch_therapist_listings(self):
            return []

        async def aclose(self):
            pass

    collector = TherapistCollector(
        AppConfig(therapie=TherapieConfig(post_code="10115")),
        csv_file=csv_path,
        state_file=tmp_path / ".contacted_therapists.json",
    )
    await collector._client.aclose()
    collector._client = FakeClient()

    try:
        result = await collector.collect()
    finally:
        await collector.close()

    assert result.total_matching == 0
    assert collector.last_csv_saved is False
    assert csv_path.read_text(encoding="utf-8") == stale_csv


@pytest.mark.asyncio
async def test_collect_preserves_existing_csv_when_partial_crawl_did_not_complete(tmp_path):
    csv_path = tmp_path / "therapists.csv"
    stale_csv = (
        "name,email,therapist_type,website,profile_url\n"
        "Reviewed,old@example.com,Type,,https://example.test/old\n"
    )
    csv_path.write_text(stale_csv, encoding="utf-8")

    class FakeClient:
        last_crawl_completed = False

        async def fetch_therapist_listings(self):
            return [
                TherapistProfile(
                    name="Partial",
                    email="partial@example.com",
                    therapist_type="Type",
                    profile_url="https://example.test/partial",
                ),
            ]

        async def aclose(self):
            pass

    collector = TherapistCollector(
        AppConfig(therapie=TherapieConfig(post_code="10115")),
        csv_file=csv_path,
        state_file=tmp_path / ".contacted_therapists.json",
    )
    await collector._client.aclose()
    collector._client = FakeClient()

    try:
        result = await collector.collect()
    finally:
        await collector.close()

    assert result.total_matching == 1
    assert collector.last_csv_saved is False
    assert csv_path.read_text(encoding="utf-8") == stale_csv


@pytest.mark.asyncio
async def test_collector_default_paths_follow_current_env(tmp_path, monkeypatch):
    csv_path = tmp_path / "env-therapists.csv"
    state_path = tmp_path / "env-state.json"
    monkeypatch.setenv("CSV_FILE", str(csv_path))
    monkeypatch.setenv("STATE_FILE", str(state_path))

    collector = TherapistCollector(
        AppConfig(therapie=TherapieConfig(post_code="10115")),
    )

    try:
        assert collector.csv_path == csv_path
        collector.mark_contacted({"new@example.com"})
    finally:
        await collector.close()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["contacted_emails"] == ["new@example.com"]
