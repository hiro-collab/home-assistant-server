# UDP 演出通知

この文書は、TouchDesigner などの外部演出ツールへ家電操作の進行を通知する UDP JSON の仕様です。

## 用途

UDP 通知は、アバター、照明演出、画面演出などを家電操作と同期させるための任意機能です。家電操作の成否判定や状態観測には使いません。

## 設定

既定では無効です。

```yaml
udp_events:
  enabled: true
  host: "127.0.0.1"
  port: 7000
  event_type: "home_control_magic"
```

## 送信条件

`execute` が実際に Home Assistant を呼ぶときだけ送信します。`preview`、`dry_run`、確認待ちの操作では送信しません。

UDP 送信に失敗しても家電操作は止めません。失敗は JSONL ログに `udp_event_failed` として記録します。

## payload

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

成功時は `phase: "done"`、失敗時は `phase: "error"` を送ります。`done` にはユーザー向けメッセージ、`error` にはユーザー向けメッセージと汎用エラーコードを含めます。

Home Assistant 側の詳細エラー本文は、UDP payload や HTTP レスポンスには出しません。
