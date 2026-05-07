# API 利用例

この文書は、手動確認と Dify 連携で使う HTTP 呼び出し例です。詳細な schema は [dify-openapi.yaml](dify-openapi.yaml) または FastAPI の `/openapi.json` を参照してください。

## 手動確認

起動確認:

```powershell
curl.exe http://127.0.0.1:8787/health
```

許可済み操作の取得:

```powershell
curl.exe http://127.0.0.1:8787/actions `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN"
```

実行前の確認。Home Assistant は呼びません。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/preview `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-1", "user_text": "照明をつけて" }'
```

実行。確認不要の操作であれば Home Assistant に script 実行を送ります。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-2", "user_text": "照明をつけて" }'
```

## 確認必須操作

確認必須の操作は、最初の `execute` では実行されません。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/curtain_close/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-3", "user_text": "カーテンを閉めて" }'
```

レスポンスの確認トークンを、ユーザー確認後の実行リクエストに含めます。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/curtain_close/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-4", "confirmed": true, "confirmation_token": "<confirmation_token>" }'
```

## dry-run

dry-run は Home Assistant を呼ばず、実行予定の応答だけを返します。

```powershell
curl.exe -X POST http://127.0.0.1:8787/actions/light_on/execute `
  -H "Authorization: Bearer $env:HOME_CONTROL_API_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{ "source": "dify", "request_id": "demo-5", "dry_run": true }'
```

## 重複実行防止

`request_id` が同じ実行リクエストは短時間重複として扱い、Home Assistant への二重送信を避けます。Dify では workflow run ID など、実行ごとに一意な値を入れてください。

## 実行レスポンス

`execute` が実際に Home Assistant へ命令を送るとき、レスポンスには実行ごとの `execution_id`、`issued_at`、`status` が含まれます。`action_id` は allowlist 上の操作名のままです。

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

## Dify HTTP Request node

一覧取得:

- Method: `GET`
- URL: `http://127.0.0.1:8787/actions`
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`

プレビュー:

- Method: `POST`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/preview`
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`, `Content-Type: application/json`
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
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`, `Content-Type: application/json`
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
