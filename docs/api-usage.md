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

## ローカル操作画面

ブラウザで `http://127.0.0.1:8787/operator` を開くと、同じ allowlist catalog を
人間が確認して選べるローカル操作画面を使えます。画面には `HOME_CONTROL_API_TOKEN`
の値を埋め込まず、operator が入力した token をブラウザメモリ内のリクエストヘッダーにだけ使います。

この画面は API と同じ `/actions`、`/actions/{action_id}/state`、preview、execute を
呼びます。固定ショートカットは token、Home Assistant URL、entity ID を埋め込まず、
押されたときだけ既存の認証済み action API を呼びます。ショートカットの意味と
証明上限は `docs/integration-contract.md` を見ます。

## 状態確認

明示的な HA state metadata を持つ action だけが読み取り専用 state check を返します。
Home Assistant の URL や entity ID は返しません。この API は post-action / restore 後の
確認用です。実行前に使う場合は、現在 state が期待値かどうかを読むだけで、command が
状態変化を起こした proof にはなりません。

- Method: `GET`
- URL: `http://127.0.0.1:8787/actions/{{action_id}}/state`
- Headers: `Authorization: Bearer {{HOME_CONTROL_API_TOKEN}}`

応答の `status`、`state_tracking`、`expected_states`、position fields、wait metadata は
command metadata と state-check 結果です。surface ごとの証明境界は
`docs/integration-contract.md` に集約しています。

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
確認必須 action の `confirmation_token` は一度だけ使えます。dry-run の確認に
使った token は消費済みなので、本実行では直前に preview を取り直し、新しい token を使います。

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
