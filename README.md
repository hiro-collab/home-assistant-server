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

Sword Agent OS の標準ディストリビューションから単独で起動する場合は、repo root で
次の helper を使うと、生成済みの organ `.env` を bridge process に渡せます。
secret 値は表示しません。

```powershell
pwsh -NoProfile -File .\scripts\start-home-control-bridge.ps1
```

この organ ディレクトリだけで起動する場合も、`.env` の存在だけでは process 環境に
値は入りません。直接起動では、必ず `$env:` を設定するか、`uv run --env-file .env`
を使ってください。

```powershell
$env:HOME_CONTROL_CONFIG = "config/home-control.yaml"
$env:HOME_CONTROL_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:HOME_ASSISTANT_TOKEN = "Home Assistant の Long-lived access token"
uv run home-control-bridge
```

直接 uvicorn で起動する場合:

```powershell
uv run --env-file .env python -m uvicorn home_control_bridge.main:app --host 127.0.0.1 --port 8787
```

plain `uv run uvicorn ...` は `.env` を自動では読みません。その場合、client 側が
`.env` から token を読めても、server 側が `HOME_ASSISTANT_TOKEN` を受け取れず
`/health` が `config_error`、`/actions` が `503` になることがあります。

## ローカル操作画面

起動中の bridge は `http://127.0.0.1:8787/operator` にローカル操作画面を出します。
画面は `GET /actions`、`/state`、`/preview`、`/execute` を人間が選べる形にした薄い UI です。
API token は画面で入力し、ページ内のメモリにだけ保持します。HTML には token、Home Assistant
URL、entity ID、secret は埋め込みません。

この画面は allowlist 済み action を可視化して、state / preview / dry-run / execute /
confirm execute を選べるようにするものです。実行ボタンは既存の bridge API を呼ぶため、
live 操作には従来どおり明示的な route / review / user authority が必要です。
起動直後の route 用に、`aircon_cool` と `aircon_hvac_off` は固定ショートカットとして
表示されます。これは action id を見えるようにするだけで、token 入力後も既存 API の
認証・allowlist・確認・実行境界をそのまま使います。詳細 metadata が必要な場合は
従来どおり `Load actions` で catalog を取得します。

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
