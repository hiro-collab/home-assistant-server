from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,79}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HA_SCRIPT_RE = re.compile(r"^script\.[a-z0-9_]+$")
PUBLIC_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,119}$")
NESTED_REGEX_QUANTIFIER_RE = re.compile(r"\([^)]*[+*{][^)]*\)\s*[+*{]")
REGEX_BACKREFERENCE_RE = re.compile(r"\\[1-9]")
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


class ExpectedEffectConfig(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    service: str = Field(min_length=1, max_length=80)
    entity_id: str = Field(min_length=1, max_length=160)
    expected_state: str = Field(min_length=1, max_length=80)

    @field_validator("domain", "service", "entity_id", "expected_state")
    @classmethod
    def normalize_non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class PositionProofConfig(BaseModel):
    attribute: str = Field(default="current_position", min_length=1, max_length=80)
    min: float | None = Field(default=None, ge=0, le=100)
    max: float | None = Field(default=None, ge=0, le=100)

    @field_validator("attribute")
    @classmethod
    def normalize_attribute(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("position attribute must not be empty")
        return value

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min is None and self.max is None:
            raise ValueError("position proof requires min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("position proof min must be less than or equal to max")
        return self


ControlType = Literal[
    "stateful_target",
    "stateless_toggle",
    "stateless_command",
    "position_command",
    "mode_command",
    "job_command",
    "script_wrapper",
]
VerificationMode = Literal[
    "ha_state",
    "external_observation",
    "command_ack_only",
    "manual_confirmation",
    "unsupported",
]
StateTrackingStatus = Literal[
    "tracked",
    "external_required",
    "ack_only",
    "manual_required",
    "unsupported",
]
StateAuthority = Literal[
    "ha_entity",
    "ha_inferred",
    "external_sensor",
    "manual",
    "open_loop",
    "submitted_only",
    "unknown",
]


class VerificationConfig(BaseModel):
    mode: VerificationMode
    accepted_states: list[str] = Field(default_factory=list, max_length=12)
    settle_seconds: float = Field(default=0.0, ge=0, le=600)
    timeout_seconds: float = Field(default=0.0, ge=0, le=1800)
    position: PositionProofConfig | None = None

    @field_validator("accepted_states")
    @classmethod
    def normalize_accepted_states(cls, value: list[str]) -> list[str]:
        states: list[str] = []
        for item in value:
            state = item.strip()
            if not state:
                raise ValueError("accepted_states entries must not be empty")
            if len(state) > 80:
                raise ValueError("accepted_states entries must be at most 80 characters")
            if state not in states:
                states.append(state)
        return states

    @model_validator(mode="after")
    def validate_position_mode(self):
        if self.position is not None and self.mode != "ha_state":
            raise ValueError("position proof is only supported with verification.mode: ha_state")
        return self


FaultScenario = Literal[
    "always_success",
    "fail_once_then_success",
    "fail_twice_then_success",
    "fail_always",
    "confirmation_required",
    "timeout_once",
    "unsupported_action",
    "duplicate",
]


class FaultMatchConfig(BaseModel):
    action_id: str | None = Field(default=None, max_length=80)
    source: str | None = Field(default=None, max_length=80)
    request_id: str | None = Field(default=None, max_length=160)
    request_id_prefix: str | None = Field(default=None, max_length=160)
    request_id_suffix: str | None = Field(default=None, max_length=160)
    request_id_regex: str | None = Field(default=None, max_length=120)
    user_text_contains: str | None = Field(default=None, max_length=200)
    user_text_regex: str | None = Field(default=None, max_length=120)
    confirmed: bool | None = None

    @field_validator(
        "action_id",
        "source",
        "request_id",
        "request_id_prefix",
        "request_id_suffix",
        "request_id_regex",
        "user_text_contains",
        "user_text_regex",
    )
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("action_id")
    @classmethod
    def validate_optional_action_id(cls, value: str | None) -> str | None:
        if value is not None and not ACTION_ID_RE.match(value):
            raise ValueError("invalid action_id")
        return value

    @field_validator("request_id_regex", "user_text_regex")
    @classmethod
    def validate_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if NESTED_REGEX_QUANTIFIER_RE.search(value) or REGEX_BACKREFERENCE_RE.search(value):
            raise ValueError("fault regex must not use nested quantifiers or backreferences")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return value


class FaultRuleConfig(BaseModel):
    match: FaultMatchConfig = Field(default_factory=FaultMatchConfig)
    scenario: FaultScenario
    message: str | None = Field(default=None, max_length=500)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class FaultInjectionConfig(BaseModel):
    enabled: bool = False
    enabled_env: str = "HOME_CONTROL_FAULT_MODE"
    rules: list[FaultRuleConfig] = Field(default_factory=list)

    @field_validator("enabled_env")
    @classmethod
    def validate_enabled_env_name(cls, value: str) -> str:
        return _validate_env_name(value)


class ActionConfig(BaseModel):
    label: str
    ha_script: str
    confirm_required: bool = False
    response_text: str
    control_type: ControlType | None = None
    state_authority: StateAuthority | None = None
    verification: VerificationConfig | None = None
    expected_effect: ExpectedEffectConfig | None = None
    live_test_candidate: bool = False
    restore_required: bool = True
    restore_action_id: str | None = Field(default=None, max_length=80)
    stop_action_id: str | None = Field(default=None, max_length=80)
    terminal_action: bool = False
    safety_requirements: list[str] = Field(default_factory=list, max_length=12)
    proof_ceiling: str | None = Field(default=None, max_length=120)

    @field_validator("ha_script")
    @classmethod
    def validate_script_entity(cls, value: str) -> str:
        value = value.strip()
        if not HA_SCRIPT_RE.match(value):
            raise ValueError("ha_script must be a Home Assistant script entity such as script.demo_light_on")
        return value

    @field_validator("restore_action_id", "stop_action_id")
    @classmethod
    def validate_optional_action_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not ACTION_ID_RE.match(value):
            raise ValueError("action references must be valid action_id values")
        return value

    @field_validator("safety_requirements")
    @classmethod
    def normalize_safety_requirements(cls, value: list[str]) -> list[str]:
        requirements: list[str] = []
        for item in value:
            requirement = item.strip()
            if not PUBLIC_CLASS_RE.match(requirement):
                raise ValueError("safety_requirements entries must be public lowercase class labels")
            if requirement not in requirements:
                requirements.append(requirement)
        return requirements

    @field_validator("proof_ceiling")
    @classmethod
    def normalize_proof_ceiling(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not PUBLIC_CLASS_RE.match(value):
            raise ValueError("proof_ceiling must be a public lowercase class label")
        return value


class BridgeConfig(BaseModel):
    home_assistant: HomeAssistantConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    udp_events: UdpEventsConfig = Field(default_factory=UdpEventsConfig)
    faults: FaultInjectionConfig = Field(default_factory=FaultInjectionConfig)
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

    @model_validator(mode="after")
    def validate_action_references(self):
        for action_id, action in self.actions.items():
            for ref_name in ("restore_action_id", "stop_action_id"):
                ref = getattr(action, ref_name)
                if ref is None:
                    continue
                if ref not in self.actions:
                    raise ValueError(f"{action_id}.{ref_name} references unknown action_id: {ref}")
                if ref == action_id:
                    raise ValueError(f"{action_id}.{ref_name} must not reference itself")
        return self


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
    payload = {
        "action_id": action_id,
        "label": action.label,
        "ha_service": "script.turn_on",
        "ha_endpoint": "/api/services/script/turn_on",
        "ha_script": action.ha_script,
        "confirm_required": action.confirm_required,
        "response_text": action.response_text,
        "control_type": action_control_type(action),
        "state_authority": action_state_authority(action),
        "verification_mode": action_verification_mode(action),
        "state_tracking": action_state_tracking_status(action),
        "expected_states": action_expected_states(action),
        "settle_seconds": action_settle_seconds(action),
        "timeout_seconds": action_timeout_seconds(action),
        "proof_ceiling": action_proof_ceiling(action),
        "live_test_candidate": action.live_test_candidate,
        "live_test_readiness": action_live_test_readiness(action),
        "live_test_blockers": action_live_test_blockers(action),
        "restore_required": action.restore_required,
        "restore_action_id": action.restore_action_id,
        "stop_action_id": action.stop_action_id,
        "terminal_action": action.terminal_action,
        "safety_requirements": list(action.safety_requirements),
    }
    effect = action_public_expected_effect(action)
    if effect is not None:
        payload["expected_effect"] = effect
    position_proof = action_public_position_proof(action)
    if position_proof is not None:
        payload["position_proof"] = position_proof
    return payload


def action_control_type(action: ActionConfig) -> ControlType:
    if action.control_type is not None:
        return action.control_type
    return "script_wrapper"


def action_verification_mode(action: ActionConfig) -> VerificationMode:
    if action.verification is not None:
        return action.verification.mode
    return "command_ack_only"


def action_state_authority(action: ActionConfig) -> StateAuthority:
    if action.state_authority is not None:
        return action.state_authority

    mode = action_verification_mode(action)
    if mode == "external_observation":
        return "external_sensor"
    if mode == "manual_confirmation":
        return "manual"
    if mode == "command_ack_only":
        return "submitted_only"
    return "unknown"


def action_state_tracking_status(action: ActionConfig) -> StateTrackingStatus:
    mode = action_verification_mode(action)
    if mode == "ha_state":
        if action.state_authority == "ha_entity" and action.expected_effect is not None:
            return "tracked"
        return "unsupported"
    if mode == "external_observation":
        return "external_required"
    if mode == "manual_confirmation":
        return "manual_required"
    if mode == "command_ack_only":
        return "ack_only"
    return "unsupported"


def action_public_expected_effect(action: ActionConfig) -> dict[str, str] | None:
    if action_state_tracking_status(action) != "tracked":
        return None
    if action.expected_effect is None:
        return None
    return action.expected_effect.model_dump()


def action_public_position_proof(action: ActionConfig) -> dict[str, object] | None:
    if action_state_tracking_status(action) != "tracked":
        return None
    if action.verification is None or action.verification.position is None:
        return None
    return action.verification.position.model_dump(exclude_none=True)


def action_expected_states(action: ActionConfig) -> list[str]:
    if action_state_tracking_status(action) != "tracked":
        return []
    if action.expected_effect is None:
        return []

    states: list[str] = [action.expected_effect.expected_state]
    if action.verification is not None:
        for state in action.verification.accepted_states:
            if state not in states:
                states.append(state)
    return states


def action_settle_seconds(action: ActionConfig) -> float:
    if action.verification is None:
        return 0.0
    return action.verification.settle_seconds


def action_timeout_seconds(action: ActionConfig) -> float:
    if action.verification is None:
        return 0.0
    return action.verification.timeout_seconds


def action_proof_ceiling(action: ActionConfig) -> str:
    if action.proof_ceiling is not None:
        return action.proof_ceiling

    mode = action_verification_mode(action)
    if mode == "ha_state" and action_state_tracking_status(action) == "tracked":
        if action_public_position_proof(action) is not None:
            return "ha_visible_position_checkstate_layer"
        return "ha_visible_state_checkstate_layer"
    if mode == "external_observation":
        return "external_observation_required"
    if mode == "manual_confirmation":
        return "manual_confirmation_required"
    if mode == "command_ack_only":
        return "command_ack_only"
    return "unsupported"


def action_live_test_readiness(action: ActionConfig) -> str:
    if action.live_test_candidate and not action_live_test_blockers(action):
        return "test_now"
    if not action.live_test_candidate:
        return "not_live_test_candidate"
    return "do_not_test_current_config"


def action_live_test_blockers(action: ActionConfig) -> list[str]:
    blockers: list[str] = []

    if not action.live_test_candidate:
        blockers.append("not_marked_live_test_candidate")

    command_stimulus_allowed = action.live_test_candidate and not action.restore_required
    if action_state_tracking_status(action) != "tracked" and not command_stimulus_allowed:
        blockers.append("missing_ha_visible_success_criterion")

    if action.live_test_candidate and action.restore_required and not action.terminal_action:
        if action.restore_action_id is None and action.stop_action_id is None:
            blockers.append("missing_restore_or_stop")

    for requirement in action.safety_requirements:
        blockers.append(f"safety_requirement:{requirement}")

    return blockers
