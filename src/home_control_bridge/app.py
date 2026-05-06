from __future__ import annotations

import secrets
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .audit import JsonlAuditLogger
from .config import ActionConfig, BridgeConfig, ConfigError, action_preview_payload, get_required_secret, load_config
from .faults import FaultContext, FaultDecision, evaluate_fault, fault_mode_enabled
from .home_assistant import HomeAssistantClient, HomeAssistantError
from .schemas import ActionRequest, ActionResponse, ActionSummary, HealthResponse
from .udp_events import UdpEventPhase, UdpEventSender

CONFIRMATION_TOKEN_TTL_SECONDS = 120
EXECUTION_REQUEST_TTL_SECONDS = 600
GENERIC_CONFIG_ERROR = "Bridge configuration is unavailable."
HOME_ASSISTANT_ERROR_CODE = "home_assistant_request_failed"


def create_app(
    config: BridgeConfig | None = None,
    ha_client: HomeAssistantClient | None = None,
    audit_logger: JsonlAuditLogger | None = None,
    udp_event_sender: UdpEventSender | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Home Control Safety Bridge",
        version="0.1.0",
        description="Allowlisted HTTP bridge for calling Home Assistant script.turn_on safely.",
    )

    config_error: str | None = None
    if config is None:
        try:
            config = load_config()
        except ConfigError as exc:
            config_error = str(exc)

    app.state.config = config
    app.state.config_error = config_error
    app.state.confirmation_tokens = {}
    app.state.execution_requests = {}
    app.state.fault_attempts = {}
    app.state.audit_logger = audit_logger if audit_logger is not None and config else (
        JsonlAuditLogger(config.server.log_path) if config else None
    )
    app.state.udp_event_sender = udp_event_sender if udp_event_sender is not None and config else (
        UdpEventSender(config.udp_events) if config else None
    )
    app.state.ha_client = ha_client

    if config and app.state.ha_client is None:
        try:
            ha_token = get_required_secret(config.home_assistant.token_env)
            app.state.ha_client = HomeAssistantClient(config.home_assistant, ha_token)
        except ConfigError as exc:
            app.state.config_error = str(exc)

    @app.exception_handler(ConfigError)
    async def config_error_handler(_: Request, __: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"ok": False, "error": GENERIC_CONFIG_ERROR},
        )

    def require_config() -> BridgeConfig:
        if app.state.config is None:
            raise ConfigError(app.state.config_error or "Bridge config is not loaded.")
        if app.state.config_error:
            raise ConfigError(app.state.config_error)
        return app.state.config

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
        x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
    ) -> None:
        config = require_config()
        expected = get_required_secret(
            config.server.api_token_env,
            min_length=config.server.min_api_token_length,
        )
        actual = _extract_token(authorization, x_api_token)
        if not actual or not secrets.compare_digest(actual, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        if app.state.config is None:
            return HealthResponse(
                ok=False,
                status="config_error",
                home_assistant={"ok": False, "error": GENERIC_CONFIG_ERROR},
                actions_count=0,
                fault_mode=False,
                fault_rules_count=0,
            )
        if app.state.config_error:
            return HealthResponse(
                ok=False,
                status="config_error",
                home_assistant={"ok": False, "error": GENERIC_CONFIG_ERROR},
                actions_count=len(app.state.config.actions),
                fault_mode=fault_mode_enabled(app.state.config),
                fault_rules_count=len(app.state.config.faults.rules),
            )
        ha_status = await app.state.ha_client.check_connection()
        ok = bool(ha_status.get("ok"))
        return HealthResponse(
            ok=ok,
            status="ok" if ok else "degraded",
            home_assistant=ha_status,
            actions_count=len(app.state.config.actions),
            fault_mode=fault_mode_enabled(app.state.config),
            fault_rules_count=len(app.state.config.faults.rules),
        )

    @app.get("/actions", response_model=list[ActionSummary], dependencies=[Depends(require_auth)])
    async def list_actions() -> list[ActionSummary]:
        config = require_config()
        return [
            ActionSummary(
                action_id=action_id,
                label=action.label,
                confirm_required=action.confirm_required,
                response_text=action.response_text,
                expected_effect=_expected_effect_payload(action),
            )
            for action_id, action in sorted(config.actions.items())
        ]

    @app.post(
        "/actions/{action_id}/preview",
        response_model=ActionResponse,
        dependencies=[Depends(require_auth)],
    )
    async def preview_action(action_id: str, body: ActionRequest | None = None) -> ActionResponse:
        body = body or ActionRequest()
        config = require_config()
        action = _get_action(config, action_id)
        preview = action_preview_payload(action_id, action)
        confirmation_token = _create_confirmation_token(app, action_id) if action.confirm_required else None
        _audit(
            app,
            {
                "event": "preview",
                "action_id": action_id,
                **_request_audit_fields(body),
                "executed": False,
                "confirm_required": action.confirm_required,
                "confirmation_challenge_issued": confirmation_token is not None,
                "ha_script": action.ha_script,
            },
        )
        message = f"{action.label}を実行します。よろしいですか？" if action.confirm_required else action.response_text
        return ActionResponse(
            ok=True,
            action_id=action_id,
            executed=False,
            status="preview",
            confirmation_required=action.confirm_required,
            message=message,
            speak=message,
            request_id=body.request_id,
            confirmation_token=confirmation_token,
            preview=preview,
            **_response_tracking_fields(action),
        )

    @app.post(
        "/actions/{action_id}/execute",
        response_model=ActionResponse,
        dependencies=[Depends(require_auth)],
    )
    async def execute_action(action_id: str, body: ActionRequest | None = None) -> ActionResponse:
        body = body or ActionRequest()
        config = require_config()
        unsupported_fault = _evaluate_fault(app, config, action_id, body, scenarios={"unsupported_action"})
        if unsupported_fault is not None:
            return _fault_response(app, action_id, None, body, None, unsupported_fault)
        action = _get_action(config, action_id)
        preview = action_preview_payload(action_id, action)

        if action.confirm_required and not (
            body.confirmed and _consume_confirmation_token(app, action_id, body.confirmation_token)
        ):
            confirmation_token = _create_confirmation_token(app, action_id)
            message = (
                f"{action.label}には確認が必要です。実行する場合は confirmed=true と "
                "confirmation_token を指定してください。"
            )
            _audit(
                app,
                {
                    "event": "execute_blocked_confirmation",
                    "action_id": action_id,
                    **_request_audit_fields(body),
                    "executed": False,
                    "confirm_required": True,
                    "confirmation_challenge_issued": True,
                    "ha_script": action.ha_script,
                },
            )
            return ActionResponse(
                ok=True,
                action_id=action_id,
                executed=False,
                status="confirmation_required",
                confirmation_required=True,
                message=message,
                speak=message,
                request_id=body.request_id,
                confirmation_token=confirmation_token,
                preview=preview,
                **_response_tracking_fields(action),
            )

        if body.dry_run:
            message = f"dry-run: {action.label}を実行予定です。"
            _audit(
                app,
                {
                    "event": "execute_dry_run",
                    "action_id": action_id,
                    **_request_audit_fields(body),
                    "executed": False,
                    "confirm_required": action.confirm_required,
                    "confirmed": body.confirmed,
                    "ha_script": action.ha_script,
                },
            )
            return ActionResponse(
                ok=True,
                action_id=action_id,
                executed=False,
                status="dry_run",
                confirmation_required=action.confirm_required,
                message=message,
                speak=message,
                request_id=body.request_id,
                preview=preview,
                **_response_tracking_fields(action),
            )

        duplicate_execution = _get_execution_request(app, action_id, body.request_id)
        if duplicate_execution is not None:
            message = "同じ request_id の操作はすでに受け付け済みです。"
            _audit(
                app,
                {
                    "event": "execute_duplicate_request",
                    "action_id": action_id,
                    "execution_id": duplicate_execution["execution_id"],
                    "issued_at": duplicate_execution["issued_at"],
                    "status": "duplicate",
                    **_request_audit_fields(body),
                    "executed": False,
                    "confirm_required": action.confirm_required,
                    "confirmed": body.confirmed,
                    "ha_script": action.ha_script,
                    **_expected_effect_audit_fields(action),
                },
            )
            return ActionResponse(
                ok=True,
                action_id=action_id,
                executed=False,
                status="duplicate",
                confirmation_required=action.confirm_required,
                message=message,
                speak=message,
                request_id=body.request_id,
                execution_id=duplicate_execution["execution_id"],
                issued_at=duplicate_execution["issued_at"],
                preview=preview,
                **_response_tracking_fields(action),
            )

        fault = _evaluate_fault(app, config, action_id, body)
        if fault is not None:
            return _fault_response(app, action_id, action, body, preview, fault)

        execution_id = str(uuid4())
        issued_at = _utc_now_iso()
        _register_execution_request(app, action_id, body.request_id, execution_id, issued_at)
        _emit_action_event(app, "start", action_id, action, body, execution_id=execution_id)

        try:
            ha_result = await app.state.ha_client.turn_on_script(action.ha_script)
        except HomeAssistantError as exc:
            error_detail = getattr(exc, "log_detail", str(exc))
            _audit(
                app,
                {
                    "event": "execute_failed",
                    "action_id": action_id,
                    "execution_id": execution_id,
                    "issued_at": issued_at,
                    "status": "failed",
                    **_request_audit_fields(body),
                    "executed": False,
                    "confirm_required": action.confirm_required,
                    "confirmed": body.confirmed,
                    "ha_script": action.ha_script,
                    **_expected_effect_audit_fields(action),
                    "error": error_detail,
                },
            )
            _emit_action_event(
                app,
                "error",
                action_id,
                action,
                body,
                execution_id=execution_id,
                message="Home Assistantへの実行要求に失敗しました。",
                error=HOME_ASSISTANT_ERROR_CODE,
            )
            return ActionResponse(
                ok=False,
                action_id=action_id,
                executed=False,
                status="failed",
                confirmation_required=action.confirm_required,
                message="Home Assistantへの実行要求に失敗しました。",
                speak="家電操作に失敗しました。",
                request_id=body.request_id,
                execution_id=execution_id,
                issued_at=issued_at,
                preview=preview,
                **_response_tracking_fields(action),
                error=HOME_ASSISTANT_ERROR_CODE,
            )

        _audit(
            app,
            {
                "event": "execute_succeeded",
                "action_id": action_id,
                "execution_id": execution_id,
                "issued_at": issued_at,
                "status": "submitted",
                **_request_audit_fields(body),
                "executed": True,
                "confirm_required": action.confirm_required,
                "confirmed": body.confirmed,
                "ha_script": action.ha_script,
                **_expected_effect_audit_fields(action),
                "ha_status_code": ha_result.get("status_code"),
            },
        )
        _emit_action_event(app, "done", action_id, action, body, execution_id=execution_id, message=action.response_text)
        return ActionResponse(
            ok=True,
            action_id=action_id,
            executed=True,
            status="submitted",
            confirmation_required=action.confirm_required,
            message=action.response_text,
            speak=action.response_text,
            request_id=body.request_id,
            execution_id=execution_id,
            issued_at=issued_at,
            preview=preview,
            **_response_tracking_fields(action),
        )

    return app


def _extract_token(authorization: str | None, x_api_token: str | None) -> str | None:
    if x_api_token:
        return x_api_token
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def _create_confirmation_token(app: FastAPI, action_id: str) -> str:
    _prune_confirmation_tokens(app)
    token = secrets.token_urlsafe(32)
    app.state.confirmation_tokens[token] = (action_id, monotonic() + CONFIRMATION_TOKEN_TTL_SECONDS)
    return token


def _consume_confirmation_token(app: FastAPI, action_id: str, token: str | None) -> bool:
    _prune_confirmation_tokens(app)
    if not token:
        return False
    challenge = app.state.confirmation_tokens.pop(token, None)
    if challenge is None:
        return False
    expected_action_id, expires_at = challenge
    return expected_action_id == action_id and expires_at >= monotonic()


def _prune_confirmation_tokens(app: FastAPI) -> None:
    now = monotonic()
    for token, (_, expires_at) in list(app.state.confirmation_tokens.items()):
        if expires_at < now:
            app.state.confirmation_tokens.pop(token, None)


def _execution_key(action_id: str, request_id: str) -> str:
    return f"{action_id}\0{request_id}"


def _get_execution_request(app: FastAPI, action_id: str, request_id: str | None) -> dict[str, str] | None:
    _prune_execution_requests(app)
    if not request_id:
        return None
    record = app.state.execution_requests.get(_execution_key(action_id, request_id))
    if record is None:
        return None
    return {
        "execution_id": record["execution_id"],
        "issued_at": record["issued_at"],
    }


def _register_execution_request(
    app: FastAPI,
    action_id: str,
    request_id: str | None,
    execution_id: str,
    issued_at: str,
) -> None:
    _prune_execution_requests(app)
    if not request_id:
        return
    app.state.execution_requests[_execution_key(action_id, request_id)] = {
        "expires_at": monotonic() + EXECUTION_REQUEST_TTL_SECONDS,
        "execution_id": execution_id,
        "issued_at": issued_at,
    }


def _prune_execution_requests(app: FastAPI) -> None:
    now = monotonic()
    for key, record in list(app.state.execution_requests.items()):
        expires_at = record["expires_at"]
        if expires_at < now:
            app.state.execution_requests.pop(key, None)


def _get_action(config: BridgeConfig, action_id: str):
    action = config.actions.get(action_id)
    if action is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action is not allowlisted.")
    return action


def _request_audit_fields(body: ActionRequest) -> dict[str, object]:
    fields: dict[str, object] = {
        "source": body.source,
        "request_id": body.request_id,
        "user_text_present": body.user_text is not None,
    }
    if body.user_text is not None:
        fields["user_text_length"] = len(body.user_text)
    return fields


def _evaluate_fault(
    app: FastAPI,
    config: BridgeConfig,
    action_id: str,
    body: ActionRequest,
    *,
    scenarios: set | None = None,
) -> FaultDecision | None:
    return evaluate_fault(
        config,
        app.state.fault_attempts,
        FaultContext(
            action_id=action_id,
            source=body.source,
            request_id=body.request_id,
            user_text=body.user_text,
            confirmed=body.confirmed,
        ),
        scenarios=scenarios,
    )


def _fault_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    if fault.outcome == "confirmation_required":
        return _fault_confirmation_response(app, action_id, action, body, preview, fault)
    if fault.outcome == "success":
        return _fault_success_response(app, action_id, action, body, preview, fault)
    if fault.outcome == "failed":
        return _fault_failed_response(app, action_id, action, body, preview, fault)
    if fault.outcome == "duplicate":
        return _fault_duplicate_response(app, action_id, action, body, preview, fault)
    if fault.outcome == "unsupported_action":
        return _fault_unsupported_response(app, action_id, action, body, preview, fault)
    raise AssertionError(f"Unhandled fault outcome: {fault.outcome}")


def _fault_confirmation_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    if body.confirmed and _consume_confirmation_token(app, action_id, body.confirmation_token):
        return _fault_success_response(app, action_id, action, body, preview, fault)

    confirmation_token = _create_confirmation_token(app, action_id)
    label = action.label if action is not None else action_id
    message = fault.message or (
        f"{label}には確認が必要です。実行する場合は confirmed=true と "
        "confirmation_token を指定してください。"
    )
    _audit_fault(
        app,
        action_id,
        action,
        body,
        fault,
        status_value="confirmation_required",
        executed=False,
        execution_id=None,
        issued_at=None,
    )
    return ActionResponse(
        ok=True,
        action_id=action_id,
        executed=False,
        status="confirmation_required",
        confirmation_required=True,
        message=message,
        speak=message,
        request_id=body.request_id,
        confirmation_token=confirmation_token,
        preview=preview,
        **_optional_response_tracking_fields(action),
    )


def _fault_success_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    execution_id = str(uuid4())
    issued_at = _utc_now_iso()
    _register_execution_request(app, action_id, body.request_id, execution_id, issued_at)
    message = fault.message or (action.response_text if action is not None else "Simulated action submitted.")
    _audit_fault(
        app,
        action_id,
        action,
        body,
        fault,
        status_value="submitted",
        executed=True,
        execution_id=execution_id,
        issued_at=issued_at,
    )
    return ActionResponse(
        ok=True,
        action_id=action_id,
        executed=True,
        status="submitted",
        confirmation_required=False,
        message=message,
        speak=message,
        request_id=body.request_id,
        execution_id=execution_id,
        issued_at=issued_at,
        preview=preview,
        **_optional_response_tracking_fields(action),
    )


def _fault_failed_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    execution_id = str(uuid4())
    issued_at = _utc_now_iso()
    _register_execution_request(app, action_id, body.request_id, execution_id, issued_at)
    message = fault.message or "Home Assistantへの実行要求に失敗しました。"
    _audit_fault(
        app,
        action_id,
        action,
        body,
        fault,
        status_value="failed",
        executed=False,
        execution_id=execution_id,
        issued_at=issued_at,
    )
    return ActionResponse(
        ok=False,
        action_id=action_id,
        executed=False,
        status="failed",
        confirmation_required=False,
        message=message,
        speak="家電操作に失敗しました。",
        request_id=body.request_id,
        execution_id=execution_id,
        issued_at=issued_at,
        preview=preview,
        **_optional_response_tracking_fields(action),
        error=HOME_ASSISTANT_ERROR_CODE,
    )


def _fault_duplicate_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    execution_id = str(uuid4())
    issued_at = _utc_now_iso()
    message = fault.message or "同じ request_id の操作はすでに受け付け済みです。"
    _audit_fault(
        app,
        action_id,
        action,
        body,
        fault,
        status_value="duplicate",
        executed=False,
        execution_id=execution_id,
        issued_at=issued_at,
    )
    return ActionResponse(
        ok=True,
        action_id=action_id,
        executed=False,
        status="duplicate",
        confirmation_required=False,
        message=message,
        speak=message,
        request_id=body.request_id,
        execution_id=execution_id,
        issued_at=issued_at,
        preview=preview,
        **_optional_response_tracking_fields(action),
    )


def _fault_unsupported_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    preview: dict[str, object] | None,
    fault: FaultDecision,
) -> ActionResponse:
    message = fault.message or "Action is not allowlisted."
    _audit_fault(
        app,
        action_id,
        action,
        body,
        fault,
        status_value="failed",
        executed=False,
        execution_id=None,
        issued_at=None,
    )
    return ActionResponse(
        ok=False,
        action_id=action_id,
        executed=False,
        status="failed",
        confirmation_required=False,
        message=message,
        speak=message,
        request_id=body.request_id,
        preview=preview,
        **_optional_response_tracking_fields(action),
        error="unsupported_action",
    )


