from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from home_control_bridge.app import create_app
from home_control_bridge.audit import JsonlAuditLogger
from home_control_bridge.config import (
    BridgeConfig,
    ConfigError,
    action_preview_payload,
    get_required_secret,
    load_config,
)
from home_control_bridge.faults import FaultContext, MAX_FAULT_ATTEMPT_STATE, evaluate_fault
from home_control_bridge.home_assistant import HomeAssistantEntityState


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
    def __init__(self, *, fail: bool = False, fail_state: bool = False, states: dict[str, object] | None = None) -> None:
        self.calls: list[str] = []
        self.state_calls: list[str] = []
        self.fail = fail
        self.fail_state = fail_state
        self.states = states or {"light.demo_room": "on"}

    async def check_connection(self):
        return {"ok": True, "status_code": 200}

    async def turn_on_script(self, script_entity_id: str):
        self.calls.append(script_entity_id)
        if self.fail:
            from home_control_bridge.home_assistant import HomeAssistantError

            raise HomeAssistantError("boom")
        return {"status_code": 200, "body": [{"entity_id": script_entity_id}]}

    async def get_entity_state(self, entity_id: str):
        return (await self.get_entity_state_snapshot(entity_id)).state

    async def get_entity_state_snapshot(self, entity_id: str):
        self.state_calls.append(entity_id)
        if self.fail_state:
            from home_control_bridge.home_assistant import HomeAssistantError

            raise HomeAssistantError("state unavailable")
        value = self.states.get(entity_id, "unknown")
        if isinstance(value, dict):
            state = value.get("state", "unknown")
            attributes = value.get("attributes", {})
        else:
            state = value
            attributes = {}
        return HomeAssistantEntityState(state=str(state), attributes=attributes if isinstance(attributes, dict) else {})


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
                    "control_type": "stateful_target",
                    "state_authority": "ha_entity",
                    "verification": {"mode": "ha_state"},
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
                    "control_type": "position_command",
                    "state_authority": "submitted_only",
                    "verification": {"mode": "command_ack_only"},
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


NO_POSITION_STATE_FIELDS = {
    "position_attribute": None,
    "expected_position_min": None,
    "expected_position_max": None,
    "actual_position": None,
    "position_status": None,
}


def config_with_faults(config, rules, *, enabled: bool = True):
    raw = config.model_dump(mode="json")
    raw["faults"] = {
        "enabled": enabled,
        "rules": rules,
    }
    return BridgeConfig.model_validate(raw)


