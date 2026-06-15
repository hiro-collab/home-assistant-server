from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExpectedEffect(BaseModel):
    domain: str
    service: str
    entity_id: str
    expected_state: str


class PositionProof(BaseModel):
    attribute: str = "current_position"
    min: float | None = None
    max: float | None = None


class Verification(BaseModel):
    mode: Literal[
        "ha_state",
        "external_observation",
        "command_ack_only",
        "manual_confirmation",
        "unsupported",
    ]
    accepted_states: list[str] = Field(default_factory=list)
    settle_seconds: float = 0.0
    timeout_seconds: float = 0.0
    position: PositionProof | None = None


class ActionSummary(BaseModel):
    action_id: str
    label: str
    confirm_required: bool
    response_text: str
    control_type: Literal[
        "stateful_target",
        "stateless_toggle",
        "stateless_command",
        "position_command",
        "mode_command",
        "job_command",
        "script_wrapper",
    ]
    state_authority: Literal[
        "ha_entity",
        "ha_inferred",
        "external_sensor",
        "manual",
        "open_loop",
        "submitted_only",
        "unknown",
    ]
    verification_mode: Literal[
        "ha_state",
        "external_observation",
        "command_ack_only",
        "manual_confirmation",
        "unsupported",
    ]
    state_tracking: Literal[
        "tracked",
        "external_required",
        "ack_only",
        "manual_required",
        "unsupported",
    ]
    verification: Verification | None = None
    expected_effect: ExpectedEffect | None = None
    position_proof: PositionProof | None = None
    expected_states: list[str] = Field(default_factory=list)
    settle_seconds: float = 0.0
    timeout_seconds: float = 0.0
    proof_ceiling: str
    live_test_candidate: bool = False
    live_test_readiness: str
    live_test_blockers: list[str] = Field(default_factory=list)
    restore_action_id: str | None = None
    stop_action_id: str | None = None
    terminal_action: bool = False
    safety_requirements: list[str] = Field(default_factory=list)


class ActionStateResponse(BaseModel):
    ok: bool
    action_id: str
    status: Literal[
        "matched",
        "mismatch",
        "untracked",
        "unavailable",
        "position_unavailable",
        "external_required",
        "ack_only",
        "manual_required",
        "unsupported",
    ]
    control_type: str | None = None
    state_authority: str | None = None
    verification_mode: str | None = None
    state_tracking: str | None = None
    expected_state: str | None = None
    expected_states: list[str] = Field(default_factory=list)
    actual_state: str | None = None
    position_attribute: str | None = None
    expected_position_min: float | None = None
    expected_position_max: float | None = None
    actual_position: float | None = None
    position_status: Literal["matched", "mismatch", "unavailable"] | None = None


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="unknown", max_length=80)
    request_id: str | None = Field(default=None, max_length=160)
    user_text: str | None = Field(default=None, max_length=1000)
    dry_run: bool = False
    confirmed: bool = False
    confirmation_token: str | None = Field(default=None, max_length=128)


class ActionResponse(BaseModel):
    ok: bool
    action_id: str
    executed: bool
    status: Literal[
        "preview",
        "confirmation_required",
        "dry_run",
        "duplicate",
        "submitted",
        "failed",
    ]
    confirmation_required: bool = False
    message: str
    speak: str
    request_id: str | None = None
    execution_id: str | None = None
    issued_at: str | None = None
    domain: str | None = None
    service: str | None = None
    entity_id: str | None = None
    expected_state: str | None = None
    control_type: str | None = None
    state_authority: str | None = None
    verification_mode: str | None = None
    state_tracking: str | None = None
    expected_effect: ExpectedEffect | None = None
    position_proof: PositionProof | None = None
    expected_states: list[str] = Field(default_factory=list)
    settle_seconds: float = 0.0
    timeout_seconds: float = 0.0
    proof_ceiling: str | None = None
    live_test_candidate: bool = False
    live_test_readiness: str | None = None
    live_test_blockers: list[str] = Field(default_factory=list)
    restore_action_id: str | None = None
    stop_action_id: str | None = None
    terminal_action: bool = False
    safety_requirements: list[str] = Field(default_factory=list)
    confirmation_token: str | None = None
    preview: dict[str, Any] | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    status: Literal["ok", "degraded", "config_error"]
    home_assistant: dict[str, Any]
    actions_count: int
    config_profile: str = "unknown"
    demo_mappings_present: bool = False
    light_demo_mappings_present: bool = False
    fault_mode: bool = False
    fault_rules_count: int = 0
