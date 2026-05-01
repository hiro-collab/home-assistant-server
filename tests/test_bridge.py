from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from home_control_bridge.app import create_app
from home_control_bridge.audit import JsonlAuditLogger
from home_control_bridge.config import BridgeConfig


class FakeHomeAssistant:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.service_calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def check_connection(self):
        return {"ok": True, "status_code": 200}

    async def turn_on_script(self, script_entity_id: str):
        return await self.call_service("script.turn_on", {"entity_id": script_entity_id})

    async def call_service(self, service_name: str, payload: dict):
        if self.fail:
            from home_control_bridge.home_assistant import HomeAssistantError

            raise HomeAssistantError("boom")
        self.service_calls.append((service_name, payload))
        if service_name == "script.turn_on":
            self.calls.append(str(payload.get("entity_id", "")))
        return {"status_code": 200, "body": [payload]}


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
                "fan_on": {
                    "label": "扇風機をつける",
                    "ha_service": "fan.turn_on",
                    "entity_id": "fan.living_room",
                    "confirm_required": False,
                    "response_text": "扇風機をつけました。",
                },
            },
        }
    )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_API_TOKEN", "test-token")
    return "test-token"


def make_client(config, token, tmp_path, ha=None):
    del token
    ha = ha or FakeHomeAssistant()
    logger = JsonlAuditLogger(str(tmp_path / "events.jsonl"))
    app = create_app(config=config, ha_client=ha, audit_logger=logger)
    return TestClient(app), ha, tmp_path / "events.jsonl"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_available_without_bridge_token(config, token, tmp_path):
    client, _, _ = make_client(config, token, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["actions_count"] == 3


def test_actions_require_api_token(config, token, tmp_path):
    client, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions")

    assert response.status_code == 401


def test_actions_returns_public_allowlist(config, token, tmp_path):
    client, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions", headers=auth_headers(token))

    assert response.status_code == 200
    actions = response.json()
    assert {action["action_id"] for action in actions} == {"light_on", "curtain_close", "fan_on"}
    assert all("ha_script" not in action for action in actions)


def test_preview_logs_without_executing(config, token, tmp_path):
    client, ha, log_path = make_client(config, token, tmp_path)

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
    assert "token" not in json.dumps(log).lower()


def test_preview_direct_action_reports_configured_service(config, token, tmp_path):
    client, ha, log_path = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/fan_on/preview",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-direct-1", "user_text": "扇風機つけて"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["executed"] is False
    assert body["preview"]["ha_service"] == "fan.turn_on"
    assert body["preview"]["ha_endpoint"] == "/api/services/fan/turn_on"
    assert body["preview"]["entity_id"] == "fan.living_room"
    assert "ha_script" not in body["preview"]
    assert ha.service_calls == []

    log = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log["event"] == "preview"
    assert log["ha_service"] == "fan.turn_on"
    assert log["entity_id"] == "fan.living_room"


def test_post_body_is_optional(config, token, tmp_path):
    client, ha, _ = make_client(config, token, tmp_path)

    response = client.post("/actions/light_on/execute", headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert ha.calls == ["script.demo_light_on"]


def test_execute_calls_only_configured_script(config, token, tmp_path):
    client, ha, _ = make_client(config, token, tmp_path)

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

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert response.json()["speak"] == "照明をつけました。"
    assert ha.calls == ["script.demo_light_on"]


def test_execute_direct_action_calls_only_configured_service(config, token, tmp_path):
    client, ha, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/fan_on/execute",
        headers=auth_headers(token),
        json={
            "source": "dify",
            "request_id": "req-direct-2",
            "user_text": "扇風機つけて",
            "ha_service": "lock.unlock",
            "entity_id": "lock.front_door",
        },
    )

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert response.json()["speak"] == "扇風機をつけました。"
    assert ha.service_calls == [("fan.turn_on", {"entity_id": "fan.living_room"})]


def test_unknown_action_is_not_executed(config, token, tmp_path):
    client, ha, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/lock_unlock/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-3"},
    )

    assert response.status_code == 404
    assert ha.calls == []


def test_dry_run_does_not_call_home_assistant(config, token, tmp_path):
    client, ha, _ = make_client(config, token, tmp_path)

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
    client, ha, _ = make_client(config, token, tmp_path)

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

    assert first.status_code == 200
    assert first.json()["executed"] is False
    assert first.json()["confirmation_required"] is True
    assert second.status_code == 200
    assert second.json()["executed"] is True
    assert ha.calls == ["script.curtain_close"]


def test_home_assistant_failure_returns_safe_response(config, token, tmp_path):
    client, _, _ = make_client(config, token, tmp_path, ha=FakeHomeAssistant(fail=True))

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-7"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["executed"] is False
    assert response.json()["speak"] == "家電操作に失敗しました。"


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


def test_config_rejects_unapproved_direct_services():
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(
            {
                "home_assistant": {"base_url": "http://homeassistant.local:8123"},
                "actions": {
                    "front_door_unlock": {
                        "label": "玄関を開ける",
                        "ha_service": "lock.unlock",
                        "entity_id": "lock.front_door",
                        "response_text": "玄関を開けました。",
                    }
                },
            }
        )


def test_config_requires_direct_service_entity_domain_match():
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(
            {
                "home_assistant": {"base_url": "http://homeassistant.local:8123"},
                "actions": {
                    "bad_light": {
                        "label": "ライトをつける",
                        "ha_service": "light.turn_on",
                        "entity_id": "switch.raito",
                        "response_text": "ライトをつけました。",
                    }
                },
            }
        )
