from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,79}$")
HA_SCRIPT_RE = re.compile(r"^script\.[a-z0-9_]+$")
ENTITY_ID_RE = re.compile(r"^(switch|light|fan)\.[a-z0-9_]+$")
ALLOWED_DIRECT_SERVICES = frozenset(
    {
        "fan.turn_off",
        "fan.turn_on",
        "light.turn_off",
        "light.turn_on",
        "switch.turn_off",
        "switch.turn_on",
    }
)


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


class ServerConfig(BaseModel):
    api_token_env: str = "HOME_CONTROL_API_TOKEN"
    log_path: str = ".cache/home_control/events.jsonl"


class ActionConfig(BaseModel):
    label: str
    ha_script: str | None = None
    ha_service: str | None = None
    entity_id: str | None = None
    confirm_required: bool = False
    response_text: str

    @field_validator("ha_script")
    @classmethod
    def validate_script_entity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not HA_SCRIPT_RE.match(value):
            raise ValueError("ha_script must be a Home Assistant script entity such as script.demo_light_on")
        return value

    @field_validator("ha_service")
    @classmethod
    def validate_direct_service(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if value not in ALLOWED_DIRECT_SERVICES:
            allowed = ", ".join(sorted(ALLOWED_DIRECT_SERVICES))
            raise ValueError(f"ha_service must be one of: {allowed}")
        return value

    @field_validator("entity_id")
    @classmethod
    def validate_entity_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not ENTITY_ID_RE.match(value):
            raise ValueError("entity_id must be a switch.*, light.*, or fan.* entity")
        return value

    @model_validator(mode="after")
    def validate_action_target(self) -> "ActionConfig":
        has_script = self.ha_script is not None
        has_direct_value = self.ha_service is not None or self.entity_id is not None
        has_direct = self.ha_service is not None and self.entity_id is not None

        if has_script and has_direct_value:
            raise ValueError("configure either ha_script or ha_service/entity_id, not both")
        if not has_script and not has_direct:
            raise ValueError("configure either ha_script or both ha_service and entity_id")
        if has_direct_value and not has_direct:
            raise ValueError("direct actions require both ha_service and entity_id")
        if has_direct and self.ha_service.split(".", 1)[0] != self.entity_id.split(".", 1)[0]:
            raise ValueError("ha_service domain must match entity_id domain")
        return self

    def service_name(self) -> str:
        if self.ha_script is not None:
            return "script.turn_on"
        if self.ha_service is None:
            raise ConfigError("Action has no Home Assistant service configured.")
        return self.ha_service

    def service_endpoint(self) -> str:
        domain, service = self.service_name().split(".", 1)
        return f"/api/services/{domain}/{service}"

    def service_payload(self) -> dict[str, str]:
        if self.ha_script is not None:
            return {"entity_id": self.ha_script}
        if self.entity_id is None:
            raise ConfigError("Action has no Home Assistant entity configured.")
        return {"entity_id": self.entity_id}


class BridgeConfig(BaseModel):
    home_assistant: HomeAssistantConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
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
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Required environment variable is not set: {name}")
    return value


def action_preview_payload(action_id: str, action: ActionConfig) -> dict[str, Any]:
    payload = {
        "action_id": action_id,
        "label": action.label,
        "ha_service": action.service_name(),
        "ha_endpoint": action.service_endpoint(),
        "confirm_required": action.confirm_required,
        "response_text": action.response_text,
    }
    if action.ha_script is not None:
        payload["ha_script"] = action.ha_script
    else:
        payload["entity_id"] = action.entity_id
    return payload


def action_audit_payload(action: ActionConfig) -> dict[str, str]:
    if action.ha_script is not None:
        return {"ha_script": action.ha_script}
    return {
        "ha_service": action.service_name(),
        "entity_id": action.entity_id or "",
    }
