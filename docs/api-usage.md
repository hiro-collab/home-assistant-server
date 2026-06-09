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

## 状態確認

`verification.mode: ha_state` かつ `expected_effect` がある action について、Home Assistant の現在 state を読み取り専用で確認します。Home Assistant の URL や entity ID は返しません。
これは post-action / restore 後の確認です。実行前に使う場合は、現在 state がすでに
その action の期待結果になっているかを読むだけなので、preflight の pass/fail と混同しないでください。
実行前に state tracking 可能かを知りたい場合は、`GET /actions` の catalog で対象 action の
`control_type`、`state_authority`、`verification_mode`、`state_tracking` を確認します。
`tracked` 以外は HA state proof ではなく、外部観測、手動確認、または command acknowledgement
だけの action です。

- Method: `GET`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/state`
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`

応答は `action_id`、`control_type`、`state_authority`、`verification_mode`、`state_tracking`、
`expected_state`、`actual_state`、`status` を見て判定します。`status: "matched"` 以外は、
実行 proof ではなく追加確認が必要な状態です。

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
