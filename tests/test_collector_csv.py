from __future__ import annotations

import json

import pytest

from doctor_collector.config import AppConfig, TherapieConfig
from doctor_collector.models.therapist import TherapistProfile
from doctor_collector.services.collector import (
    TherapistCollector,
    load_therapists_csv,
    save_therapists_csv,
)


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
        "name,email,therapist_type,website,profile_url,excluded\n"
    )


def test_load_csv_defaults_missing_excluded_column_to_active(tmp_path):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Active,active@example.com,Type,,https://example.test/active\n",
        encoding="utf-8",
    )

    [therapist] = load_therapists_csv(csv_path)

    assert therapist.email == "active@example.com"
    assert therapist.excluded is False


def test_save_and_load_csv_preserves_excluded_rows(tmp_path):
    csv_path = tmp_path / "therapists.csv"

    save_therapists_csv(
        csv_path,
        [
            TherapistProfile(
                name="Disabled",
                email="disabled@example.com",
                therapist_type="Type",
                profile_url="https://example.test/disabled",
                excluded=True,
            ),
        ],
    )

    text = csv_path.read_text(encoding="utf-8")
    [therapist] = load_therapists_csv(csv_path)

    assert text == (
        "name,email,therapist_type,website,profile_url,excluded\n"
        "Disabled,disabled@example.com,Type,,https://example.test/disabled,yes\n"
    )
    assert therapist.excluded is True


def test_save_csv_keeps_existing_file_when_atomic_replace_fails(tmp_path, monkeypatch):
    csv_path = tmp_path / "therapists.csv"
    original = (
        "name,email,therapist_type,website,profile_url\n"
        "Original,original@example.com,Type,,https://example.test/original\n"
    )
    csv_path.write_text(original, encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("doctor_collector.services.collector.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_therapists_csv(
            csv_path,
            [
                TherapistProfile(
                    name="New",
                    email="new@example.com",
                    therapist_type="Type",
                    profile_url="https://example.test/new",
                ),
            ],
        )

    assert csv_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".therapists.csv.*.tmp"))


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
