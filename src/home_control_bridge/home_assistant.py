from __future__ import annotations

from typing import Any

import httpx

from .config import HomeAssistantConfig


class HomeAssistantError(RuntimeError):
    def __init__(self, safe_message: str, *, log_detail: str | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.log_detail = log_detail or safe_message


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

    async def turn_on_script(self, script_entity_id: str) -> dict[str, Any]:
        url = f"{self.config.base_url}/api/services/script/turn_on"
        payload = {"entity_id": script_entity_id}
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise HomeAssistantError(
                "Home Assistant request failed.",
                log_detail=f"Home Assistant request failed: {exc.__class__.__name__}",
            ) from exc

        if not response.is_success:
            raise HomeAssistantError(
                "Home Assistant returned an error.",
                log_detail=f"Home Assistant returned HTTP {response.status_code}.",
            )

        try:
            body: Any = response.json()
        except ValueError:
            body = None

        return {
            "status_code": response.status_code,
            "body": body,
        }
