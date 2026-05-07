# Dify からの呼び出し

この文書は、Dify HTTP Request node から Home Control Safety Bridge を呼ぶための最小例です。詳細な schema は [dify-openapi.yaml](dify-openapi.yaml) または FastAPI の `/openapi.json` を参照してください。

## 共通ヘッダー

`GET /health` 以外は、Dify からブリッジ用 API token を送ります。

```http
Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}
Content-Type: application/json
```

## 操作一覧

許可済みの操作だけを取得します。Dify 側はこの結果に含まれる `action_id` だけを後続 node に渡します。

- Method: `GET`
- URL: `http://127.0.0.1:8787/actions`
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`

## プレビュー

実行前に、ユーザーへ返す文言、確認要否、期待メタデータを確認します。Home Assistant は呼びません。

- Method: `POST`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/preview`
- Headers: 共通ヘッダー
- Body:

```json
{
  "source": "dify",
  "request_id": "{{workflow_run_id}}",
  "user_text": "{{query}}"
}
```

## 実行

確認不要の操作であれば、Home Assistant へ script 実行を送ります。

- Method: `POST`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/execute`
- Headers: 共通ヘッダー
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

## 分岐で使う応答

- `status: "confirmation_required"`: ユーザー確認を挟み、返ってきた確認トークンを次の `execute` に渡します。
- `status: "dry_run"`: Home Assistant は呼ばれていません。
- `status: "duplicate"`: 同じ `request_id` の再送です。二重送信は行われず、元の実行 ID が返ります。
- `execution_id`: Home Assistant へ命令を出した一回分の追跡 ID です。観測結果やユーザー確認と結合するときに保持します。

`request_id` は Dify の workflow run ID など、実行ごとに一意な値を使います。
