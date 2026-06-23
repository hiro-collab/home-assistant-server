from __future__ import annotations

import secrets
import os
from hashlib import sha256
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from .audit import JsonlAuditLogger
from .config import (
    ActionConfig,
    BridgeConfig,
    ConfigError,
    action_control_type,
    action_expected_states,
    action_live_test_blockers,
    action_live_test_readiness,
    action_preview_payload,
    action_proof_ceiling,
    action_public_expected_effect,
    action_public_position_proof,
    action_settle_seconds,
    action_state_authority,
    action_state_tracking_status,
    action_timeout_seconds,
    action_verification_mode,
    get_required_secret,
    load_config,
)
from .faults import FaultContext, FaultDecision, evaluate_fault
from .home_assistant import HomeAssistantClient, HomeAssistantEntityState, HomeAssistantError
from .schemas import ActionRequest, ActionResponse, ActionStateResponse, ActionSummary, HealthResponse
from .udp_events import UdpEventPhase, UdpEventSender

CONFIRMATION_TOKEN_TTL_SECONDS = 120
EXECUTION_REQUEST_TTL_SECONDS = 600
GENERIC_CONFIG_ERROR = "Bridge configuration is unavailable."
HOME_ASSISTANT_ERROR_CODE = "home_assistant_request_failed"
DRY_RUN_CONFLICT_ERROR_CODE = "dry_run_request_conflict"

