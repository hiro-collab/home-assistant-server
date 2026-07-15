# 外部サービスとの接続契約

この文書は、Dify、AITuberKit、sword-voice-agent、観測系サービスが Home Control Safety Bridge と接続するときの契約です。

## 操作の選び方

クライアントは `GET /actions` で返った操作だけをユーザーへ提示し、`preview` / `execute` に渡します。Home Assistant の service 名や entity 名をクライアント側で生成してはいけません。

`action_id` は「許可された操作の名前」です。家電機器や Home Assistant entity を直接指す ID ではありません。

## 認証

`GET /health` 以外は、次のどちらかでブリッジ用 API token を送ります。

```http
Authorization: Bearer <HOME_CONTROL_API_TOKEN>
X-API-Token: <HOME_CONTROL_API_TOKEN>
```

Home Assistant の long-lived access token はこのブリッジだけが持ちます。Dify や音声エージェントへ渡しません。

## 実行単位の追跡

クライアントは、実行ごとに一意な `request_id` を送ります。Dify では workflow run ID のように、同じ実行を識別できる値を使います。

同じ `request_id` が短時間に再送された場合、このブリッジは二重送信を避け、元の `execution_id` を返します。

`execution_id` は、Home Assistant へ命令を出した一回分の追跡 ID です。後続サービスはこの ID を使って、観測結果やユーザー確認を命令と結びつけます。

```json
{
  "execution_id": "2c9f9f6a-1f4b-43aa-89ef-4e1c7c73f9d2",
  "action_id": "light_on",
  "observation_id": "obs_20260506_122018",
  "label": "on",
  "label_source": "user_confirmation"
}
```

## 確認必須操作

危険度や生活影響が高い操作は、設定で確認必須にします。最初の `execute` は Home Assistant を呼ばず、確認が必要であることと確認用の一時トークンを返します。

ユーザー確認後、クライアントは `confirmed: true` と `confirmation_token` を付けて同じ action を再実行します。確認トークンは短時間で失効し、一度だけ使えます。
確認必須 action で dry-run まで確認する場合、dry-run に使った token は消費済みです。
実際に Home Assistant へ送る直前に preview を取り直し、新しい token を本実行だけに使います。

## dry-run

`dry_run: true` は、Home Assistant へ命令を送らずに実行予定の応答だけを返す試運転です。Dify 側の分岐確認や統合テストに使います。

## 証明境界

Home Control Safety Bridge は action 実行の入口と Home Assistant への中継結果を扱います。
各 surface の証明上限を混ぜないでください。

| surface | 実行副作用 | 証明できること | 証明しないこと |
|---|---|---|---|
| `/operator` fixed shortcut | クリック前はなし | allowlist 済み action ID と route metadata が見えること | live execute、HA state match、物理家電動作 |
| `GET /actions` | なし | allowlist catalog、restore/stop 候補、live readiness metadata | action 実行、catalog 外 action の許可 |
| `POST /actions/{action_id}/preview` | Home Assistant 呼び出しなし | 実行予定文言、確認要否、一時 confirmation token | live execute、dry-run、HA state match |
| `POST /actions/{action_id}/execute` with `dry_run: true` | Home Assistant 呼び出しなし | execute payload の分岐と dry-run 応答 | live execute、confirmation token の再利用可否 |
| `POST /actions/{action_id}/execute` | Home Assistant script 呼び出しあり | command submission と bridge response | HA-visible state match、物理家電動作 |
| `GET /executions/{execution_id}` | なし | 同一 process 内の submission lifecycle / count の class-only CheckTracking | HA-visible state、物理家電動作、process restart をまたぐ exactly-once |
| `GET /actions/{action_id}/state` | なし | HA-visible current state / expected state の読み取り一致 | command submission、物理家電動作 |

`CheckTracking` 相当の追跡 ID、`CheckState` 相当の HA-visible state、一時
confirmation token、物理家電状態は別々の層です。reviewed route が要求した層だけを
結果として主張し、`command accepted` や `confirmed-submitted` を physical proof として
扱わないでください。

すべての本実行には Bridge 自身が bounded monotonic deadline を設定します。上流から
より短い deadline が渡された場合はそれを採用し、request body の `source` 文字列を
期限適用の信頼根拠にはしません。Thought Core は同じ turn deadline から導出した
deadline と決定的な `request_id` を渡します。Bridge は `(action_id, request_id)`
ごとに、`submission_in_flight`、`submission_completed`、
`failed_before_submit`、`submission_outcome_unknown`、`expired_before_submit` を
process lifetime 内だけ保持します。同じ key の再要求は Home Assistant へ再送せず、
同じ execution lifecycle を返します。

接続前の確定失敗は submission count 0、HTTP request が届いた可能性を除外できない
timeout / disconnect / protocol failure は count unknown とします。後者を成功・失敗の
どちらにも読み替えず、blind retry や自動 restore を行いません。2xx は submission
count 1 の証拠に限られ、HA-visible state や物理結果の証拠ではありません。terminal
record だけが TTL cleanup 対象で、in-flight record は期限だけで消しません。Bridge
再起動をまたぐ exactly-once が必要な場合は、別途 local-private ledger の設計と承認が
必要です。

`restore_required: false` の light / fan などは command stimulus として扱えますが、
これは「戻し操作不要」の source/static planning metadata であり、現在状態が読めたことや
物理状態が変化したことを意味しません。door は `door_open` に対して `door_close` を
復帰候補にし、vacuum の終端/復帰候補は route ごとに `vacuum_return` などの allowlist
action で明示します。

## 演出通知

UDP 通知は TouchDesigner などの演出同期用です。命令の開始、成功、失敗を横流ししますが、家電状態の観測結果や成功判定には使いません。詳しくは [event-notifications.md](event-notifications.md) を参照してください。
