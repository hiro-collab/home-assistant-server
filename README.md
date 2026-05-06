# Home Control Safety Bridge

Dify / AITuberKit / sword-voice-agent から Home Assistant を安全に操作するためのローカルHTTPブリッジです。

このブリッジは、リクエストから Home Assistant の任意サービス名、任意entity、任意URLを受け取りません。設定ファイルで許可した `action_id` だけを受け付け、内部では Home Assistant REST API の `/api/services/script/turn_on` だけを呼びます。Matter / SwitchBot の違いは Home Assistant の script 側に閉じ込めます。

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
$env:HOME_CONTROL_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:HOME_ASSISTANT_TOKEN = "Home Assistant の Long-lived access token"
```

`HOME_CONTROL_API_TOKEN` は32文字以上のランダム値にしてください。`.env.example` のプレースホルダーや短い値のままだと起動時/認証時に拒否されます。

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
  min_api_token_length: 32
udp_events:
  enabled: false
  host: "127.0.0.1"
  port: 7000
  event_type: "home_control_magic"
faults:
  enabled: false
  enabled_env: "HOME_CONTROL_FAULT_MODE"
  rules:
    - match:
        source: "dify"
        action_id: "light_off"
      scenario: "fail_once_then_success"
      message: "simulated transient failure"
actions:
  light_on:
    label: "照明をつける"
    ha_script: "script.demo_light_on"
    confirm_required: false
    response_text: "照明をつけました。"
    expected_effect:
      domain: "light"
      service: "turn_on"
      entity_id: "light.demo_room"
      expected_state: "on"
  curtain_close:
    label: "カーテンを閉める"
    ha_script: "script.curtain_close"
    confirm_required: true
    response_text: "カーテンを閉めました。"
```

`ha_script` は `script.*` だけ許可されます。`light.turn_on` や `lock.unlock` のようなHome Assistantサービスは、このブリッジの設定としても受け付けません。

`expected_effect` は任意の追跡用メタデータです。Home Assistant へ実際に送るリクエストは引き続き `script.turn_on` だけで、`expected_effect` は後続の観測やユーザー確認と結合するための期待状態を表します。これは確定ラベルではありません。

## UDPイベント

TouchDesignerなどの外部演出ツールに、家電操作の開始・完了・失敗をUDP JSONで通知できます。既定では無効です。

```yaml
udp_events:
  enabled: true
  host: "127.0.0.1"
  port: 7000
  event_type: "home_control_magic"
```

`execute` が実際にHome Assistantを呼ぶときだけ送信します。`preview`、`dry_run`、確認待ちの操作では送信しません。UDP送信に失敗しても家電操作は止めず、JSONLログに `udp_event_failed` を残します。

開始時:

```json
{
  "type": "home_control_magic",
  "phase": "start",
  "action_id": "light_off",
  "label": "ライトを消す",
  "source": "dify",
  "request_id": "..."
}
```

成功時は `phase: "done"`、失敗時は `phase: "error"` を送ります。`done` には `message`、`error` には `message` と汎用エラーコードが追加されます。Home Assistant 側の詳細エラー本文はUDPやHTTPレスポンスには出しません。

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

レスポンスの `confirmation_token` を確認後の実行リクエストに含めます。確認トークンは短時間で失効し、1回だけ使えます。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/curtain_close/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-4", "confirmed": true, "confirmation_token": "<confirmation_token>" }'
```

dry-runはHome Assistantを呼びません。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-5", "dry_run": true }'
```

`request_id` が同じ実行リクエストは短時間重複として扱い、Home Assistant への二重送信を避けます。Dify の `workflow_run_id` など、実行ごとに一意な値を入れてください。

## Action tracking

`execute` が実際に Home Assistant へ命令を送るとき、レスポンスには実行ごとの `execution_id`、`issued_at`、`status` が含まれます。既存の `action_id` は allowlist 上の操作名のままです。

