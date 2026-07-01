"""Configuration loading and validation.

Reads ``config.yaml``, resolves ``${ENV_VAR}`` placeholders, and exposes
the result as typed Pydantic models.  The entire configuration can also be
supplied via environment variables when no YAML file is present.
"""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")
_DEFAULT_CONFIG_PATH = Path.cwd() / "config.yaml"
_SUPPORTED_SEARCH_RADII_KM = (10, 25, 50, 100)
_SECRET_PLACEHOLDER = "***"


def _resolve_env_vars(value: object) -> object:
    """Recursively replace ``${VAR}`` placeholders with ``os.environ[VAR]``."""
    if isinstance(value, str):

        def _replacer(match: re.Match[str]) -> str:
            var = match.group(1)
            resolved = os.environ.get(var, "")
            if not resolved:
                logger.warning("Environment variable %s is not set", var)
            return resolved

        return _ENV_VAR_RE.sub(_replacer, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Environment variable -> config mapping
# ---------------------------------------------------------------------------

_ENV_MAP: list[tuple[str, list[str], str]] = [
    ("THERAPIE_POST_CODE", ["therapie", "post_code"], "str"),
    ("THERAPIE_SEARCH_RADIUS_KM", ["therapie", "search_radius_km"], "int"),
    ("THERAPIE_THERAPY_FORM", ["therapie", "therapy_form"], "int"),
    ("THERAPIE_THERAPY_TYPE", ["therapie", "therapy_type"], "int"),
    ("THERAPIE_START_PAGE", ["therapie", "start_page"], "int"),
    ("THERAPIE_MAX_PAGES", ["therapie", "max_pages"], "int"),
    ("THERAPIE_MAX_THERAPISTS", ["therapie", "max_therapists"], "int"),
    ("THERAPIE_REQUEST_DELAY", ["therapie", "request_delay_seconds"], "float"),
    ("FILTER_EXCLUDE_TYPES", ["filters", "exclude_types"], "list"),
    ("CONTACT_SUBJECT", ["contact", "subject"], "str"),
    ("CONTACT_BODY", ["contact", "body"], "str"),
    ("CONTACT_SMTP_HOST", ["contact", "smtp_host"], "str"),
    ("CONTACT_SMTP_PORT", ["contact", "smtp_port"], "int"),
    ("CONTACT_USE_TLS", ["contact", "use_tls"], "bool"),
    ("CONTACT_SMTP_USER", ["contact", "smtp_user"], "str"),
    ("CONTACT_SMTP_PASSWORD", ["contact", "smtp_password"], "str"),
    ("CONTACT_FROM_ADDRESS", ["contact", "from_address"], "str"),
]


def _coerce(value: str, type_hint: str) -> Any:
    if type_hint == "int":
        return int(value)
    if type_hint == "float":
        return float(value)
    if type_hint == "bool":
        return value.strip().lower() in ("1", "true", "yes")
    if type_hint == "list":
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _set_nested(data: dict, keys: list[str], value: Any) -> None:
    for key in keys[:-1]:
        data = data.setdefault(key, {})
    data[keys[-1]] = value


def _config_from_env() -> dict:
    result: dict[str, Any] = {}
    for env_name, path, type_hint in _ENV_MAP:
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        _set_nested(result, path, _coerce(raw, type_hint))
    return result


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class TherapieConfig(BaseModel):
    post_code: str = ""
    search_radius_km: int = Field(default=10)
    therapy_form: int = Field(default=1, ge=1)
    therapy_type: int = Field(default=2, ge=1)
    start_page: int = Field(default=1, ge=1)
    max_pages: int = Field(default=100, ge=1)
    max_therapists: int = Field(default=0, ge=0)
    request_delay_seconds: float = Field(default=1.5, ge=0.1)

    @field_validator("search_radius_km")
    @classmethod
    def _validate_search_radius_km(cls, value: int) -> int:
        if value not in _SUPPORTED_SEARCH_RADII_KM:
            supported = ", ".join(str(radius) for radius in _SUPPORTED_SEARCH_RADII_KM)
            raise ValueError(f"search_radius_km must be one of: {supported}")
        return value


class FilterConfig(BaseModel):
    exclude_types: list[str] = Field(default_factory=lambda: ["Heil", "Kinder", "Privat"])


class ContactConfig(BaseModel):
    subject: str = "Erstgespräch Anfrage"
    body: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    use_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""


class AppConfig(BaseModel):
    therapie: TherapieConfig = Field(default_factory=TherapieConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    contact: ContactConfig = Field(default_factory=ContactConfig)

    @model_validator(mode="after")
    def _validate_post_code(self) -> "AppConfig":
        if self.therapie.post_code:
            self.therapie.post_code = self.therapie.post_code.strip()
        return self


def _config_from_raw(
    raw: object,
    *,
    apply_env_overrides: bool,
) -> AppConfig:
    if raw is None:
        resolved: dict[str, Any] = {}
    elif isinstance(raw, dict):
        resolved = _resolve_env_vars(raw)  # type: ignore[assignment]
    else:
        raise ValueError("config.yaml must contain a YAML mapping")

    if apply_env_overrides:
        env_overrides = _config_from_env()
        if env_overrides:
            resolved = _deep_merge(resolved, env_overrides)

    return AppConfig.model_validate(resolved)


def load_config_from_text(
    text: str,
    *,
    apply_env_overrides: bool = False,
) -> AppConfig:
    """Validate a config YAML document supplied as text."""
    raw = yaml.safe_load(text)
    return _config_from_raw(raw, apply_env_overrides=apply_env_overrides)


def read_config_text(path: Path | str | None = None) -> str:
    """Return raw config YAML text for editors."""
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if config_path.exists():
        return config_path.read_text(encoding="utf-8")

    return _dump_config_yaml(AppConfig())


def save_config_text(text: str, path: Path | str | None = None) -> AppConfig:
    """Validate and persist config YAML text."""
    config = load_config_from_text(text, apply_env_overrides=False)
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    output = text if text.endswith("\n") else f"{text}\n"
    config_path.write_text(output, encoding="utf-8")
    return config


def _read_raw_config_data(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _nested_config_value(data: dict[str, Any], keys: list[str]) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _raw_config_value(path: Path | str | None, keys: list[str]) -> Any:
    return _nested_config_value(_read_raw_config_data(path), keys)


def _raw_smtp_password(path: Path | str | None = None) -> str:
    password = _raw_config_value(path, ["contact", "smtp_password"])
    return password if isinstance(password, str) else ""


def _restore_raw_placeholder_values(
    data: dict[str, Any],
    path: Path | str | None = None,
    *,
    only_if_unchanged: bool = False,
) -> dict[str, Any]:
    restored = deepcopy(data)
    for _, keys, type_hint in _ENV_MAP:
        if type_hint != "str":
            continue

        raw_value = _raw_config_value(path, keys)
        if not isinstance(raw_value, str) or not _ENV_VAR_RE.search(raw_value):
            continue

        if only_if_unchanged and _nested_config_value(restored, keys) != raw_value:
            continue

        section = restored
        for key in keys[:-1]:
            next_section = section.setdefault(key, {})
            if not isinstance(next_section, dict):
                break
            section = next_section
        else:
            section[keys[-1]] = raw_value

    return restored


def config_to_public_data(
    config: AppConfig,
    *,
    raw_smtp_password: str = "",
) -> dict[str, Any]:
    """Return config data safe to embed in the local web UI."""
    data = config.model_dump(mode="json")
    if data["contact"]["smtp_password"] or raw_smtp_password:
        data["contact"]["smtp_password"] = _SECRET_PLACEHOLDER
    return data


def load_config_public_data(path: Path | str | None = None) -> dict[str, Any]:
    """Load config file data for the structured web UI."""
    data = config_to_public_data(
        load_config(path, apply_env_overrides=False),
        raw_smtp_password=_raw_smtp_password(path),
    )
    data = _restore_raw_placeholder_values(data, path)
    if data["contact"]["smtp_password"] or _raw_smtp_password(path):
        data["contact"]["smtp_password"] = _SECRET_PLACEHOLDER
    return data


def _prepare_config_data(data: dict[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("config data must be an object")

    restored = _restore_raw_placeholder_values(data, path, only_if_unchanged=True)
    contact = restored.setdefault("contact", {})
    if not isinstance(contact, dict):
        raise ValueError("contact config must be an object")

    if contact.get("smtp_password") == _SECRET_PLACEHOLDER:
        contact["smtp_password"] = _raw_smtp_password(path)

    return restored


def load_config_from_data(
    data: dict[str, Any],
    path: Path | str | None = None,
    *,
    apply_env_overrides: bool = False,
) -> AppConfig:
    """Validate structured config data from the web UI without persisting it."""
    restored = _prepare_config_data(data, path)
    return _config_from_raw(restored, apply_env_overrides=apply_env_overrides)


def save_config_data(data: dict[str, Any], path: Path | str | None = None) -> AppConfig:
    """Validate and persist structured config data from the web UI."""
    config = AppConfig.model_validate(_prepare_config_data(data, path))
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_dump_config_yaml(config), encoding="utf-8")
    return config


def _dump_config_yaml(config: AppConfig) -> str:
    data = config.model_dump(mode="json")
    dumped = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
    )
    dumped = dumped.replace(
        "  max_therapists:",
        "  # max_therapists: 0 bedeutet kein Limit.\n  max_therapists:",
        1,
    )
    return (
        "# Doctor Collector configuration\n"
        "# Edit this file directly, or run `python -m doctor_collector --web`.\n"
        + dumped
    )


def load_config(
    path: Path | str | None = None,
    *,
    apply_env_overrides: bool = True,
) -> AppConfig:
    """Load and validate configuration.

    Resolution order (later wins):
    1. Pydantic defaults
    2. ``config.yaml`` (with ``${VAR}`` placeholder substitution)
    3. Environment variables — only when *apply_env_overrides* is True
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    else:
        logger.info("No config file at %s — building config from env vars", config_path)
        raw = {}

    return _config_from_raw(raw, apply_env_overrides=apply_env_overrides)