OPERATOR_CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Home Control Operator</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    body {
      margin: 0;
      background: Canvas;
      color: CanvasText;
    }
    main {
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px;
    }
    h1 {
      margin: 0 0 16px;
      font-size: 1.5rem;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 18px;
    }
    input {
      min-width: min(460px, 100%);
      flex: 1 1 280px;
      padding: 8px 10px;
      border: 1px solid color-mix(in srgb, CanvasText 35%, Canvas);
      border-radius: 6px;
      font: inherit;
    }
    button {
      min-height: 36px;
      padding: 7px 11px;
      border: 1px solid color-mix(in srgb, CanvasText 35%, Canvas);
      border-radius: 6px;
      background: ButtonFace;
      color: ButtonText;
      font: inherit;
      cursor: pointer;
    }
    button.primary {
      border-color: #1d4ed8;
      background: #2563eb;
      color: white;
    }
    button.danger {
      border-color: #b91c1c;
      background: #dc2626;
      color: white;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }
    .action {
      border: 1px solid color-mix(in srgb, CanvasText 20%, Canvas);
      border-radius: 8px;
      padding: 14px;
      background: color-mix(in srgb, Canvas 94%, CanvasText);
    }
    .action h2 {
      margin: 0 0 8px;
      font-size: 1rem;
    }
    .meta {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 4px 10px;
      margin: 10px 0 12px;
      font-size: 0.9rem;
    }
    .meta dt {
      color: color-mix(in srgb, CanvasText 65%, Canvas);
    }
    .meta dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }
    .status {
      margin: 0 0 14px;
      min-height: 20px;
      color: color-mix(in srgb, CanvasText 72%, Canvas);
    }
    pre {
      margin-top: 18px;
      padding: 12px;
      max-height: 360px;
      overflow: auto;
      border: 1px solid color-mix(in srgb, CanvasText 20%, Canvas);
      border-radius: 8px;
      background: color-mix(in srgb, Canvas 88%, CanvasText);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
  </style>
</head>
<body>
<main>
  <h1>Home Control Operator</h1>
  <div class="toolbar">
    <input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="Bridge API token">
    <button id="load" class="primary" type="button">Load actions</button>
  </div>
  <p id="status" class="status">Enter the local bridge token, then load the allowlisted actions.</p>
  <section id="actions" class="grid" aria-live="polite"></section>
  <pre id="output" aria-live="polite"></pre>
</main>
<script>
(() => {
  const tokenInput = document.getElementById("token");
  const loadButton = document.getElementById("load");
  const statusNode = document.getElementById("status");
  const actionsNode = document.getElementById("actions");
  const outputNode = document.getElementById("output");

  const setStatus = (text) => {
    statusNode.textContent = text;
  };

  const show = (label, value) => {
    outputNode.textContent = `${label}\\n${JSON.stringify(value, null, 2)}`;
  };

  const requestId = (actionId, suffix) => (
    `operator-${actionId}-${suffix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  );

  const token = () => tokenInput.value.trim();

  async function callBridge(path, options = {}) {
    const currentToken = token();
    if (!currentToken) {
      throw new Error("Bridge API token is required.");
    }
    const response = await fetch(path, {
      ...options,
      headers: {
        "Authorization": `Bearer ${currentToken}`,
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.body = body;
      throw error;
    }
    return body;
  }

  const bodyFor = (actionId, suffix, extra = {}) => ({
    source: "home_control_operator_console",
    request_id: requestId(actionId, suffix),
    ...extra,
  });

  async function withResult(label, fn) {
    try {
      setStatus(`${label}...`);
      const result = await fn();
      show(label, result);
      setStatus(`${label}: done`);
      return result;
    } catch (error) {
      show(`${label}: failed`, error.body || { error: error.message });
      setStatus(`${label}: failed`);
      return null;
    }
  }

  function appendMetaRow(list, label, value) {
    if (value === null || value === undefined || value === "") {
      return;
    }
    const rendered = Array.isArray(value) ? value.join(", ") : String(value);
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = rendered;
    list.append(term, description);
  }

  function renderAction(action) {
    const article = document.createElement("article");
    article.className = "action";
    const title = document.createElement("h2");
    title.textContent = action.label;
    const actionId = document.createElement("div");
    actionId.textContent = action.action_id;
    const meta = document.createElement("dl");
    meta.className = "meta";
    appendMetaRow(meta, "control", action.control_type);
    appendMetaRow(meta, "tracking", action.state_tracking);
    appendMetaRow(meta, "verification", action.verification_mode);
    appendMetaRow(meta, "readiness", action.live_test_readiness);
    appendMetaRow(meta, "proof", action.proof_ceiling);
    appendMetaRow(meta, "restore", action.restore_action_id);
    appendMetaRow(meta, "stop", action.stop_action_id);
    appendMetaRow(meta, "wait", `${action.settle_seconds || 0}s / ${action.timeout_seconds || 0}s`);
    appendMetaRow(meta, "blockers", action.live_test_blockers);
    article.append(title, actionId, meta);
    const row = document.createElement("div");
    row.className = "row";

    const stateButton = document.createElement("button");
    stateButton.type = "button";
    stateButton.textContent = "State";
    stateButton.onclick = () => withResult(`state ${action.action_id}`, () => callBridge(`/actions/${action.action_id}/state`));
    row.appendChild(stateButton);

    const previewButton = document.createElement("button");
    previewButton.type = "button";
    previewButton.textContent = "Preview";
    previewButton.onclick = () => withResult(`preview ${action.action_id}`, () => callBridge(
      `/actions/${action.action_id}/preview`,
      { method: "POST", body: JSON.stringify(bodyFor(action.action_id, "preview")) },
    ));
    row.appendChild(previewButton);

    const dryRunButton = document.createElement("button");
    dryRunButton.type = "button";
    dryRunButton.textContent = "Dry run";
    dryRunButton.onclick = () => withResult(`dry run ${action.action_id}`, () => callBridge(
      `/actions/${action.action_id}/execute`,
      { method: "POST", body: JSON.stringify(bodyFor(action.action_id, "dry-run", { dry_run: true })) },
    ));
    row.appendChild(dryRunButton);

    const executeButton = document.createElement("button");
    executeButton.type = "button";
    executeButton.className = "danger";
    executeButton.textContent = "Execute";
    executeButton.onclick = async () => {
      const result = await withResult(`execute ${action.action_id}`, () => callBridge(
        `/actions/${action.action_id}/execute`,
        { method: "POST", body: JSON.stringify(bodyFor(action.action_id, "execute")) },
      ));
      if (result && result.confirmation_required && result.confirmation_token) {
        const confirmButton = document.createElement("button");
        confirmButton.type = "button";
        confirmButton.className = "danger";
        confirmButton.textContent = "Confirm execute";
        confirmButton.onclick = () => withResult(`confirm ${action.action_id}`, () => callBridge(
          `/actions/${action.action_id}/execute`,
          {
            method: "POST",
            body: JSON.stringify(bodyFor(action.action_id, "confirm", {
              confirmed: true,
              confirmation_token: result.confirmation_token,
            })),
          },
        ));
        row.appendChild(confirmButton);
      }
    };
    row.appendChild(executeButton);

    article.appendChild(row);
    return article;
  }

  async function loadActions() {
    const actions = await withResult("load actions", () => callBridge("/actions"));
    actionsNode.textContent = "";
    if (!Array.isArray(actions)) {
      return;
    }
    for (const action of actions) {
      actionsNode.appendChild(renderAction(action));
    }
  }

  loadButton.addEventListener("click", loadActions);
})();
</script>
</body>
</html>
"""


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
    app.state.dry_run_requests = {}
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
                config_profile="unknown",
                demo_mappings_present=False,
                light_demo_mappings_present=False,
                fault_mode=False,
                fault_rules_count=0,
            )
        if app.state.config_error:
            return HealthResponse(
                ok=False,
                status="config_error",
                home_assistant={"ok": False, "error": GENERIC_CONFIG_ERROR},
                actions_count=len(app.state.config.actions),
                config_profile=_config_profile(app.state.config),
                demo_mappings_present=_demo_mappings_present(app.state.config),
                light_demo_mappings_present=_light_demo_mappings_present(app.state.config),
                fault_mode=False,
                fault_rules_count=0,
            )
        ha_status = await app.state.ha_client.check_connection()
        ok = bool(ha_status.get("ok"))
        return HealthResponse(
            ok=ok,
            status="ok" if ok else "degraded",
            home_assistant=ha_status,
            actions_count=len(app.state.config.actions),
            config_profile=_config_profile(app.state.config),
            demo_mappings_present=_demo_mappings_present(app.state.config),
            light_demo_mappings_present=_light_demo_mappings_present(app.state.config),
            fault_mode=False,
            fault_rules_count=0,
        )

    @app.get("/operator", response_class=HTMLResponse, include_in_schema=False)
    async def operator_console() -> HTMLResponse:
        return HTMLResponse(OPERATOR_CONSOLE_HTML)

    @app.get("/actions", response_model=list[ActionSummary], dependencies=[Depends(require_auth)])
    async def list_actions() -> list[ActionSummary]:
        config = require_config()
        return [
            ActionSummary(
                action_id=action_id,
                label=action.label,
                confirm_required=action.confirm_required,
                response_text=action.response_text,
                control_type=action_control_type(action),
                state_authority=action_state_authority(action),
                verification_mode=action_verification_mode(action),
                state_tracking=action_state_tracking_status(action),
                verification=action.verification.model_dump(exclude_none=True) if action.verification is not None else None,
                expected_effect=_expected_effect_payload(action),
                position_proof=_position_proof_payload(action),
                expected_states=action_expected_states(action),
                settle_seconds=action_settle_seconds(action),
                timeout_seconds=action_timeout_seconds(action),
                proof_ceiling=action_proof_ceiling(action),
                live_test_candidate=action.live_test_candidate,
                live_test_readiness=action_live_test_readiness(action),
                live_test_blockers=action_live_test_blockers(action),
                restore_action_id=action.restore_action_id,
                stop_action_id=action.stop_action_id,
                terminal_action=action.terminal_action,
                safety_requirements=list(action.safety_requirements),
            )
            for action_id, action in sorted(config.actions.items())
        ]

    @app.get(
        "/actions/{action_id}/state",
        response_model=ActionStateResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_action_state(action_id: str) -> ActionStateResponse:
        config = require_config()
        action = _get_action(config, action_id)
        tracking_status = action_state_tracking_status(action)
        tracking_fields = _state_tracking_summary_fields(action)
        if tracking_status != "tracked":
            return ActionStateResponse(
                ok=False,
                action_id=action_id,
                status=tracking_status,
                **tracking_fields,
            )

        expected_state = action.expected_effect.expected_state
        expected_states = action_expected_states(action)
        try:
            entity_state = await app.state.ha_client.get_entity_state_snapshot(action.expected_effect.entity_id)
        except HomeAssistantError:
            return ActionStateResponse(
                ok=False,
                action_id=action_id,
                status="unavailable",
                expected_state=expected_state,
                expected_states=expected_states,
                **tracking_fields,
                **_empty_position_state_fields(action),
            )

        actual_state = entity_state.state
        position_fields = _position_state_fields(action, entity_state)
        position_status = position_fields["position_status"]
        state_matched = actual_state in expected_states
        if position_status == "unavailable":
            status = "position_unavailable"
        elif state_matched and position_status in (None, "matched"):
            status = "matched"
        else:
            status = "mismatch"
        return ActionStateResponse(
            ok=status == "matched",
            action_id=action_id,
            status=status,
            expected_state=expected_state,
            expected_states=expected_states,
            actual_state=actual_state,
            **tracking_fields,
            **position_fields,
        )

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

        dry_run_record = _get_dry_run_request(app, body.request_id)
        if body.dry_run and dry_run_record is not None:
            if dry_run_record["fingerprint"] == _dry_run_fingerprint(action_id, body):
                return _dry_run_duplicate_response(app, action_id, action, body, preview)
            return _dry_run_conflict_response(app, action_id, action, body, preview)
        if not body.dry_run and dry_run_record is not None:
            return _dry_run_conflict_response(app, action_id, action, body, preview)

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
            _register_dry_run_request(app, action_id, body)
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


def _get_dry_run_request(app: FastAPI, request_id: str | None) -> dict[str, object] | None:
    _prune_dry_run_requests(app)
    if not request_id:
        return None
    record = app.state.dry_run_requests.get(request_id)
    if record is None:
        return None
    return {"fingerprint": record["fingerprint"]}


def _register_dry_run_request(app: FastAPI, action_id: str, body: ActionRequest) -> None:
    _prune_dry_run_requests(app)
    if not body.request_id:
        return
    app.state.dry_run_requests[body.request_id] = {
        "expires_at": monotonic() + EXECUTION_REQUEST_TTL_SECONDS,
        "fingerprint": _dry_run_fingerprint(action_id, body),
    }


def _prune_dry_run_requests(app: FastAPI) -> None:
    now = monotonic()
    for key, record in list(app.state.dry_run_requests.items()):
        expires_at = record["expires_at"]
        if expires_at < now:
            app.state.dry_run_requests.pop(key, None)


def _dry_run_fingerprint(action_id: str, body: ActionRequest) -> dict[str, object]:
    user_text_hash = None
    if body.user_text is not None:
        user_text_hash = sha256(body.user_text.encode("utf-8")).hexdigest()
    return {
        "action_id": action_id,
        "source": body.source,
        "dry_run": body.dry_run,
        "confirmed": body.confirmed,
        "confirmation_token_present": body.confirmation_token is not None,
        "user_text_present": body.user_text is not None,
        "user_text_length": len(body.user_text) if body.user_text is not None else None,
        "user_text_sha256": user_text_hash,
    }


def _get_action(config: BridgeConfig, action_id: str):
    action = config.actions.get(action_id)
    if action is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action is not allowlisted.")
    return action


def _demo_mappings_present(config: BridgeConfig) -> bool:
    return any(action.ha_script.startswith("script.demo_") for action in config.actions.values())


def _light_demo_mappings_present(config: BridgeConfig) -> bool:
    return any(
        action_id in {"light_on", "light_off"} and action.ha_script.startswith("script.demo_")
        for action_id, action in config.actions.items()
    )


def _config_profile(config: BridgeConfig) -> str:
    configured = os.environ.get("HOME_CONTROL_CONFIG", "").strip()
    if configured:
        normalized = configured.replace("\\", "/").lower()
        name = Path(normalized).name
        if normalized.endswith("local/env/home-control.live.yaml"):
            return "local"
        if "example" in name or "demo" in name:
            return "demo"
        if "local" in name or "private" in name or "/local/" in normalized:
            return "private"
        if "generated" in name or ".cache/" in normalized:
            return "generated"
        if name == "home-control.yaml" and _light_demo_mappings_present(config):
            return "demo"
        return "custom"
    if _light_demo_mappings_present(config):
        return "demo"
    if _demo_mappings_present(config):
        return "custom"
    return "unknown"


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


def _dry_run_duplicate_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig,
    body: ActionRequest,
    preview: dict[str, object],
) -> ActionResponse:
    message = "同じ request_id の dry-run はすでに受け付け済みです。"
    _audit(
        app,
        {
            "event": "execute_dry_run_duplicate",
            "action_id": action_id,
            "status": "duplicate",
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
        status="duplicate",
        confirmation_required=action.confirm_required,
        message=message,
        speak=message,
        request_id=body.request_id,
        preview=preview,
        **_response_tracking_fields(action),
    )


def _dry_run_conflict_response(
    app: FastAPI,
    action_id: str,
    action: ActionConfig,
    body: ActionRequest,
    preview: dict[str, object],
) -> ActionResponse:
    message = "同じ request_id の dry-run と異なる操作要求は受け付けられません。"
    _audit(
        app,
        {
            "event": "execute_dry_run_conflict",
            "action_id": action_id,
            "status": "failed",
            "error": DRY_RUN_CONFLICT_ERROR_CODE,
            **_request_audit_fields(body),
            "executed": False,
            "confirm_required": action.confirm_required,
            "confirmed": body.confirmed,
            "ha_script": action.ha_script,
        },
    )
    return ActionResponse(
        ok=False,
        action_id=action_id,
        executed=False,
        status="failed",
        confirmation_required=action.confirm_required,
        message=message,
        speak=message,
        request_id=body.request_id,
        preview=preview,
        **_response_tracking_fields(action),
        error=DRY_RUN_CONFLICT_ERROR_CODE,
    )


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
    return action_public_expected_effect(action)


def _position_proof_payload(action: ActionConfig) -> dict[str, object] | None:
    return action_public_position_proof(action)


def _state_tracking_summary_fields(action: ActionConfig) -> dict[str, str]:
    return {
        "control_type": action_control_type(action),
        "state_authority": action_state_authority(action),
        "verification_mode": action_verification_mode(action),
        "state_tracking": action_state_tracking_status(action),
    }


def _response_tracking_fields(action: ActionConfig) -> dict[str, object]:
    fields: dict[str, object] = _state_tracking_summary_fields(action)
    fields["expected_states"] = action_expected_states(action)
    fields["settle_seconds"] = action_settle_seconds(action)
    fields["timeout_seconds"] = action_timeout_seconds(action)
    fields["position_proof"] = _position_proof_payload(action)
    fields["proof_ceiling"] = action_proof_ceiling(action)
    fields["live_test_candidate"] = action.live_test_candidate
    fields["live_test_readiness"] = action_live_test_readiness(action)
    fields["live_test_blockers"] = action_live_test_blockers(action)
    fields["restore_action_id"] = action.restore_action_id
    fields["stop_action_id"] = action.stop_action_id
    fields["terminal_action"] = action.terminal_action
    fields["safety_requirements"] = list(action.safety_requirements)
    effect = _expected_effect_payload(action)
    fields["expected_effect"] = effect
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
    fields: dict[str, object] = _state_tracking_summary_fields(action)
    fields["expected_states"] = action_expected_states(action)
    fields["settle_seconds"] = action_settle_seconds(action)
    fields["timeout_seconds"] = action_timeout_seconds(action)
    fields["position_proof"] = _position_proof_payload(action)
    effect = _expected_effect_payload(action)
    if effect is None:
        return fields
    fields["expected_effect"] = effect
    return fields


def _empty_position_state_fields(action: ActionConfig) -> dict[str, object]:
    proof = _position_proof_payload(action)
    if proof is None:
        return {
            "position_attribute": None,
            "expected_position_min": None,
            "expected_position_max": None,
            "actual_position": None,
            "position_status": None,
        }
    return {
        "position_attribute": proof["attribute"],
        "expected_position_min": proof.get("min"),
        "expected_position_max": proof.get("max"),
        "actual_position": None,
        "position_status": "unavailable",
    }


def _position_state_fields(action: ActionConfig, entity_state: HomeAssistantEntityState) -> dict[str, object]:
    proof = _position_proof_payload(action)
    if proof is None:
        return _empty_position_state_fields(action)

    position = _coerce_position(entity_state.attributes.get(proof["attribute"]))
    min_position = proof.get("min")
    max_position = proof.get("max")
    matched = position is not None
    if matched and min_position is not None:
        matched = position >= float(min_position)
    if matched and max_position is not None:
        matched = position <= float(max_position)

    return {
        "position_attribute": proof["attribute"],
        "expected_position_min": min_position,
        "expected_position_max": max_position,
        "actual_position": position,
        "position_status": ("matched" if matched else "mismatch") if position is not None else "unavailable",
    }


def _coerce_position(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        position = float(value)
    elif isinstance(value, str):
        try:
            position = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not isfinite(position):
        return None
    return position


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