def read_logs(log_path):
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_example_config_loads_with_standard_appliance_actions_and_demo_climate_candidates():
    config_path = Path(__file__).resolve().parents[1] / "config" / "home-control.example.yaml"

    loaded = load_config(config_path)

    standard_actions = {
        "light_on",
        "light_off",
        "fan_on",
        "fan_off",
        "aircon_on",
        "aircon_off",
        "door_open",
        "door_close",
        "door_stop",
        "vacuum_start",
        "vacuum_return",
        "vacuum_pause",
    }
    assert standard_actions.issubset(loaded.actions)

    assert loaded.actions["light_on"].control_type == "stateless_toggle"
    assert loaded.actions["light_on"].state_authority == "open_loop"
    assert loaded.actions["light_on"].verification is not None
    assert loaded.actions["light_on"].verification.mode == "external_observation"
    assert loaded.actions["light_on"].expected_effect is None
    assert loaded.actions["light_on"].live_test_candidate is True
    assert loaded.actions["light_on"].restore_required is False
    assert loaded.actions["light_off"].control_type == "stateless_toggle"
    assert loaded.actions["light_off"].expected_effect is None
    assert loaded.actions["light_off"].live_test_candidate is True
    assert loaded.actions["light_off"].restore_required is False

    for action_id in ("fan_on", "fan_off", "aircon_on", "aircon_off"):
        action = loaded.actions[action_id]
        assert action.control_type == "stateless_command"
        assert action.state_authority == "submitted_only"
        assert action.verification is not None
        assert action.verification.mode == "command_ack_only"
        assert action.expected_effect is None

    for action_id in ("light_on", "light_off", "fan_on", "fan_off"):
        payload = action_preview_payload(action_id, loaded.actions[action_id])
        assert payload["live_test_readiness"] == "test_now"
        assert payload["restore_required"] is False
        assert payload["live_test_blockers"] == []

    for action_id in ("door_open", "door_close"):
        action = loaded.actions[action_id]
        assert action.control_type == "position_command"
        assert action.state_authority == "ha_entity"
        assert action.verification is not None
        assert action.verification.mode == "ha_state"
        assert action.verification.position is not None
        assert action.expected_effect is not None
        assert action.expected_effect.domain == "cover"

        payload = action_preview_payload(action_id, action)
        assert payload["proof_ceiling"] == "ha_visible_cover_position_checkstate_layer"
        assert payload["live_test_candidate"] is True
        assert payload["live_test_readiness"] == "test_now"
        assert payload["live_test_blockers"] == []

    assert loaded.actions["door_open"].restore_action_id == "door_close"
    assert loaded.actions["door_close"].terminal_action is True

    door_stop = loaded.actions["door_stop"]
    assert door_stop.control_type == "position_command"
    assert door_stop.state_authority == "submitted_only"
    assert door_stop.verification is not None
    assert door_stop.verification.mode == "command_ack_only"
    assert door_stop.expected_effect is None

    for action_id in ("vacuum_start", "vacuum_pause"):
        action = loaded.actions[action_id]
        assert action.control_type == "job_command"
        assert action.state_authority == "ha_entity"
        assert action.verification is not None
        assert action.verification.mode == "ha_state"
        assert action.expected_effect is not None
        assert action.expected_effect.domain == "vacuum"
        assert action.restore_action_id == "vacuum_return"

        payload = action_preview_payload(action_id, action)
        assert payload["proof_ceiling"] == "ha_visible_vacuum_state_checkstate_layer"
        assert payload["live_test_candidate"] is True
        assert payload["live_test_readiness"] == "test_now"
        assert payload["live_test_blockers"] == []

    assert loaded.actions["vacuum_return"].control_type == "job_command"
    assert loaded.actions["vacuum_return"].state_authority == "ha_entity"
    assert loaded.actions["vacuum_return"].live_test_candidate is True
    assert loaded.actions["vacuum_return"].terminal_action is True
    assert loaded.actions["vacuum_return"].verification is not None
    assert loaded.actions["vacuum_return"].verification.mode == "ha_state"
    assert loaded.actions["vacuum_return"].expected_effect is not None
    assert loaded.actions["vacuum_return"].expected_effect.expected_state == "docked"
    vacuum_return_payload = action_preview_payload("vacuum_return", loaded.actions["vacuum_return"])
    assert vacuum_return_payload["live_test_readiness"] == "test_now"
    assert vacuum_return_payload["live_test_blockers"] == []

    assert loaded.actions["aircon_cool"].control_type == "mode_command"
    assert loaded.actions["aircon_cool"].live_test_candidate is True
    assert loaded.actions["aircon_cool"].restore_action_id == "aircon_hvac_off"
    assert loaded.actions["aircon_cool"].verification is not None
    assert loaded.actions["aircon_cool"].verification.mode == "ha_state"
    assert loaded.actions["aircon_cool"].expected_effect is not None
    assert loaded.actions["aircon_cool"].expected_effect.domain == "climate"
    assert loaded.actions["aircon_cool"].expected_effect.service == "set_hvac_mode"
    assert action_preview_payload("aircon_cool", loaded.actions["aircon_cool"])["live_test_readiness"] == "test_now"
    assert loaded.actions["aircon_hvac_off"].live_test_candidate is True
    assert loaded.actions["aircon_hvac_off"].terminal_action is True
    assert loaded.actions["aircon_hvac_off"].expected_effect is not None
    assert loaded.actions["aircon_hvac_off"].expected_effect.expected_state == "off"

    projection_payload = action_preview_payload("projection_mode", loaded.actions["projection_mode"])
    assert projection_payload["proof_ceiling"] == "not_home_control_appliance_coverage_row"
    assert projection_payload["live_test_candidate"] is False
    assert projection_payload["live_test_readiness"] == "not_live_test_candidate"