```json
{
  "ok": true,
  "action_id": "light_on",
  "execution_id": "2c9f9f6a-1f4b-43aa-89ef-4e1c7c73f9d2",
  "executed": true,
  "status": "submitted",
  "issued_at": "2026-05-06T03:20:15.123456+00:00",
  "domain": "light",
  "service": "turn_on",
  "entity_id": "light.demo_room",
  "expected_state": "on",
  "message": "照明をつけました。",
  "speak": "照明をつけました。",
  "request_id": "demo-2"
}
```

`execution_id` は「Home Assistant に命令を出した」単位の correlation id です。camera-hub の観測やユーザー確認で得たラベルは、後続サービス側で `execution_id + observation_id + label` として結合してください。同じ `request_id` の重複リクエストには、元の `execution_id` が返ります。

## Fault injection

E2E検証用に、Home Assistantへ実リクエストを送らず `execute` の応答を差し替える fault injection を設定できます。既定では完全に無効です。誤有効化を避けるため、`faults.enabled: true` と `HOME_CONTROL_FAULT_MODE=1` の両方が揃ったときだけ有効になります。本番環境では有効化しないでください。

```yaml
faults:
  enabled: false
  enabled_env: "HOME_CONTROL_FAULT_MODE"
  rules:
    - match:
        source: "dify"
        action_id: "light_off"
        request_id_prefix: "e2e-light-off"
      scenario: "fail_once_then_success"
      message: "simulated transient failure"
```

`match` は `action_id`、`source`、`request_id`、`request_id_prefix`、`request_id_suffix`、`request_id_regex`、`user_text_contains`、`user_text_regex`、`confirmed` を指定できます。正規表現はテスト用の短く単純なものに限定され、危険なネスト量指定子や後方参照は設定ロード時に拒否されます。外部workflow固有のIDは扱わず、`request_id` と `execution_id` を correlation id として使います。

利用できる `scenario` は以下です。

- `always_success`
- `fail_once_then_success`
- `fail_twice_then_success`
- `fail_always`
- `confirmation_required`
- `timeout_once`
- `unsupported_action`
- `duplicate`

`fail_once_then_success`、`fail_twice_then_success`、`timeout_once` は、同じrule・`action_id`・`source`・正規化済み `request_id` ごとにattemptを数えます。`request_id` 末尾の `-attempt-1`、`-try-2`、`-retry-3` のような表現は同じ試行系列として扱います。attempt状態は短時間で失効し、保持数にも上限があります。

faultが発火すると操作ログに `event: "fault_injected"`、`source`、`action_id`、`request_id`、`scenario`、`attempt`、`status` を記録します。API token、confirmation token、ユーザー発話本文は保存しません。未認証の `GET /health` では fault mode の有効状態やルール数を公開しません。

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
  "confirmed": false,
  "confirmation_token": "{{confirmation_token}}"
}
```

Dify用OpenAPI schemaは [docs/dify-openapi.yaml](docs/dify-openapi.yaml) にあります。FastAPI標準の `/openapi.json` も利用できます。

Dify / sword-voice-agent は、実行レスポンスの `execution_id` を保持して、camera-hub の `observation_id` とユーザー確認ラベルを learner-service へ渡します。

```json
{
  "execution_id": "2c9f9f6a-1f4b-43aa-89ef-4e1c7c73f9d2",
  "action_id": "light_on",
  "observation_id": "obs_20260506_122018",
  "label": "on",
  "label_source": "user_confirmation"
}
```

## Home Assistant script例

[docs/home-assistant-scripts.example.yaml](docs/home-assistant-scripts.example.yaml) を参照してください。SwitchBot Cloud APIやMatter機器の個別制御は、Home Assistant側のscriptに書きます。

## 操作ログ

既定では `.cache/home_control/events.jsonl` にJSONLで保存します。API token、Authorization、password、secretを含むキーは保存しません。ユーザー発話本文は保存せず、本文の有無と文字数だけを残します。

## テスト

```powershell
uv run pytest
```
