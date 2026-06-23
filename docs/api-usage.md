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
呼ぶため、証明上限や confirmation token の扱いは API と同じです。`dry run` ボタンは
Home Assistant を呼ばず、`execute` / `confirm execute` ボタンだけが既存の実行 API へ進みます。
起動直後に catalog を読まなくても route の候補を選べるように、AC、light、fan、door、
vacuum の代表 action は route metadata 付きの固定ショートカットとして表示されます。ショートカットは token や
Home Assistant の URL/entity ID を埋め込まず、押されたときだけ既存の認証済み action API を呼びます。
light / fan は command stimulus として復帰を要求しない候補です。door は `door_open` に対して
`door_close` を復帰候補として扱い、vacuum は `vacuum_return` を復帰/終端候補として扱います。
`vacuum_start`、`vacuum_pause`、`door_stop` は別/条件付き row として扱い、この固定ショートカットには
含めません。

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
`expected_state`、`expected_states`、`actual_state`、`status` を見て判定します。
`expected_states` は、cover / vacuum / climate などで複数の完了 state を許容する時の
読み取り専用 proof 条件です。`status: "matched"` 以外は、実行 proof ではなく追加確認が必要な状態です。
cover / door のように `verification.position` が設定された action では、
`expected_position_min` / `expected_position_max` と `actual_position` も判定対象です。
state が一致していても `position_status: "matched"` でなければ `status: "matched"` にはなりません。
catalog / preview には `settle_seconds` と `timeout_seconds` も出ますが、これは実行後に
どれだけ待ってから state を読むかのメタデータであり、それ単体では proof ではありません。

`GET /actions` と preview / execute の応答には、レビューや planner が live 対象を
選ぶための公開診断も含まれます。

- `proof_ceiling`: その action が証明できる上限です。
- `live_test_candidate`: この構成で live review 候補として扱うかどうかです。
- `live_test_readiness`: `test_now`、`do_not_test_current_config`、または
  `not_live_test_candidate` です。
- `live_test_blockers`: `missing_ha_visible_success_criterion`、
  `missing_restore_or_stop`、`safety_requirement:*` など、実行しない理由です。
- `restore_required`: `false` の action は、復帰や HA state proof を要求しない
  command stimulus として review 計画に入れられます。これは command submission
  proof 以上の主張ではありません。
- `restore_action_id` / `stop_action_id`: live 実行時に戻す、または止める候補 action です。
- `terminal_action`: `vacuum_return` のように、その action 自体が安全な終端/復帰として
  扱えることを示します。
- `safety_requirements`: 障害物、床面安全、active task など、システムが自動で証明できない
  live 前提です。

`live_test_readiness: "test_now"` 以外の action は、現構成レビューの live batch に
自動投入しないでください。Home Assistant の状態が読める行でも、route が復帰や
state proof を要求するなら `restore_action_id` / `stop_action_id` / `terminal_action`
を明示します。逆に、light / fan のような command-stimulus 行は
`restore_required: false` にして、unknown current state を proof limitation として
返しながら command submission の刺激にできます。

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
