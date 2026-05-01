from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlAuditLogger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def write(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe_event = {
            "timestamp": datetime.now(UTC).isoformat(),
            **_sanitize(event),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe_event, ensure_ascii=False, sort_keys=True) + "\n")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if "token" in lowered or "authorization" in lowered or "password" in lowered or "secret" in lowered:
                continue
            if lowered == "user_text":
                sanitized["user_text_present"] = item is not None
                if isinstance(item, str):
                    sanitized["user_text_length"] = len(item)
                continue
            sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return value[:1000]
    return value
