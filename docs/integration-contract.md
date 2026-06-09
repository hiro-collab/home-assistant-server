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

## 演出通知

UDP 通知は TouchDesigner などの演出同期用です。命令の開始、成功、失敗を横流ししますが、家電状態の観測結果や成功判定には使いません。詳しくは [event-notifications.md](event-notifications.md) を参照してください。
