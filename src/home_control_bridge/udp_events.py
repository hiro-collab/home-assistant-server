from __future__ import annotations

import json
import socket
from typing import Literal

from .config import UdpEventsConfig

UdpEventPhase = Literal["start", "done", "error"]


class UdpEventSender:
    def __init__(self, config: UdpEventsConfig) -> None:
        self.config = config

    def emit(
        self,
        *,
        phase: UdpEventPhase,
        action_id: str,
        label: str,
        source: str,
        request_id: str | None,
        message: str | None = None,
        error: str | None = None,
    ) -> dict[str, object] | None:
        if not self.config.enabled:
            return None

        payload: dict[str, object] = {
            "type": self.config.event_type,
            "phase": phase,
            "action_id": action_id,
            "label": label,
            "source": source,
            "request_id": request_id,
        }
        if message is not None:
            payload["message"] = message
        if error is not None:
            payload["error"] = error

        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(data, (self.config.host, self.config.port))
        return payload
