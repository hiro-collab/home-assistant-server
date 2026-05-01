from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .audit import JsonlAuditLogger
from .config import BridgeConfig, ConfigError, action_preview_payload, get_required_env, load_config
from .home_assistant import HomeAssistantClient, HomeAssistantError
from .schemas import ActionRequest, ActionResponse, ActionSummary, HealthResponse


def create_app(
    config: BridgeConfig | None = None,
    ha_client: HomeAssistantClient | None = None,
    audit_logger: JsonlAuditLogger | None = None,
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
    app.state.audit_logger = audit_logger if audit_logger is not None and config else (
        JsonlAuditLogger(config.server.log_path) if config else None
    )
    app.state.ha_client = ha_client

    if config and app.state.ha_client is None:
        try:
            ha_token = get_required_env(config.home_assistant.token_env)
            app.state.ha_client = HomeAssistantClient(config.home_assistant, ha_token)
        except ConfigError as exc:
            app.state.config_error = str(exc)

    @app.exception_handler(ConfigError)
    async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
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
        expected = get_required_env(config.server.api_token_env)
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
                home_assistant={"ok": False, "error": app.state.config_error or "Config is not loaded."},
                actions_count=0,
            )
        if app.state.config_error:
            return HealthResponse(
                ok=False,
                status="config_error",
                home_assistant={"ok": False, "error": app.state.config_error},
                actions_count=len(app.state.config.actions),
            )
        ha_status = await app.state.ha_client.check_connection()
        ok = bool(ha_status.get("ok"))
        return HealthResponse(
            ok=ok,
            status="ok" if ok else "degraded",
            home_assistant=ha_status,
            actions_count=len(app.state.config.actions),
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
        _audit(
            app,
            {
                "event": "preview",
                "action_id": action_id,
                "source": body.source,
                "request_id": body.request_id,
                "user_text": body.user_text,
                "executed": False,
                "confirm_required": action.confirm_required,
                "ha_script": action.ha_script,
            },
        )
        message = f"{action.label}を実行します。よろしいですか？" if action.confirm_required else action.response_text
        return ActionResponse(
            ok=True,
            action_id=action_id,
            executed=False,
            confirmation_required=action.confirm_required,
            message=message,
            speak=message,
            request_id=body.request_id,
            preview=preview,
        )

    @app.post(
        "/actions/{action_id}/execute",
        response_model=ActionResponse,
        dependencies=[Depends(require_auth)],
    )
    async def execute_action(action_id: str, body: ActionRequest | None = None) -> ActionResponse:
        body = body or ActionRequest()
        config = require_config()
        action = _get_action(config, action_id)
        preview = action_preview_payload(action_id, action)

        if action.confirm_required and not body.confirmed:
            message = f"{action.label}には確認が必要です。実行する場合は confirmed=true を指定してください。"
            _audit(
                app,
                {
                    "event": "execute_blocked_confirmation",
                    "action_id": action_id,
                    "source": body.source,
                    "request_id": body.request_id,
                    "user_text": body.user_text,
                    "executed": False,
                    "confirm_required": True,
                    "ha_script": action.ha_script,
                },
            )
            return ActionResponse(
                ok=True,
                action_id=action_id,
                executed=False,
                confirmation_required=True,
                message=message,
                speak=message,
                request_id=body.request_id,
                preview=preview,
            )

        if body.dry_run:
            message = f"dry-run: {action.label}を実行予定です。"
            _audit(
                app,
                {
                    "event": "execute_dry_run",
                    "action_id": action_id,
                    "source": body.source,
                    "request_id": body.request_id,
                    "user_text": body.user_text,
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
                confirmation_required=action.confirm_required,
                message=message,
                speak=message,
                request_id=body.request_id,
                preview=preview,
            )

        try:
            ha_result = await app.state.ha_client.turn_on_script(action.ha_script)
        except HomeAssistantError as exc:
            _audit(
                app,
                {
                    "event": "execute_failed",
                    "action_id": action_id,
                    "source": body.source,
                    "request_id": body.request_id,
                    "user_text": body.user_text,
                    "executed": False,
                    "confirm_required": action.confirm_required,
                    "confirmed": body.confirmed,
                    "ha_script": action.ha_script,
                    "error": str(exc),
                },
            )
            return ActionResponse(
                ok=False,
                action_id=action_id,
                executed=False,
                confirmation_required=action.confirm_required,
                message="Home Assistantへの実行要求に失敗しました。",
                speak="家電操作に失敗しました。",
                request_id=body.request_id,
                preview=preview,
                error=str(exc),
            )

        _audit(
            app,
            {
                "event": "execute_succeeded",
                "action_id": action_id,
                "source": body.source,
                "request_id": body.request_id,
                "user_text": body.user_text,
                "executed": True,
                "confirm_required": action.confirm_required,
                "confirmed": body.confirmed,
                "ha_script": action.ha_script,
                "ha_status_code": ha_result.get("status_code"),
            },
        )
        return ActionResponse(
            ok=True,
            action_id=action_id,
            executed=True,
            confirmation_required=action.confirm_required,
            message=action.response_text,
            speak=action.response_text,
            request_id=body.request_id,
            preview=preview,
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


def _get_action(config: BridgeConfig, action_id: str):
    action = config.actions.get(action_id)
    if action is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action is not allowlisted.")
    return action


def _audit(app: FastAPI, event: dict) -> None:
    logger = app.state.audit_logger
    if logger is not None:
        logger.write(event)
