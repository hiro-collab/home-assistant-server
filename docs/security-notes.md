# セキュリティ注意

この文書は、Home Control Safety Bridge を運用するときに守る境界です。

## 公開範囲

既定は `127.0.0.1:8787` で起動します。別マシンから呼ぶ場合だけ `0.0.0.0` を検討し、ルーター越しに公開しないでください。

## token

`HOME_CONTROL_API_TOKEN` は 32 文字以上のランダム値にします。`.env.example` のプレースホルダーや短い値は拒否されます。

Home Assistant の long-lived access token はこのブリッジだけが持ちます。Dify、AITuberKit、sword-voice-agent へ渡しません。

## allowlist

外部クライアントに許可する操作は、設定ファイルの `actions` に登録した `action_id` だけです。玄関、鍵、セキュリティ、暖房器具などの危険な操作は初期 allowlist に入れないでください。

## ログ

操作ログは既定で `.cache/home_control/events.jsonl` に保存します。API token、Authorization、password、secret、ユーザー発話本文は保存しません。

## 疑似障害テスト

fault injection はローカル検証専用です。`faults.enabled: true` と `HOME_CONTROL_FAULT_MODE=1` の両方が揃った場合だけ有効になり、本番環境では使いません。
