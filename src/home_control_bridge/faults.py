from __future__ import annotations

import os
import re
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from .config import BridgeConfig, FaultRuleConfig, FaultScenario

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
ATTEMPT_SUFFIX_RE = re.compile(r"(?i)(?:[-_:](?:attempt|try|retry|hca)[-_:]?\d+)$")
FAULT_ATTEMPT_TTL_SECONDS = 600
MAX_FAULT_ATTEMPT_STATE = 256

FaultOutcome = Literal[
    "success",
    "failed",
    "confirmation_required",
    "duplicate",
    "unsupported_action",
]


@dataclass(frozen=True)
class FaultContext:
    action_id: str
    source: str
    request_id: str | None
    user_text: str | None
    confirmed: bool


@dataclass(frozen=True)
class FaultDecision:
    rule_index: int
    scenario: FaultScenario
    outcome: FaultOutcome
    attempt: int
    message: str | None


@dataclass(frozen=True)
class FaultAttemptRecord:
    attempt: int
    expires_at: float


def fault_mode_enabled(config: BridgeConfig) -> bool:
    env_value = os.environ.get(config.faults.enabled_env, "").strip().lower()
    return config.faults.enabled and env_value in TRUTHY_ENV_VALUES


def evaluate_fault(
    config: BridgeConfig,
    state: dict[str, FaultAttemptRecord],
    context: FaultContext,
    *,
    scenarios: set[FaultScenario] | None = None,
) -> FaultDecision | None:
    if not fault_mode_enabled(config):
        return None
    _prune_attempt_state(state)

    for index, rule in enumerate(config.faults.rules):
        if scenarios is not None and rule.scenario not in scenarios:
            continue
        if not _matches(rule, context):
            continue

        key = _state_key(index, context)
        record = state.get(key)
        attempt = (record.attempt if record is not None else 0) + 1
        if record is None:
            _reserve_attempt_slot(state)
        state[key] = FaultAttemptRecord(
            attempt=attempt,
            expires_at=monotonic() + FAULT_ATTEMPT_TTL_SECONDS,
        )
        outcome = _scenario_outcome(rule.scenario, attempt)
        return FaultDecision(
            rule_index=index,
            scenario=rule.scenario,
            outcome=outcome,
            attempt=attempt,
            message=rule.message,
        )

    return None


def _prune_attempt_state(state: dict[str, FaultAttemptRecord]) -> None:
    now = monotonic()
    for key, record in list(state.items()):
        if record.expires_at < now:
            state.pop(key, None)


def _reserve_attempt_slot(state: dict[str, FaultAttemptRecord]) -> None:
    if len(state) < MAX_FAULT_ATTEMPT_STATE:
        return
    oldest_key = min(state, key=lambda key: state[key].expires_at)
    state.pop(oldest_key, None)


def _matches(rule: FaultRuleConfig, context: FaultContext) -> bool:
    match = rule.match
    if match.action_id is not None and context.action_id != match.action_id:
        return False
    if match.source is not None and context.source != match.source:
        return False
    if match.confirmed is not None and context.confirmed is not match.confirmed:
        return False

    request_id = context.request_id or ""
    if match.request_id is not None and request_id != match.request_id:
        return False
    if match.request_id_prefix is not None and not request_id.startswith(match.request_id_prefix):
        return False
    if match.request_id_suffix is not None and not request_id.endswith(match.request_id_suffix):
        return False
    if match.request_id_regex is not None and re.search(match.request_id_regex, request_id) is None:
        return False

    user_text = context.user_text or ""
    if match.user_text_contains is not None and match.user_text_contains not in user_text:
        return False
    if match.user_text_regex is not None and re.search(match.user_text_regex, user_text) is None:
        return False

    return True


def _state_key(rule_index: int, context: FaultContext) -> str:
    request_id = _normalize_request_id(context.request_id)
    return "\0".join([str(rule_index), context.action_id, context.source, request_id or ""])


def _normalize_request_id(request_id: str | None) -> str | None:
    if request_id is None:
        return None
    return ATTEMPT_SUFFIX_RE.sub("", request_id)


def _scenario_outcome(scenario: FaultScenario, attempt: int) -> FaultOutcome:
    if scenario == "always_success":
        return "success"
    if scenario == "fail_once_then_success":
        return "failed" if attempt == 1 else "success"
    if scenario == "fail_twice_then_success":
        return "failed" if attempt <= 2 else "success"
    if scenario == "fail_always":
        return "failed"
    if scenario == "confirmation_required":
        return "confirmation_required"
    if scenario == "timeout_once":
        return "failed" if attempt == 1 else "success"
    if scenario == "unsupported_action":
        return "unsupported_action"
    if scenario == "duplicate":
        return "duplicate"
    raise AssertionError(f"Unhandled fault scenario: {scenario}")
