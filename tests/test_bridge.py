from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from home_control_bridge.app import create_app
from home_control_bridge.audit import JsonlAuditLogger
from home_control_bridge.config import BridgeConfig, ConfigError, get_required_secret


class FakeUdpEventSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[dict] = []
        self.fail = fail

    def emit(self, **event):
        if self.fail:
            raise OSError("udp unavailable")
        self.events.append(event)
        return event


class FakeHomeAssistant:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def check_connection(self):
        return {"ok": True, "status_code": 200}

    async def turn_on_script(self, script_entity_id: str):
        self.calls.append(script_entity_id)
        if self.fail:
            from home_control_bridge.home_assistant import HomeAssistantError

            raise HomeAssistantError("boom")
        return {"status_code": 200, "body": [{"entity_id": script_entity_id}]}


@pytest.fixture
def config(tmp_path):
    return BridgeConfig.model_validate(
        {
            "home_assistant": {
                "base_url": "http://homeassistant.local:8123",
                "token_env": "HOME_ASSISTANT_TOKEN",
            },
            "server": {
                "api_token_env": "HOME_CONTROL_API_TOKEN",
                "log_path": str(tmp_path / "events.jsonl"),
            },
            "actions": {
                "light_on": {
                    "label": "照明をつける",
                    "ha_script": "script.demo_light_on",
                    "confirm_required": False,
                    "response_text": "照明をつけました。",
                },
                "curtain_close": {
                    "label": "カーテンを閉める",
                    "ha_script": "script.curtain_close",
                    "confirm_required": True,
                    "response_text": "カーテンを閉めました。",
                },
            },
        }
    )


@pytest.fixture
def token(monkeypatch):
    value = "local-test-token-with-at-least-32-characters"
    monkeypatch.setenv("HOME_CONTROL_API_TOKEN", value)
    return value


def make_client(config, token, tmp_path, ha=None, udp=None):
    del token
    ha = ha or FakeHomeAssistant()
    logger = JsonlAuditLogger(str(tmp_path / "events.jsonl"))
    app = create_app(config=config, ha_client=ha, audit_logger=logger, udp_event_sender=udp)
    return TestClient(app), ha, tmp_path / "events.jsonl", udp


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_available_without_bridge_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["actions_count"] == 2


def test_actions_require_api_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions")

    assert response.status_code == 401


