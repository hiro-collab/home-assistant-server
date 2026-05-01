# Home Control Safety Bridge

Dify / AITuberKit / sword-voice-agent から Home Assistant を安全に操作するためのローカルHTTPブリッジです。

このブリッジは、リクエストから Home Assistant の任意サービス名、任意entity、任意URLを受け取りません。設定ファイルで許可した `action_id` だけを受け付け、内部では Home Assistant REST API の設定済みサービスだけを呼びます。複雑な操作、危険な操作、Matter / SwitchBot の差分は Home Assistant の script 側に閉じ込められます。単純な `switch` / `light` / `fan` のON/OFFだけは、Home Assistant scriptを作らずに設定ファイルから直接allowlistできます。

## API

- `GET /health`
- `GET /actions`
- `POST /actions/{action_id}/preview`
- `POST /actions/{action_id}/execute`

`/health` 以外はAPI tokenが必要です。以下のどちらかのヘッダーを使えます。

```http
Authorization: Bearer <HOME_CONTROL_API_TOKEN>
X-API-Token: <HOME_CONTROL_API_TOKEN>
```

## セットアップ

```powershell
uv sync --extra dev
Copy-Item config/home-control.example.yaml config/home-control.yaml
Copy-Item .env.example .env
```

環境変数を設定します。

```powershell
$env:HOME_CONTROL_CONFIG = "config/home-control.yaml"
$env:HOME_CONTROL_API_TOKEN = "change-me-local-bridge-token"
$env:HOME_ASSISTANT_TOKEN = "Home Assistant の Long-lived access token"
```

`config/home-control.yaml` の `home_assistant.base_url` と `actions` を自宅環境に合わせて編集してください。危険な操作、玄関、鍵、セキュリティ、暖房器具などは初期allowlistに入れないでください。

## 起動

```powershell
uv run uvicorn home_control_bridge.main:app --host 127.0.0.1 --port 8787
```

ローカルネットワーク内の別マシンから呼ぶ場合だけ `--host 0.0.0.0` を検討してください。その場合もルーター越しに公開しない構成を推奨します。

## 設定例

```yaml
home_assistant:
  base_url: "http://homeassistant.local:8123"
  token_env: "HOME_ASSISTANT_TOKEN"
server:
  api_token_env: "HOME_CONTROL_API_TOKEN"
  log_path: ".cache/home_control/events.jsonl"
actions:
  light_on:
    label: "照明をつける"
    ha_service: "switch.turn_on"
    entity_id: "switch.demo_light"
    confirm_required: false
    response_text: "照明をつけました。"
  light_off:
    label: "照明を消す"
    ha_service: "switch.turn_off"
    entity_id: "switch.demo_light"
    confirm_required: false
    response_text: "照明を消しました。"
  curtain_close:
    label: "カーテンを閉める"
    ha_script: "script.curtain_close"
    confirm_required: true
    response_text: "カーテンを閉めました。"
```

1つのactionには、以下のどちらか一方だけを設定します。

- `ha_script`: `script.*` だけ許可されます。Home Assistant側のscriptを `script.turn_on` で実行します。
- `ha_service` + `entity_id`: `switch.turn_on/off`、`light.turn_on/off`、`fan.turn_on/off` だけ許可されます。serviceのdomainとentityのdomainは一致している必要があります。

`lock.unlock` や `cover.close_cover` のような操作は直接allowlistできません。必要な場合はHome Assistant側にscriptを作成し、`confirm_required: true` を付けてください。

## curl例

```powershell
curl.exe http://127.0.0.1:8787/health
```

```powershell
curl.exe http://127.0.0.1:8787/actions `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN"
```

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/preview `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-1", "user_text": "照明をつけて" }'
```

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-2", "user_text": "照明をつけて" }'
```

確認必須の操作は、最初の `execute` では実行されません。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/curtain_close/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-3", "user_text": "カーテンを閉めて" }'
```

実行する場合は `confirmed: true` を送ります。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/curtain_close/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-4", "confirmed": true }'
```

dry-runはHome Assistantを呼びません。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-5", "dry_run": true }'
```

## Dify HTTP Request node例

一覧取得:

- Method: `GET`
- URL: `http://127.0.0.1:8787/actions`
- Headers:
  - `Authorization`: `Bearer {{HOME_CONTROL_API_TOKEN}}`

プレビュー:

- Method: `POST`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/preview`
- Headers:
  - `Authorization`: `Bearer {{HOME_CONTROL_API_TOKEN}}`
  - `Content-Type`: `application/json`
- Body:

```json
{
  "source": "dify",
  "request_id": "{{workflow_run_id}}",
  "user_text": "{{query}}"
}
```

実行:

- Method: `POST`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/execute`
- Headers:
  - `Authorization`: `Bearer {{HOME_CONTROL_API_TOKEN}}`
  - `Content-Type`: `application/json`
- Body:

```json
{
  "source": "dify",
  "request_id": "{{workflow_run_id}}",
  "user_text": "{{query}}",
  "confirmed": false
}
```

Dify用OpenAPI schemaは [docs/dify-openapi.yaml](docs/dify-openapi.yaml) にあります。FastAPI標準の `/openapi.json` も利用できます。

## Home Assistant script例

[docs/home-assistant-scripts.example.yaml](docs/home-assistant-scripts.example.yaml) を参照してください。SwitchBot Cloud APIやMatter機器の個別制御は、Home Assistant側のscriptに書きます。

## 操作ログ

既定では `.cache/home_control/events.jsonl` にJSONLで保存します。API token、Authorization、password、secretを含むキーは保存しません。

## テスト

```powershell
uv run pytest
```
