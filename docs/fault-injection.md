# 疑似障害テスト

この文書は、Home Assistant を実際に操作せずに成功、失敗、タイムアウト、確認要求などを返すテスト機能の仕様です。

## 用途

疑似障害テストは、Dify や音声エージェント側の分岐を確認するためのローカル E2E 検証機能です。本番環境では有効化しません。

## 有効化条件

誤有効化を避けるため、設定ファイルの `faults.enabled: true` と環境変数 `HOME_CONTROL_FAULT_MODE=1` の両方が揃ったときだけ有効になります。

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

## 発火条件

`match` には、どのリクエストで疑似障害を返すかを指定します。

- `action_id`: 対象の許可済み操作。
- `source`: 呼び出し元。
- `request_id`: 実行単位 ID の完全一致。
- `request_id_prefix` / `request_id_suffix`: 実行単位 ID の前方一致または後方一致。
- `request_id_regex`: 実行単位 ID の正規表現一致。
- `user_text_contains` / `user_text_regex`: ユーザー発話による一致。
- `confirmed`: 確認済みリクエストかどうか。

正規表現はテスト用の短く単純なものに限定されます。危険なネスト量指定子や後方参照は設定ロード時に拒否されます。

## 疑似挙動

利用できる `scenario` は以下です。

- `always_success`: 常に成功応答を返す。
- `fail_once_then_success`: 同じ試行系列で一回だけ失敗し、その後は成功する。
- `fail_twice_then_success`: 同じ試行系列で二回失敗し、その後は成功する。
- `fail_always`: 常に失敗する。
- `confirmation_required`: 確認必須応答を返す。
- `timeout_once`: 同じ試行系列で一回だけタイムアウト扱いにする。
- `unsupported_action`: 未対応操作として返す。
- `duplicate`: 重複実行として返す。

`fail_once_then_success`、`fail_twice_then_success`、`timeout_once` は、同じ rule、`action_id`、`source`、正規化済み `request_id` ごとに試行回数を数えます。`request_id` 末尾の `-attempt-1`、`-try-2`、`-retry-3` のような表現は同じ試行系列として扱います。

試行状態は短時間で失効し、保持数にも上限があります。

## ログと秘匿

疑似障害が発火すると、操作ログに `event: "fault_injected"`、`source`、`action_id`、`request_id`、`scenario`、`attempt`、`status` を記録します。

API token、confirmation token、ユーザー発話本文は保存しません。未認証の `GET /health` では、fault mode の有効状態やルール数を公開しません。
