# Repository Guide

This repository contains the Home Control Safety Bridge: a local HTTP bridge that exposes only allowlisted Home Assistant scripts to agent-side clients.

## Structure

- `home_control_bridge/`: FastAPI app, action registry, Home Assistant client, event notification, fault injection, and logging.
- `config/`: example allowlist and runtime config shape.
- `docs/`: API usage, integration contract, responsibility boundary, event notification, and fault-injection notes.
- `tests/`: unit and integration checks for action safety and bridge behavior.

Runtime output belongs under `.cache/` or local environment files. Do not treat cache, tokens, or local Home Assistant state as source.

## Commands

```powershell
uv sync --extra dev
uv run pytest
uv run home-control-bridge
```

Direct server start:

```powershell
uv run uvicorn home_control_bridge.main:app --host 127.0.0.1 --port 8787
```

## Documentation

Keep README short. Detailed behavior is split by role:

- `README.md`: setup, endpoints, and safety summary.
- `docs/module-responsibilities.md`: what this module owns and does not own.
- `docs/integration-contract.md`: client-facing API contract and execution IDs.
- `docs/api-usage.md`: concrete request examples and Dify wiring notes.
- `docs/event-notifications.md`: optional UDP event surface.
- `docs/fault-injection.md`: local-only fault testing.
- `docs/security-notes.md`: token, allowlist, logging, and local-only safety notes.
- `docs/retired-paths.md`: archived or deferred paths.

Files under `docs/archive/` are historical records. Do not cite them as active requirements or connection contracts unless a human explicitly asks for history.

## Change Rules

- Do not accept arbitrary Home Assistant service names, entity names, or URLs from clients.
- Keep the bridge operation surface limited to configured `action_id` values.
- Keep Home Assistant tokens and API tokens out of logs.
- Preserve `execution_id` as the correlation unit for commands actually issued to Home Assistant.