def test_example_config_live_readiness_classes_are_source_reproducible():
    config_path = Path(__file__).resolve().parents[1] / "config" / "home-control.example.yaml"

    loaded = load_config(config_path)

    door_open_payload = action_preview_payload("door_open", loaded.actions["door_open"])
    assert door_open_payload["proof_ceiling"] == "ha_visible_cover_position_checkstate_layer"
    assert door_open_payload["live_test_readiness"] == "test_now"
    assert door_open_payload["restore_action_id"] == "door_close"

    vacuum_start_payload = action_preview_payload("vacuum_start", loaded.actions["vacuum_start"])
    assert vacuum_start_payload["proof_ceiling"] == "ha_visible_vacuum_state_checkstate_layer"
    assert vacuum_start_payload["live_test_readiness"] == "test_now"
    assert vacuum_start_payload["restore_action_id"] == "vacuum_return"

    vacuum_return_payload = action_preview_payload("vacuum_return", loaded.actions["vacuum_return"])
    assert vacuum_return_payload["proof_ceiling"] == "ha_visible_vacuum_return_checkstate_layer"
    assert vacuum_return_payload["live_test_readiness"] == "test_now"

    projection_payload = action_preview_payload("projection_mode", loaded.actions["projection_mode"])
    assert projection_payload["proof_ceiling"] == "not_home_control_appliance_coverage_row"
    assert projection_payload["live_test_readiness"] == "not_live_test_candidate"


def test_health_is_available_without_bridge_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["actions_count"] == 2
    assert response.json()["config_profile"] == "demo"
    assert response.json()["demo_mappings_present"] is True
    assert response.json()["light_demo_mappings_present"] is True
    assert response.json()["fault_mode"] is False
    assert response.json()["fault_rules_count"] == 0


def test_health_exposes_redacted_config_profile_without_paths(config, token, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_CONFIG", str(tmp_path / "local" / "env" / "home-control.live.yaml"))
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body)
    assert body["config_profile"] == "local"
    assert body["light_demo_mappings_present"] is True
    assert "home-control.live.yaml" not in serialized
    assert "light.demo_room" not in serialized
    assert "HOME_ASSISTANT_TOKEN" not in serialized


