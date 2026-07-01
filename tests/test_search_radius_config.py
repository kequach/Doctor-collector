from __future__ import annotations

import pytest
from pydantic import ValidationError

from doctor_collector.config import AppConfig, TherapieConfig, load_config


def test_loads_search_radius_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: "60320"
  search_radius_km: 50
""",
        encoding="utf-8",
    )

    config = load_config(config_path, apply_env_overrides=False)

    assert config.therapie.search_radius_km == 50


def test_loads_search_radius_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_SEARCH_RADIUS_KM", "25")

    config = load_config(tmp_path / "missing.yaml")

    assert config.therapie.search_radius_km == 25


def test_loads_max_therapists_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_MAX_THERAPISTS", "7")

    config = load_config(tmp_path / "missing.yaml")

    assert config.therapie.max_therapists == 7


def test_rejects_unsupported_search_radius():
    with pytest.raises(ValidationError, match="search_radius_km must be one of: 10, 25, 50, 100"):
        TherapieConfig(search_radius_km=20)


def test_rejects_zero_request_delay():
    with pytest.raises(ValidationError, match="greater than or equal to 0.1"):
        TherapieConfig(request_delay_seconds=0)


def test_request_delay_defaults_to_one_and_a_half_seconds():
    assert TherapieConfig().request_delay_seconds == 1.5


def test_max_therapists_defaults_to_no_limit_and_rejects_negative_values():
    assert TherapieConfig().max_therapists == 0

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        TherapieConfig(max_therapists=-1)


def test_start_url_includes_search_radius():
    from doctor_collector.clients.therapie import TherapieClient

    config = AppConfig(
        therapie=TherapieConfig(
            post_code="60320",
            search_radius_km=100,
            therapy_form=1,
            therapy_type=2,
            start_page=3,
        ),
    )
    client = TherapieClient(config)

    try:
        assert client._build_start_url() == (
            "https://www.therapie.de/therapeutensuche/ergebnisse/"
            "?ort=60320&page=3&search_radius=100&therapieangebot=1&verfahren=2"
        )
    finally:
        import asyncio

        asyncio.run(client.aclose())
