# Home Control Safety Bridge

Home Assistant の script allowlist だけを外部クライアントへ公開するローカル HTTP ブリッジです。Dify / AITuberKit / sword-voice-agent などの音声・エージェント層から、家電操作を安全に中継するための単体モジュールとして使います。

## 初期セットアップ

```powershell
uv sync --extra dev
if (-not (Test-Path config/home-control.yaml)) {
  Copy-Item config/home-control.example.yaml config/home-control.yaml
}
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
}
```

`config/home-control.yaml` の `home_assistant.base_url` と `actions` を環境に合わせて編集し、実行環境で必要な token を渡します。
`.env` には実 token を入れるため、コミットしません。

## dotenv / local config

`.env.example` を `.env` にコピーし、少なくとも次を設定します。

- `HOME_CONTROL_CONFIG`: 通常は `config/home-control.yaml`。
- `HOME_CONTROL_API_TOKEN`: Dify / AITuberKit / Thought Core / Environment State Server がこの bridge へ送る local token。
- `ENVIRONMENT_API_TOKEN`: Environment State Server 専用 token。空なら `HOME_CONTROL_API_TOKEN` を共有。
- `HOME_ASSISTANT_TOKEN`: Home Assistant の Long-lived access token。

Home Assistant 側では、`config/home-control.yaml` に書いた script / entity が実在し、Windows PC から
`home_assistant.base_url` へ到達できる必要があります。

## 通常起動

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
- 退避導線: [docs/retired-paths.md](docs/retired-paths.md)

`docs/archive/` は履歴確認用です。通常の実装判断では上記の文書を参照します。
