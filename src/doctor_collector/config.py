"""Configuration loading and validation.

Reads ``config.yaml``, resolves ``${ENV_VAR}`` placeholders, and exposes
the result as typed Pydantic models.  The entire configuration can also be
supplied via environment variables when no YAML file is present.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")
_DEFAULT_CONFIG_PATH = Path.cwd() / "config.yaml"


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
    ("THERAPIE_THERAPY_FORM", ["therapie", "therapy_form"], "int"),
    ("THERAPIE_THERAPY_TYPE", ["therapie", "therapy_type"], "int"),
    ("THERAPIE_START_PAGE", ["therapie", "start_page"], "int"),
    ("THERAPIE_MAX_PAGES", ["therapie", "max_pages"], "int"),
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
    therapy_form: int = Field(default=1, ge=1)
    therapy_type: int = Field(default=2, ge=1)
    start_page: int = Field(default=1, ge=1)
    max_pages: int = Field(default=100, ge=1)
    request_delay_seconds: float = Field(default=0.2, ge=0)


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
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        resolved: dict = _resolve_env_vars(raw)  # type: ignore[assignment]
    else:
        logger.info("No config file at %s — building config from env vars", config_path)
        resolved = {}

    if apply_env_overrides:
        env_overrides = _config_from_env()
        if env_overrides:
            resolved = _deep_merge(resolved, env_overrides)

    return AppConfig.model_validate(resolved)
