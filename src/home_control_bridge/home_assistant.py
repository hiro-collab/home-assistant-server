from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import HomeAssistantConfig


class HomeAssistantError(RuntimeError):
    def __init__(
        self,
        safe_message: str,
        *,
        log_detail: str | None = None,
        submission_outcome: str = "failed_before_submit",
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.log_detail = log_detail or safe_message
        self.submission_outcome = submission_outcome


@dataclass(frozen=True)
class HomeAssistantEntityState:
    state: str
    attributes: dict[str, Any]


class HomeAssistantClient:
    def __init__(self, config: HomeAssistantConfig, token: str) -> None:
        self.config = config
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def check_connection(self) -> dict[str, Any]:
        url = f"{self.config.base_url}/api/"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.get(url, headers=self._headers())
            return {
                "ok": response.is_success,
                "status_code": response.status_code,
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "error": exc.__class__.__name__,
            }

    async def turn_on_script(
        self,
        script_entity_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}/api/services/script/turn_on"
        payload = {"entity_id": script_entity_id}
        timeout = self.config.timeout_seconds
        if timeout_seconds is not None:
            timeout = min(timeout, timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            before_submit = isinstance(
                exc,
                (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
            )
            raise HomeAssistantError(
                "Home Assistant request failed.",
                log_detail=f"Home Assistant request failed: {exc.__class__.__name__}",
                submission_outcome=(
                    "failed_before_submit"
                    if before_submit
                    else "submission_outcome_unknown"
                ),
            ) from exc

        if not response.is_success:
            definitive_rejection_statuses = {
                400,
                401,
                403,
                404,
                405,
                413,
                415,
                422,
            }
            outcome = (
                "failed_before_submit"
                if response.status_code in definitive_rejection_statuses
                else "submission_outcome_unknown"
            )
            raise HomeAssistantError(
                "Home Assistant returned an error.",
                log_detail=f"Home Assistant returned HTTP {response.status_code}.",
                submission_outcome=outcome,
            )

        try:
            body: Any = response.json()
        except ValueError:
            body = None

        return {
            "status_code": response.status_code,
            "body": body,
        }

    async def get_entity_state(self, entity_id: str) -> str:
        return (await self.get_entity_state_snapshot(entity_id)).state

    async def get_entity_state_snapshot(self, entity_id: str) -> HomeAssistantEntityState:
        encoded_entity_id = quote(entity_id, safe="")
        url = f"{self.config.base_url}/api/states/{encoded_entity_id}"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise HomeAssistantError(
                "Home Assistant state request failed.",
                log_detail=f"Home Assistant state request failed: {exc.__class__.__name__}",
            ) from exc

        if not response.is_success:
            raise HomeAssistantError(
                "Home Assistant returned an error.",
                log_detail=f"Home Assistant state returned HTTP {response.status_code}.",
            )

        try:
            body: Any = response.json()
        except ValueError as exc:
            raise HomeAssistantError(
                "Home Assistant state response was not JSON.",
                log_detail="Home Assistant state response was not JSON.",
            ) from exc

        state = body.get("state") if isinstance(body, dict) else None
        if not isinstance(state, str) or not state:
            raise HomeAssistantError(
                "Home Assistant state response did not include a state.",
                log_detail="Home Assistant state response did not include a state.",
            )

        attributes = body.get("attributes") if isinstance(body, dict) else None
        if not isinstance(attributes, dict):
            attributes = {}
        return HomeAssistantEntityState(state=state, attributes=attributes)
