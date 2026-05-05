from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExpectedEffect(BaseModel):
    domain: str
    service: str
    entity_id: str
    expected_state: str


class ActionSummary(BaseModel):
    action_id: str
    label: str
    confirm_required: bool
    response_text: str
    expected_effect: ExpectedEffect | None = None


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
    expected_effect: ExpectedEffect | None = None
    confirmation_token: str | None = None
    preview: dict[str, Any] | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    status: Literal["ok", "degraded", "config_error"]
    home_assistant: dict[str, Any]
    actions_count: int
