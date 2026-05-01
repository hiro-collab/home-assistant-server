from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,79}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HA_SCRIPT_RE = re.compile(r"^script\.[a-z0-9_]+$")
PLACEHOLDER_SECRET_PREFIXES = ("change-me", "replace", "example", "dummy")
PLACEHOLDER_SECRET_VALUES = {
    "changeme",
    "change-me-local-bridge-token",
    "change-me-home-assistant-token",
    "test-token",
}


class ConfigError(RuntimeError):
    """Raised when bridge configuration is missing or invalid."""


class HomeAssistantConfig(BaseModel):
    base_url: str
    token_env: str = "HOME_ASSISTANT_TOKEN"
    timeout_seconds: float = Field(default=8.0, gt=0, le=60)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("home_assistant.base_url must start with http:// or https://")
        return value

    @field_validator("token_env")
    @classmethod
    def validate_token_env_name(cls, value: str) -> str:
        return _validate_env_name(value)


class ServerConfig(BaseModel):
    api_token_env: str = "HOME_CONTROL_API_TOKEN"
    log_path: str = ".cache/home_control/events.jsonl"
    min_api_token_length: int = Field(default=32, ge=16, le=512)

    @field_validator("api_token_env")
    @classmethod
    def validate_api_token_env_name(cls, value: str) -> str:
        return _validate_env_name(value)


class UdpEventsConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=7000, ge=1, le=65535)
    event_type: str = "home_control_magic"

    @field_validator("host", "event_type")
    @classmethod
    def validate_non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class ActionConfig(BaseModel):
    label: str
    ha_script: str
    confirm_required: bool = False
    response_text: str

    @field_validator("ha_script")
    @classmethod
    def validate_script_entity(cls, value: str) -> str:
        value = value.strip()
        if not HA_SCRIPT_RE.match(value):
            raise ValueError("ha_script must be a Home Assistant script entity such as script.demo_light_on")
        return value


class BridgeConfig(BaseModel):
    home_assistant: HomeAssistantConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    udp_events: UdpEventsConfig = Field(default_factory=UdpEventsConfig)
    actions: dict[str, ActionConfig]

    @field_validator("actions")
    @classmethod
    def validate_actions(cls, value: dict[str, ActionConfig]) -> dict[str, ActionConfig]:
        if not value:
            raise ValueError("at least one action must be configured")
        for action_id in value:
            if not ACTION_ID_RE.match(action_id):
                raise ValueError(f"invalid action_id: {action_id!r}")
        return value


def load_config(path: str | Path | None = None) -> BridgeConfig:
    config_path = Path(path or os.environ.get("HOME_CONTROL_CONFIG", "config/home-control.yaml"))
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. Copy config/home-control.example.yaml to this path first."
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {config_path} must contain a YAML mapping.")

    try:
        return BridgeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config file {config_path}: {exc}") from exc


def get_required_env(name: str) -> str:
    name = _validate_env_name(name)
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Required environment variable is not set: {name}")
    return value


def get_required_secret(name: str, *, min_length: int = 32) -> str:
    value = get_required_env(name).strip()
    lowered = value.lower()
    if len(value) < min_length or lowered in PLACEHOLDER_SECRET_VALUES:
        raise ConfigError(
            f"Required environment variable {name} must be a non-placeholder secret "
            f"with at least {min_length} characters."
        )
    if any(lowered.startswith(prefix) for prefix in PLACEHOLDER_SECRET_PREFIXES):
        raise ConfigError(f"Required environment variable {name} must not use a placeholder value.")
    return value


def _validate_env_name(value: str) -> str:
    value = value.strip()
    if not ENV_NAME_RE.match(value):
        raise ValueError("environment variable names must use uppercase letters, numbers, and underscores")
    return value


def action_preview_payload(action_id: str, action: ActionConfig) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "label": action.label,
        "ha_service": "script.turn_on",
        "ha_endpoint": "/api/services/script/turn_on",
        "ha_script": action.ha_script,
        "confirm_required": action.confirm_required,
        "response_text": action.response_text,
    }
