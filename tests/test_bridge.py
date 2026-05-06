from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from home_control_bridge.app import create_app
from home_control_bridge.audit import JsonlAuditLogger
from home_control_bridge.config import BridgeConfig, ConfigError, get_required_secret
from home_control_bridge.faults import FaultContext, MAX_FAULT_ATTEMPT_STATE, evaluate_fault


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
                    "expected_effect": {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_id": "light.demo_room",
                        "expected_state": "on",
                    },
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
    monkeypatch.delenv("HOME_CONTROL_FAULT_MODE", raising=False)
    return value


@pytest.fixture
def fault_mode(monkeypatch, token):
    del token
    monkeypatch.setenv("HOME_CONTROL_FAULT_MODE", "1")


def make_client(config, token, tmp_path, ha=None, udp=None):
    del token
    ha = ha or FakeHomeAssistant()
    logger = JsonlAuditLogger(str(tmp_path / "events.jsonl"))
    app = create_app(config=config, ha_client=ha, audit_logger=logger, udp_event_sender=udp)
    return TestClient(app), ha, tmp_path / "events.jsonl", udp


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_uuid(value: str) -> None:
    assert str(UUID(value)) == value


def config_with_faults(config, rules, *, enabled: bool = True):
    raw = config.model_dump(mode="json")
    raw["faults"] = {
        "enabled": enabled,
        "rules": rules,
    }
    return BridgeConfig.model_validate(raw)


def read_logs(log_path):
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_health_is_available_without_bridge_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["actions_count"] == 2
    assert response.json()["fault_mode"] is False
    assert response.json()["fault_rules_count"] == 0


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


