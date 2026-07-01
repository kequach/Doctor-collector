from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from doctor_collector.config import (
    load_config,
    load_config_from_data,
    load_config_from_text,
    load_config_public_data,
    save_config_data,
    save_config_text,
)


def test_save_config_text_validates_before_writing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  search_radius_km: 25
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="search_radius_km"):
        save_config_text(
            """
therapie:
  search_radius_km: 20
""",
            config_path,
        )

    assert "search_radius_km: 25" in config_path.read_text(encoding="utf-8")


def test_load_config_from_text_requires_mapping():
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config_from_text("- not: a mapping")


def test_save_config_data_restores_unchanged_secret_placeholder(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: "10115"
contact:
  smtp_password: "real-secret"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)
    assert data["contact"]["smtp_password"] == "***"
    data["therapie"]["post_code"] = "60320"

    save_config_data(data, config_path)

    saved = load_config(config_path, apply_env_overrides=False)
    assert saved.therapie.post_code == "60320"
    assert saved.contact.smtp_password == "real-secret"


def test_save_config_data_persists_new_secret(tmp_path):
    config_path = tmp_path / "config.yaml"

    save_config_data(
        {
            "contact": {
                "smtp_password": "new-secret",
            },
        },
        config_path,
    )

    saved = load_config(config_path, apply_env_overrides=False)
    assert saved.contact.smtp_password == "new-secret"


def test_save_config_data_preserves_env_secret_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTACT_SMTP_PASSWORD", "real-secret-from-env")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
contact:
  smtp_password: "${CONTACT_SMTP_PASSWORD}"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)
    assert data["contact"]["smtp_password"] == "***"
    data["contact"]["subject"] = "Changed"

    save_config_data(data, config_path)

    saved_text = config_path.read_text(encoding="utf-8")
    assert "${CONTACT_SMTP_PASSWORD}" in saved_text
    assert "real-secret-from-env" not in saved_text
    saved = load_config(config_path, apply_env_overrides=False)
    assert saved.contact.smtp_password == "real-secret-from-env"


def test_public_config_preserves_non_password_env_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_POST_CODE", "10115")
    monkeypatch.setenv("CONTACT_SMTP_USER", "real-user@example.com")
    monkeypatch.setenv("CONTACT_FROM_ADDRESS", "real-from@example.com")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: "${THERAPIE_POST_CODE}"
contact:
  smtp_user: "${CONTACT_SMTP_USER}"
  from_address: "${CONTACT_FROM_ADDRESS}"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)

    serialized = json.dumps(data)
    assert data["therapie"]["post_code"] == "${THERAPIE_POST_CODE}"
    assert data["contact"]["smtp_user"] == "${CONTACT_SMTP_USER}"
    assert data["contact"]["from_address"] == "${CONTACT_FROM_ADDRESS}"
    assert "real-user@example.com" not in serialized
    assert "real-from@example.com" not in serialized

    data["contact"]["subject"] = "Changed"
    save_config_data(data, config_path)

    saved_text = config_path.read_text(encoding="utf-8")
    assert "${THERAPIE_POST_CODE}" in saved_text
    assert "${CONTACT_SMTP_USER}" in saved_text
    assert "${CONTACT_FROM_ADDRESS}" in saved_text
    assert "real-user@example.com" not in saved_text
    assert "real-from@example.com" not in saved_text


def test_load_config_from_data_resolves_unchanged_env_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_POST_CODE", "10115")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: "${THERAPIE_POST_CODE}"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)
    config = load_config_from_data(data, config_path)

    assert data["therapie"]["post_code"] == "${THERAPIE_POST_CODE}"
    assert config.therapie.post_code == "10115"


def test_save_config_data_allows_replacing_env_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTACT_SMTP_USER", "real-user@example.com")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
contact:
  smtp_user: "${CONTACT_SMTP_USER}"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)
    data["contact"]["smtp_user"] = "new-user@example.com"

    save_config_data(data, config_path)

    saved_text = config_path.read_text(encoding="utf-8")
    assert "new-user@example.com" in saved_text
    assert "${CONTACT_SMTP_USER}" not in saved_text
    assert "real-user@example.com" not in saved_text


def test_save_config_data_handles_numeric_env_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_SEARCH_RADIUS_KM", "25")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: "10115"
  search_radius_km: "${THERAPIE_SEARCH_RADIUS_KM}"
""",
        encoding="utf-8",
    )

    data = load_config_public_data(config_path)

    assert data["therapie"]["search_radius_km"] == 25
    save_config_data(data, config_path)

    saved = load_config(config_path, apply_env_overrides=False)
    assert saved.therapie.search_radius_km == 25