def _audit_fault(
    app: FastAPI,
    action_id: str,
    action: ActionConfig | None,
    body: ActionRequest,
    fault: FaultDecision,
    *,
    status_value: str,
    executed: bool,
    execution_id: str | None,
    issued_at: str | None,
) -> None:
    event: dict[str, object] = {
        "event": "fault_injected",
        "action_id": action_id,
        "scenario": fault.scenario,
        "attempt": fault.attempt,
        "fault_rule_index": fault.rule_index,
        "status": status_value,
        **_request_audit_fields(body),
        "executed": executed,
        "confirmed": body.confirmed,
    }
    if execution_id is not None:
        event["execution_id"] = execution_id
    if issued_at is not None:
        event["issued_at"] = issued_at
    if action is not None:
        event["ha_script"] = action.ha_script
        event.update(_expected_effect_audit_fields(action))
    if fault.message is not None:
        event["fault_message"] = fault.message
    _audit(app, event)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _expected_effect_payload(action: ActionConfig) -> dict[str, str] | None:
    if action.expected_effect is None:
        return None
    return action.expected_effect.model_dump()


def _response_tracking_fields(action: ActionConfig) -> dict[str, object]:
    effect = _expected_effect_payload(action)
    fields: dict[str, object] = {"expected_effect": effect}
    if effect is None:
        return fields
    fields.update(
        {
            "domain": effect["domain"],
            "service": effect["service"],
            "entity_id": effect["entity_id"],
            "expected_state": effect["expected_state"],
        }
    )
    return fields


def _optional_response_tracking_fields(action: ActionConfig | None) -> dict[str, object]:
    if action is None:
        return {}
    return _response_tracking_fields(action)


def _expected_effect_audit_fields(action: ActionConfig) -> dict[str, object]:
    effect = _expected_effect_payload(action)
    if effect is None:
        return {}
    return {"expected_effect": effect}


def _audit(app: FastAPI, event: dict) -> None:
    logger = app.state.audit_logger
    if logger is not None:
        logger.write(event)


def _emit_action_event(
    app: FastAPI,
    phase: UdpEventPhase,
    action_id: str,
    action: ActionConfig,
    body: ActionRequest,
    *,
    execution_id: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> None:
    sender = app.state.udp_event_sender
    if sender is None:
        return

    try:
        sender.emit(
            phase=phase,
            action_id=action_id,
            execution_id=execution_id,
            label=action.label,
            source=body.source,
            request_id=body.request_id,
            message=message,
            error=error,
        )
    except Exception as exc:
        _audit(
            app,
            {
                "event": "udp_event_failed",
                "phase": phase,
                "action_id": action_id,
                "execution_id": execution_id,
                "source": body.source,
                "request_id": body.request_id,
                "error": str(exc),
            },
        )