def test_actions_returns_public_allowlist(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions", headers=auth_headers(token))

    assert response.status_code == 200
    actions = response.json()
    assert {action["action_id"] for action in actions} == {"light_on", "curtain_close"}
    assert all("ha_script" not in action for action in actions)


def test_preview_logs_without_executing(config, token, tmp_path):
    client, ha, log_path, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/light_on/preview",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-1", "user_text": "照明をつけて"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["executed"] is False
    assert body["preview"]["ha_service"] == "script.turn_on"
    assert body["preview"]["ha_script"] == "script.demo_light_on"
    assert ha.calls == []

    log = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log["event"] == "preview"
    assert log["action_id"] == "light_on"
    assert log["user_text_present"] is True
    assert log["user_text_length"] == len("照明をつけて")
    assert "照明をつけて" not in json.dumps(log, ensure_ascii=False)
    assert "token" not in json.dumps(log).lower()


def test_post_body_is_optional(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post("/actions/light_on/execute", headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert ha.calls == ["script.demo_light_on"]


def test_execute_rejects_unexpected_payload_fields(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={
            "source": "dify",
            "request_id": "req-2",
            "user_text": "照明をつけて",
            "ha_script": "script.not_allowed",
            "entity_id": "lock.front_door",
        },
    )

    assert response.status_code == 422
    assert ha.calls == []


def test_unknown_action_is_not_executed(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/lock_unlock/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-3"},
    )

    assert response.status_code == 404
    assert ha.calls == []


def test_dry_run_does_not_call_home_assistant(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-4", "dry_run": True},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert response.json()["message"].startswith("dry-run:")
    assert ha.calls == []


def test_confirmation_required_action_is_blocked_until_confirmed(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    first = client.post(
        "/actions/curtain_close/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-5"},
    )
    second = client.post(
        "/actions/curtain_close/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-6", "confirmed": True},
    )
    third = client.post(
        "/actions/curtain_close/execute",
        headers=auth_headers(token),
        json={
            "source": "dify",
            "request_id": "req-7",
            "confirmed": True,
            "confirmation_token": first.json()["confirmation_token"],
        },
    )

    assert first.status_code == 200
    assert first.json()["executed"] is False
    assert first.json()["confirmation_required"] is True
    assert isinstance(first.json()["confirmation_token"], str)
    assert second.status_code == 200
    assert second.json()["executed"] is False
    assert second.json()["confirmation_required"] is True
    assert isinstance(second.json()["confirmation_token"], str)
    assert third.status_code == 200
    assert third.json()["executed"] is True
    assert ha.calls == ["script.curtain_close"]


def test_home_assistant_failure_returns_safe_response(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path, ha=FakeHomeAssistant(fail=True))

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-7"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["executed"] is False
    assert response.json()["speak"] == "家電操作に失敗しました。"
    assert response.json()["error"] == "home_assistant_request_failed"
    assert "boom" not in json.dumps(response.json(), ensure_ascii=False)


def test_execute_emits_udp_start_and_done(config, token, tmp_path):
    udp = FakeUdpEventSender()
    client, _, _, _ = make_client(config, token, tmp_path, udp=udp)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-udp-1", "user_text": "照明をつけて"},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert udp.events == [
        {
            "phase": "start",
            "action_id": "light_on",
            "label": "照明をつける",
            "source": "dify",
            "request_id": "req-udp-1",
            "message": None,
            "error": None,
        },
        {
            "phase": "done",
            "action_id": "light_on",
            "label": "照明をつける",
            "source": "dify",
            "request_id": "req-udp-1",
            "message": "照明をつけました。",
            "error": None,
        },
    ]


def test_execute_emits_udp_error_on_home_assistant_failure(config, token, tmp_path):
    udp = FakeUdpEventSender()
    client, _, _, _ = make_client(config, token, tmp_path, ha=FakeHomeAssistant(fail=True), udp=udp)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-udp-2"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert [event["phase"] for event in udp.events] == ["start", "error"]
    assert udp.events[1]["action_id"] == "light_on"
    assert udp.events[1]["message"] == "Home Assistantへの実行要求に失敗しました。"
    assert udp.events[1]["error"] == "home_assistant_request_failed"


def test_udp_failure_does_not_block_execution(config, token, tmp_path):
    udp = FakeUdpEventSender(fail=True)
    client, ha, log_path, _ = make_client(config, token, tmp_path, udp=udp)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-udp-3"},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert ha.calls == ["script.demo_light_on"]
    logs = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(log["event"] == "udp_event_failed" and log["phase"] == "start" for log in logs)
    assert any(log["event"] == "udp_event_failed" and log["phase"] == "done" for log in logs)


def test_dry_run_does_not_emit_udp(config, token, tmp_path):
    udp = FakeUdpEventSender()
    client, _, _, _ = make_client(config, token, tmp_path, udp=udp)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-udp-4", "dry_run": True},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert udp.events == []


def test_duplicate_request_id_is_not_executed_twice(config, token, tmp_path):
    client, ha, log_path, _ = make_client(config, token, tmp_path)

    first = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-duplicate"},
    )
    second = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-duplicate"},
    )

    assert first.status_code == 200
    assert first.json()["executed"] is True
    assert second.status_code == 200
    assert second.json()["executed"] is False
    assert ha.calls == ["script.demo_light_on"]
    logs = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(log["event"] == "execute_duplicate_request" for log in logs)


def test_confirm_preview_issues_one_time_confirmation_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/curtain_close/preview",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-confirm-preview"},
    )

    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert response.json()["confirmation_required"] is True
    assert isinstance(response.json()["confirmation_token"], str)


def test_placeholder_bridge_token_is_rejected(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_API_TOKEN", "change-me-local-bridge-token")

    with pytest.raises(ConfigError):
        get_required_secret("HOME_CONTROL_API_TOKEN")


def test_config_rejects_non_script_entities():
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(
            {
                "home_assistant": {"base_url": "http://homeassistant.local:8123"},
                "actions": {
                    "front_door_unlock": {
                        "label": "玄関を開ける",
                        "ha_script": "lock.front_door",
                        "response_text": "玄関を開けました。",
                    }
                },
            }
        )