def test_operator_console_is_local_ui_without_embedded_secrets(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/operator")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Home Control Operator" in body
    assert "/actions" in body
    assert "/preview" in body
    assert "/execute" in body
    assert "Bridge API token" in body
    assert 'id="route-actions"' in body
    for action_id in (
        "aircon_cool",
        "aircon_hvac_off",
        "light_on",
        "light_off",
        "fan_on",
        "fan_off",
        "door_open",
        "door_close",
        "vacuum_return",
    ):
        assert action_id in body
    assert "reviewed_light_command_stimulus_candidate" in body
    assert "reviewed_vacuum_return_restore_candidate" in body
    assert "command_stimulus_without_restore_required" in body
    assert "position_command_open_then_close" in body
    assert "terminal_return_command_candidate" in body
    assert "operator_shortcut_submission_summary_only" in body
    assert token not in body
    assert "HOME_CONTROL_API_TOKEN" not in body
    assert "local-test-token" not in body


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
    by_id = {action["action_id"]: action for action in actions}
    assert by_id["light_on"]["control_type"] == "stateful_target"
    assert by_id["light_on"]["state_authority"] == "ha_entity"
    assert by_id["light_on"]["verification_mode"] == "ha_state"
    assert by_id["light_on"]["state_tracking"] == "tracked"
    assert by_id["light_on"]["proof_ceiling"] == "ha_visible_state_checkstate_layer"
    assert by_id["light_on"]["live_test_candidate"] is False
    assert by_id["light_on"]["live_test_readiness"] == "not_live_test_candidate"
    assert by_id["light_on"]["live_test_blockers"] == ["not_marked_live_test_candidate"]
    assert by_id["curtain_close"]["control_type"] == "position_command"
    assert by_id["curtain_close"]["state_authority"] == "submitted_only"
    assert by_id["curtain_close"]["verification_mode"] == "command_ack_only"
    assert by_id["curtain_close"]["state_tracking"] == "ack_only"
    assert by_id["curtain_close"]["proof_ceiling"] == "command_ack_only"
    assert by_id["curtain_close"]["live_test_readiness"] == "not_live_test_candidate"
    assert by_id["curtain_close"]["live_test_blockers"] == [
        "not_marked_live_test_candidate",
        "missing_ha_visible_success_criterion",
    ]


def test_action_state_requires_api_token(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions/light_on/state")

    assert response.status_code == 401


def test_action_state_returns_redacted_match(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "action_id": "light_on",
        "status": "matched",
        "control_type": "stateful_target",
        "state_authority": "ha_entity",
        "verification_mode": "ha_state",
        "state_tracking": "tracked",
        "expected_state": "on",
        "expected_states": ["on"],
        "actual_state": "on",
        **NO_POSITION_STATE_FIELDS,
    }
    assert "entity_id" not in body
    assert ha.state_calls == ["light.demo_room"]


def test_action_state_reports_mismatch_without_entity(config, token, tmp_path):
    ha = FakeHomeAssistant(states={"light.demo_room": "off"})
    client, _, _, _ = make_client(config, token, tmp_path, ha=ha)

    response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "mismatch"
    assert body["expected_state"] == "on"
    assert body["expected_states"] == ["on"]
    assert body["actual_state"] == "off"
    assert "entity_id" not in body


def test_action_state_matches_accepted_states(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["verification"] = {
        "mode": "ha_state",
        "accepted_states": ["opening", "on"],
        "settle_seconds": 2,
        "timeout_seconds": 30,
    }
    accepted_config = BridgeConfig.model_validate(raw)
    ha = FakeHomeAssistant(states={"light.demo_room": "opening"})
    client, _, _, _ = make_client(accepted_config, token, tmp_path, ha=ha)

    response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "matched"
    assert body["expected_state"] == "on"
    assert body["expected_states"] == ["on", "opening"]
    assert body["actual_state"] == "opening"

    actions_response = client.get("/actions", headers=auth_headers(token))
    action = next(action for action in actions_response.json() if action["action_id"] == "light_on")
    assert action["expected_states"] == ["on", "opening"]
    assert action["settle_seconds"] == 2
    assert action["timeout_seconds"] == 30


def test_cover_action_state_requires_position_threshold(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["state_authority"] = "ha_entity"
    raw["actions"]["curtain_close"]["verification"] = {
        "mode": "ha_state",
        "accepted_states": ["closed"],
        "settle_seconds": 8,
        "timeout_seconds": 60,
        "position": {"attribute": "current_position", "max": 5},
    }
    raw["actions"]["curtain_close"]["expected_effect"] = {
        "domain": "cover",
        "service": "close_cover",
        "entity_id": "cover.demo_curtain",
        "expected_state": "closed",
    }
    cover_config = BridgeConfig.model_validate(raw)
    ha = FakeHomeAssistant(
        states={"cover.demo_curtain": {"state": "closed", "attributes": {"current_position": "2"}}}
    )
    client, _, _, _ = make_client(cover_config, token, tmp_path, ha=ha)

    actions_response = client.get("/actions", headers=auth_headers(token))
    action = next(action for action in actions_response.json() if action["action_id"] == "curtain_close")
    assert action["state_tracking"] == "tracked"
    assert action["position_proof"] == {"attribute": "current_position", "min": None, "max": 5.0}

    response = client.get("/actions/curtain_close/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "matched"
    assert body["expected_state"] == "closed"
    assert body["expected_states"] == ["closed"]
    assert body["actual_state"] == "closed"
    assert body["position_attribute"] == "current_position"
    assert body["expected_position_min"] is None
    assert body["expected_position_max"] == 5.0
    assert body["actual_position"] == 2.0
    assert body["position_status"] == "matched"
    assert "entity_id" not in body


def test_cover_action_state_rejects_state_match_when_position_is_outside_threshold(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["state_authority"] = "ha_entity"
    raw["actions"]["curtain_close"]["verification"] = {
        "mode": "ha_state",
        "accepted_states": ["closed"],
        "position": {"attribute": "current_position", "max": 5},
    }
    raw["actions"]["curtain_close"]["expected_effect"] = {
        "domain": "cover",
        "service": "close_cover",
        "entity_id": "cover.demo_curtain",
        "expected_state": "closed",
    }
    cover_config = BridgeConfig.model_validate(raw)
    ha = FakeHomeAssistant(states={"cover.demo_curtain": {"state": "closed", "attributes": {"current_position": 42}}})
    client, _, _, _ = make_client(cover_config, token, tmp_path, ha=ha)

    response = client.get("/actions/curtain_close/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "mismatch"
    assert body["actual_state"] == "closed"
    assert body["actual_position"] == 42.0
    assert body["position_status"] == "mismatch"


def test_cover_action_state_reports_missing_position_attribute_as_unavailable(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["state_authority"] = "ha_entity"
    raw["actions"]["curtain_close"]["verification"] = {
        "mode": "ha_state",
        "accepted_states": ["closed"],
        "position": {"attribute": "current_position", "max": 5},
    }
    raw["actions"]["curtain_close"]["expected_effect"] = {
        "domain": "cover",
        "service": "close_cover",
        "entity_id": "cover.demo_curtain",
        "expected_state": "closed",
    }
    cover_config = BridgeConfig.model_validate(raw)
    ha = FakeHomeAssistant(states={"cover.demo_curtain": {"state": "closed", "attributes": {}}})
    client, _, _, _ = make_client(cover_config, token, tmp_path, ha=ha)

    response = client.get("/actions/curtain_close/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "position_unavailable"
    assert body["actual_state"] == "closed"
    assert body["actual_position"] is None
    assert body["position_status"] == "unavailable"


def test_action_state_ack_only_action_does_not_call_home_assistant(config, token, tmp_path):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.get("/actions/curtain_close/state", headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "action_id": "curtain_close",
        "status": "ack_only",
        "control_type": "position_command",
        "state_authority": "submitted_only",
        "verification_mode": "command_ack_only",
        "state_tracking": "ack_only",
        "expected_state": None,
        "expected_states": [],
        "actual_state": None,
        **NO_POSITION_STATE_FIELDS,
    }
    assert ha.state_calls == []


def test_external_observation_action_ignores_expected_effect(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["control_type"] = "stateless_toggle"
    raw["actions"]["light_on"]["state_authority"] = "open_loop"
    raw["actions"]["light_on"]["verification"] = {"mode": "external_observation"}
    external_config = BridgeConfig.model_validate(raw)
    client, ha, _, _ = make_client(external_config, token, tmp_path)

    actions_response = client.get("/actions", headers=auth_headers(token))
    action = next(action for action in actions_response.json() if action["action_id"] == "light_on")
    assert action["control_type"] == "stateless_toggle"
    assert action["state_authority"] == "open_loop"
    assert action["verification_mode"] == "external_observation"
    assert action["state_tracking"] == "external_required"
    assert action["expected_effect"] is None

    state_response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert state_response.status_code == 200
    assert state_response.json() == {
        "ok": False,
        "action_id": "light_on",
        "status": "external_required",
        "control_type": "stateless_toggle",
        "state_authority": "open_loop",
        "verification_mode": "external_observation",
        "state_tracking": "external_required",
        "expected_state": None,
        "expected_states": [],
        "actual_state": None,
        **NO_POSITION_STATE_FIELDS,
    }
    assert ha.state_calls == []


def test_command_ack_only_action_ignores_expected_effect(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["control_type"] = "stateless_command"
    raw["actions"]["curtain_close"]["state_authority"] = "submitted_only"
    raw["actions"]["curtain_close"]["verification"] = {"mode": "command_ack_only"}
    raw["actions"]["curtain_close"]["expected_effect"] = {
        "domain": "switch",
        "service": "turn_off",
        "entity_id": "switch.demo_unknown_aircon_wrapper",
        "expected_state": "off",
    }
    ack_only_config = BridgeConfig.model_validate(raw)
    client, ha, _, _ = make_client(ack_only_config, token, tmp_path)

    actions_response = client.get("/actions", headers=auth_headers(token))
    action = next(action for action in actions_response.json() if action["action_id"] == "curtain_close")
    assert action["control_type"] == "stateless_command"
    assert action["state_authority"] == "submitted_only"
    assert action["verification_mode"] == "command_ack_only"
    assert action["state_tracking"] == "ack_only"
    assert action["expected_effect"] is None

    state_response = client.get("/actions/curtain_close/state", headers=auth_headers(token))

    assert state_response.status_code == 200
    assert state_response.json() == {
        "ok": False,
        "action_id": "curtain_close",
        "status": "ack_only",
        "control_type": "stateless_command",
        "state_authority": "submitted_only",
        "verification_mode": "command_ack_only",
        "state_tracking": "ack_only",
        "expected_state": None,
        "expected_states": [],
        "actual_state": None,
        **NO_POSITION_STATE_FIELDS,
    }
    assert ha.state_calls == []


def test_climate_mode_action_tracks_hvac_state(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["aircon_cool"] = {
        "label": "エアコンを冷房にする",
        "ha_script": "script.demo_aircon_cool",
        "confirm_required": True,
        "response_text": "エアコンを冷房にしました。",
        "control_type": "mode_command",
        "state_authority": "ha_entity",
        "verification": {
            "mode": "ha_state",
            "accepted_states": ["cool"],
            "settle_seconds": 5,
            "timeout_seconds": 60,
        },
        "expected_effect": {
            "domain": "climate",
            "service": "set_hvac_mode",
            "entity_id": "climate.demo_aircon",
            "expected_state": "cool",
        },
    }
    climate_config = BridgeConfig.model_validate(raw)
    ha = FakeHomeAssistant(states={"climate.demo_aircon": "cool"})
    client, _, _, _ = make_client(climate_config, token, tmp_path, ha=ha)

    actions_response = client.get("/actions", headers=auth_headers(token))
    action = next(action for action in actions_response.json() if action["action_id"] == "aircon_cool")
    assert action["control_type"] == "mode_command"
    assert action["state_authority"] == "ha_entity"
    assert action["verification_mode"] == "ha_state"
    assert action["state_tracking"] == "tracked"
    assert action["expected_states"] == ["cool"]
    assert action["settle_seconds"] == 5
    assert action["timeout_seconds"] == 60
    assert action["expected_effect"] == {
        "domain": "climate",
        "service": "set_hvac_mode",
        "entity_id": "climate.demo_aircon",
        "expected_state": "cool",
    }

    state_response = client.get("/actions/aircon_cool/state", headers=auth_headers(token))

    assert state_response.status_code == 200
    assert state_response.json() == {
        "ok": True,
        "action_id": "aircon_cool",
        "status": "matched",
        "control_type": "mode_command",
        "state_authority": "ha_entity",
        "verification_mode": "ha_state",
        "state_tracking": "tracked",
        "expected_state": "cool",
        "expected_states": ["cool"],
        "actual_state": "cool",
        **NO_POSITION_STATE_FIELDS,
    }
    assert ha.state_calls == ["climate.demo_aircon"]


def test_action_state_unavailable_is_redacted(config, token, tmp_path):
    client, _, _, _ = make_client(config, token, tmp_path, ha=FakeHomeAssistant(fail_state=True))

    response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "unavailable"
    assert body["expected_state"] == "on"
    assert body["expected_states"] == ["on"]
    assert body["actual_state"] is None
    assert "entity_id" not in body
    assert "state unavailable" not in json.dumps(body)


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
    assert body["control_type"] == "stateful_target"
    assert body["state_authority"] == "ha_entity"
    assert body["verification_mode"] == "ha_state"
    assert body["state_tracking"] == "tracked"
    assert body["proof_ceiling"] == "ha_visible_state_checkstate_layer"
    assert body["live_test_candidate"] is False
    assert body["live_test_readiness"] == "not_live_test_candidate"
    assert body["live_test_blockers"] == ["not_marked_live_test_candidate"]
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
    assert log["control_type"] == "stateful_target"
    assert log["state_authority"] == "ha_entity"
    assert log["verification_mode"] == "ha_state"
    assert log["state_tracking"] == "tracked"
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


def test_repeated_dry_run_request_id_is_classified_without_execution(config, token, tmp_path):
    client, ha, log_path, _ = make_client(config, token, tmp_path)
    payload = {"source": "dify", "request_id": "req-dry-dup", "dry_run": True}

    first = client.post("/actions/light_on/execute", headers=auth_headers(token), json=payload)
    second = client.post("/actions/light_on/execute", headers=auth_headers(token), json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "dry_run"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["executed"] is False
    assert second.json()["execution_id"] is None
    assert ha.calls == []
    logs = read_logs(log_path)
    assert [log["event"] for log in logs] == ["execute_dry_run", "execute_dry_run_duplicate"]


def test_conflicting_dry_run_request_id_is_rejected_without_execution(config, token, tmp_path):
    client, ha, log_path, _ = make_client(config, token, tmp_path)

    first = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-dry-conflict", "dry_run": True},
    )
    second = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={
            "source": "different-client",
            "request_id": "req-dry-conflict",
            "dry_run": True,
            "user_text": "照明をつけて",
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "dry_run"
    assert second.status_code == 200
    assert second.json()["ok"] is False
    assert second.json()["status"] == "failed"
    assert second.json()["error"] == "dry_run_request_conflict"
    assert ha.calls == []
    logs = read_logs(log_path)
    assert logs[-1]["event"] == "execute_dry_run_conflict"
    assert logs[-1]["error"] == "dry_run_request_conflict"
    assert "照明をつけて" not in json.dumps(logs[-1], ensure_ascii=False)


def test_dry_run_request_id_cannot_be_reused_for_real_execution(config, token, tmp_path):
    client, ha, log_path, _ = make_client(config, token, tmp_path)

    first = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-dry-then-real", "dry_run": True},
    )
    second = client.post(
        "/actions/light_on/execute",
        headers=auth_headers(token),
        json={"source": "dify", "request_id": "req-dry-then-real"},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "dry_run"
    assert second.status_code == 200
    assert second.json()["status"] == "failed"
    assert second.json()["error"] == "dry_run_request_conflict"
    assert ha.calls == []
    assert [log["event"] for log in read_logs(log_path)] == [
        "execute_dry_run",
        "execute_dry_run_conflict",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "dify", "dry_run": True},
        {"source": "dify", "request_id": "", "dry_run": True},
        {"source": "dify", "request_id": "req-dry-\n\t-雪", "dry_run": True},
    ],
)
def test_dry_run_missing_empty_and_unusual_request_ids_do_not_execute(config, token, tmp_path, payload):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post("/actions/light_on/execute", headers=auth_headers(token), json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "dry_run"
    assert response.json()["executed"] is False
    assert ha.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "dify", "request_id": "x" * 161, "dry_run": True},
        {"source": "dify", "request_id": "req-dry-bad", "dry_run": {"nested": True}},
        {"source": "dify", "request_id": "req-dry-extra", "dry_run": True, "ha_script": "script.any"},
    ],
)
def test_malformed_dry_run_bodies_are_rejected_before_execution(config, token, tmp_path, payload):
    client, ha, _, _ = make_client(config, token, tmp_path)

    response = client.post("/actions/light_on/execute", headers=auth_headers(token), json=payload)

    assert response.status_code == 422
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
@pytest.mark.parametrize("attempt_suffix", ["attempt", "hca"])
def test_fault_transient_scenarios_track_attempts_by_normalized_request_id(
    config,
    token,
    fault_mode,
    tmp_path,
    scenario,
    expected_statuses,
    attempt_suffix,
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
            json={"source": "dify", "request_id": f"workflow-1-{attempt_suffix}-{index}"},
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


def test_config_rejects_position_proof_without_threshold(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["verification"]["position"] = {"attribute": "current_position"}

    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(raw)


def test_config_rejects_position_proof_without_ha_state_mode(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["verification"] = {
        "mode": "command_ack_only",
        "position": {"attribute": "current_position", "max": 5},
    }

    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(raw)


def test_config_rejects_unknown_restore_or_stop_action_refs(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["restore_action_id"] = "missing_action"

    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(raw)


def test_live_readiness_blocks_candidate_without_restore_or_stop(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["live_test_candidate"] = True

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("light_on", loaded.actions["light_on"])

    assert payload["live_test_readiness"] == "do_not_test_current_config"
    assert payload["live_test_blockers"] == ["missing_restore_or_stop"]


def test_restore_not_required_allows_command_stimulus_candidate(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["live_test_candidate"] = True
    raw["actions"]["curtain_close"]["restore_required"] = False

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("curtain_close", loaded.actions["curtain_close"])

    assert payload["live_test_readiness"] == "test_now"
    assert payload["restore_required"] is False
    assert payload["state_tracking"] == "ack_only"
    assert payload["live_test_blockers"] == []


def test_terminal_action_with_ha_state_can_be_live_test_candidate(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"]["live_test_candidate"] = True
    raw["actions"]["light_on"]["terminal_action"] = True

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("light_on", loaded.actions["light_on"])

    assert payload["live_test_readiness"] == "test_now"
    assert payload["live_test_blockers"] == []


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


def test_source_no_live_fuzz_classifies_open_loop_toggles_as_external_required():
    config_path = Path(__file__).resolve().parents[1] / "config" / "home-control.example.yaml"
    loaded = load_config(config_path)

    for action_id in ("light_on", "light_off"):
        action = loaded.actions[action_id]
        payload = action_preview_payload(action_id, action)

        assert payload["control_type"] == "stateless_toggle"
        assert payload["state_authority"] == "open_loop"
        assert payload["verification_mode"] == "external_observation"
        assert payload["state_tracking"] == "external_required"
        assert payload["expected_states"] == []
        assert "expected_effect" not in payload


def test_source_no_live_fuzz_ha_state_without_expected_effect_is_unsupported(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"].pop("expected_effect")
    raw["actions"]["light_on"]["verification"] = {"mode": "ha_state"}

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("light_on", loaded.actions["light_on"])

    assert payload["verification_mode"] == "ha_state"
    assert payload["state_tracking"] == "unsupported"
    assert payload["expected_states"] == []
    assert "expected_effect" not in payload


def test_source_no_live_fuzz_expected_effect_without_explicit_metadata_is_not_tracked(config, token, tmp_path):
    raw = config.model_dump(mode="json")
    raw["actions"]["light_on"].pop("control_type")
    raw["actions"]["light_on"].pop("state_authority")
    raw["actions"]["light_on"].pop("verification")

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("light_on", loaded.actions["light_on"])

    assert payload["control_type"] == "script_wrapper"
    assert payload["state_authority"] == "submitted_only"
    assert payload["verification_mode"] == "command_ack_only"
    assert payload["state_tracking"] == "ack_only"
    assert payload["expected_states"] == []
    assert "expected_effect" not in payload

    client, ha, _, _ = make_client(loaded, token, tmp_path)
    state_response = client.get("/actions/light_on/state", headers=auth_headers(token))

    assert state_response.status_code == 200
    assert state_response.json()["state_tracking"] == "ack_only"
    assert ha.state_calls == []


def test_source_no_live_fuzz_ack_only_with_expected_effect_is_not_tracked(config):
    raw = config.model_dump(mode="json")
    raw["actions"]["curtain_close"]["verification"] = {"mode": "command_ack_only"}
    raw["actions"]["curtain_close"]["expected_effect"] = {
        "domain": "cover",
        "service": "close_cover",
        "entity_id": "cover.demo_curtain",
        "expected_state": "closed",
    }

    loaded = BridgeConfig.model_validate(raw)
    payload = action_preview_payload("curtain_close", loaded.actions["curtain_close"])

    assert payload["verification_mode"] == "command_ack_only"
    assert payload["state_tracking"] == "ack_only"
    assert payload["expected_states"] == []
    assert "expected_effect" not in payload


@pytest.mark.parametrize(
    "ha_script",
    [
        "light.demo_room",
        "scene.movie_mode",
        "climate.demo_aircon",
        "script.BadName",
        "script.demo-action",
    ],
)
def test_source_no_live_fuzz_rejects_unsafe_or_ambiguous_script_refs(ha_script):
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(
            {
                "home_assistant": {"base_url": "http://homeassistant.local:8123"},
                "actions": {
                    "ambiguous_action": {
                        "label": "Ambiguous",
                        "ha_script": ha_script,
                        "response_text": "Rejected before live operation.",
                    }
                },
            }
        )
