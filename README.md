# Home Control Safety Bridge

Home Assistant の script allowlist だけを外部クライアントへ公開するローカル HTTP ブリッジです。Dify / AITuberKit / sword-voice-agent などの音声・エージェント層から、家電操作を安全に中継するための単体モジュールとして使います。

## 起動

```powershell
uv sync --extra dev
Copy-Item config/home-control.example.yaml config/home-control.yaml
Copy-Item .env.example .env
```

`config/home-control.yaml` の `home_assistant.base_url` と `actions` を環境に合わせて編集し、実行環境で必要な token を渡します。

```powershell
$env:HOME_CONTROL_CONFIG = "config/home-control.yaml"
$env:HOME_CONTROL_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:HOME_ASSISTANT_TOKEN = "Home Assistant の Long-lived access token"
uv run home-control-bridge
```

直接 uvicorn で起動する場合:

```powershell
uv run uvicorn home_control_bridge.main:app --host 127.0.0.1 --port 8787
```

## 文書

- 責務境界: [docs/module-responsibilities.md](docs/module-responsibilities.md)
- 接続契約: [docs/integration-contract.md](docs/integration-contract.md)
- Dify からの呼び出し: [docs/api-usage.md](docs/api-usage.md)
- UDP 演出通知: [docs/event-notifications.md](docs/event-notifications.md)
- 疑似障害テスト: [docs/fault-injection.md](docs/fault-injection.md)
- セキュリティ注意: [docs/security-notes.md](docs/security-notes.md)
- OpenAPI: [docs/dify-openapi.yaml](docs/dify-openapi.yaml) または起動中の `/openapi.json`
- Home Assistant script 例: [docs/home-assistant-scripts.example.yaml](docs/home-assistant-scripts.example.yaml)