def test_execute_returns_tracking_metadata_and_logs_it(config, token, tmp_path):
    client, _, log_path, _ = make_client(config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-track-1", "user_text": "照明をつけて"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action_id"] == "light_on"
    assert_uuid(body["execution_id"])
    assert body["issued_at"].endswith("+00:00")
    assert body["status"] == "submitted"
    assert body["domain"] == "light"
    assert body["service"] == "turn_on"
    assert body["entity_id"] == "light.demo_room"
    assert body["expected_state"] == "on"
    assert body["expected_effect"] == {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.demo_room",
        "expected_state": "on",
    }

    log = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log["event"] == "execute_succeeded"
    assert log["action_id"] == "light_on"
    assert log["execution_id"] == body["execution_id"]
    assert log["issued_at"] == body["issued_at"]
    assert log["status"] == "submitted"
    assert log["expected_effect"] == body["expected_effect"]


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
    execution_id = response.json()["execution_id"]
    assert udp.events == [
        {
            "phase": "start",
            "action_id": "light_on",
            "execution_id": execution_id,
            "label": "照明をつける",
            "source": "dify",
            "request_id": "req-udp-1",
            "message": None,
            "error": None,
        },
        {
            "phase": "done",
            "action_id": "light_on",
            "execution_id": execution_id,
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
    assert_uuid(response.json()["execution_id"])
    assert [event["phase"] for event in udp.events] == ["start", "error"]
    assert udp.events[0]["execution_id"] == response.json()["execution_id"]
    assert udp.events[1]["execution_id"] == response.json()["execution_id"]
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
    assert second.json()["status"] == "duplicate"
    assert second.json()["execution_id"] == first.json()["execution_id"]
    assert second.json()["issued_at"] == first.json()["issued_at"]
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


def test_fault_mode_off_ignores_configured_faults(config, token, tmp_path):
    fault_config = config_with_faults(
        config,
        [
            {
                "match": {"action_id": "light_on"},
                "scenario": "fail_always",
                "message": "simulated failure",
            }
        ],
        enabled=False,
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fault-off"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "submitted"
    assert ha.calls == ["script.demo_light_on"]
    assert all(log["event"] != "fault_injected" for log in read_logs(log_path))


def test_fault_mode_requires_config_and_env(config, token, tmp_path, monkeypatch):
    fault_config = config_with_faults(
        config,
        [{"match": {"source": "dify", "action_id": "light_on"}, "scenario": "always_success"}],
        enabled=False,
    )
    monkeypatch.setenv("HOME_CONTROL_FAULT_MODE", "1")
    client, ha, _, _ = make_client(fault_config, token, tmp_path)

    health = client.get("/health")
    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fault-env"},
    )

    assert health.status_code == 200
    assert health.json()["fault_mode"] is False
    assert health.json()["fault_rules_count"] == 0
    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
    assert ha.calls == ["script.demo_light_on"]


def test_fault_always_success_returns_submitted_without_home_assistant(config, token, fault_mode, tmp_path):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [{"match": {"source": "dify", "action_id": "light_on"}, "scenario": "always_success"}],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fault-success"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["executed"] is True
    assert body["status"] == "submitted"
    assert_uuid(body["execution_id"])
    assert body["expected_state"] == "on"
    assert ha.calls == []
    logs = read_logs(log_path)
    assert logs[0]["event"] == "fault_injected"
    assert logs[0]["scenario"] == "always_success"
    assert logs[0]["attempt"] == 1
    assert logs[0]["source"] == "dify"
    assert logs[0]["action_id"] == "light_on"


@pytest.mark.parametrize(
    ("scenario", "expected_statuses"),
    [
        ("fail_once_then_success", ["failed", "submitted"]),
        ("fail_twice_then_success", ["failed", "failed", "submitted"]),
        ("timeout_once", ["failed", "submitted"]),
    ],
)
def test_fault_transient_scenarios_track_attempts_by_normalized_request_id(
    config,
    token,
    fault_mode,
    tmp_path,
    scenario,
    expected_statuses,
):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [
            {
                "match": {
                    "action_id": "light_on",
                    "request_id_regex": "^workflow-1",
                },
                "scenario": scenario,
                "message": "simulated transient failure",
            }
        ],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    responses = [
        client.post(
            "/actions/light_on/execute",
            headers=auth_headers(token),
            json={"source": "dify", "request_id": f"workflow-1-attempt-{index}"},
        )
        for index in range(1, len(expected_statuses) + 1)
    ]

    assert [response.status_code for response in responses] == [200] * len(expected_statuses)
    assert [response.json()["status"] for response in responses] == expected_statuses
    assert [response.json()["ok"] for response in responses] == [
        status == "submitted" for status in expected_statuses
    ]
    assert ha.calls == []
    assert [log["attempt"] for log in read_logs(log_path)] == list(range(1, len(expected_statuses) + 1))


def test_fault_fail_always_returns_failed_without_home_assistant(config, token, fault_mode, tmp_path):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [{"match": {"action_id": "light_on", "user_text_contains": "照明"}, "scenario": "fail_always"}],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    response = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fail-always", "user_text": "照明をつけて"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "home_assistant_request_failed"
    assert ha.calls == []
    assert read_logs(log_path)[0]["scenario"] == "fail_always"


def test_fault_confirmation_required_uses_one_time_token(config, token, fault_mode, tmp_path):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [{"match": {"action_id": "light_on"}, "scenario": "confirmation_required"}],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    first = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fault-confirm-1"},
    )
    second = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-fault-confirm-2", "confirmed": True},
    )
    third = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={
            "source": "dify",
            "request_id": "req-fault-confirm-3",
            "confirmed": True,
            "confirmation_token": first.json()["confirmation_token"],
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "confirmation_required"
    assert isinstance(first.json()["confirmation_token"], str)
    assert second.status_code == 200
    assert second.json()["status"] == "confirmation_required"
    assert third.status_code == 200
    assert third.json()["status"] == "submitted"
    assert_uuid(third.json()["execution_id"])
    assert ha.calls == []
    assert [log["status"] for log in read_logs(log_path)] == [
        "confirmation_required",
        "confirmation_required",
        "submitted",
    ]


def test_fault_unsupported_action_can_simulate_unallowlisted_response(config, token, fault_mode, tmp_path):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [
            {
                "match": {"action_id": "unknown_action", "source": "dify"},
                "scenario": "unsupported_action",
            }
        ],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    response = client.post(
        "/actions/unknown_action/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-unsupported"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "unsupported_action"
    assert ha.calls == []
    assert read_logs(log_path)[0]["scenario"] == "unsupported_action"


def test_fault_duplicate_scenario_does_not_break_existing_duplicate_tracking(config, token, fault_mode, tmp_path):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [
            {
                "match": {"action_id": "light_on", "request_id_suffix": "-sim-dup"},
                "scenario": "duplicate",
            }
        ],
    )
    client, ha, log_path, _ = make_client(fault_config, token, tmp_path)

    simulated = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-sim-dup"},
    )
    first_real = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-real-dup"},
    )
    second_real = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-real-dup"},
    )

    assert simulated.status_code == 200
    assert simulated.json()["status"] == "duplicate"
    assert first_real.status_code == 200
    assert first_real.json()["status"] == "submitted"
    assert second_real.status_code == 200
    assert second_real.json()["status"] == "duplicate"
    assert second_real.json()["execution_id"] == first_real.json()["execution_id"]
    assert ha.calls == ["script.demo_light_on"]
    logs = read_logs(log_path)
    assert logs[0]["event"] == "fault_injected"
    assert any(log["event"] == "execute_duplicate_request" for log in logs)


def test_fault_attempt_state_is_bounded(config, fault_mode):
    del fault_mode
    fault_config = config_with_faults(
        config,
        [{"match": {"action_id": "light_on"}, "scenario": "fail_once_then_success"}],
    )
    state = {}

    for index in range(MAX_FAULT_ATTEMPT_STATE + 20):
        evaluate_fault(
            fault_config,
            state,
            FaultContext(
                action_id="light_on",
                source="dify",
                request_id=f"req-{index}",
                user_text=None,
                confirmed=False,
            ),
        )

    assert len(state) <= MAX_FAULT_ATTEMPT_STATE


def test_config_rejects_potentially_catastrophic_fault_regex(config):
    with pytest.raises(ValidationError):
        config_with_faults(
            config,
            [
                {
                    "match": {"user_text_regex": "(a+)+$"},
                    "scenario": "fail_always",
                }
            ],
        )


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
