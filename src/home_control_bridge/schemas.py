from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionSummary(BaseModel):
    action_id: str
    label: str
    confirm_required: bool
    response_text: str


class ActionRequest(BaseModel):
    source: str = Field(default="unknown", max_length=80)
    request_id: str | None = Field(default=None, max_length=160)
    user_text: str | None = Field(default=None, max_length=1000)
    dry_run: bool = False
    confirmed: bool = False


class ActionResponse(BaseModel):
    ok: bool
    action_id: str
    executed: bool
    confirmation_required: bool = False
    message: str
    speak: str
    request_id: str | None = None
    preview: dict[str, Any] | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    status: Literal["ok", "degraded", "config_error"]
    home_assistant: dict[str, Any]
    actions_count: int
