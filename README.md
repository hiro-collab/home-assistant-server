# Home Control Safety Bridge

Home Assistant の script allowlist だけを外部クライアントへ公開するローカル HTTP ブリッジです。Dify / AITuberKit / sword-voice-agent などの音声・エージェント層から、家電操作を安全に中継するための単体モジュールとして使います。

## 責務

- 外部入力から Home Assistant の任意 service、entity、URL を受け取らない。
- 設定ファイルで許可した `action_id` だけを受け付ける。
- Home Assistant REST API へ送る操作は `script.turn_on` に限定し、対象 script は `ha_script: script.*` から決める。
- Matter、SwitchBot、機器固有 API の差分は Home Assistant 側の script に閉じ込める。
- `expected_effect` は後続の観測・確認と結合するための期待メタデータであり、実状態の証明ではない。

## 起動

```powershell
uv sync --extra dev
Copy-Item config/home-control.example.yaml config/home-control.yaml
Copy-Item .env.example .env
```

`config/home-control.yaml` の `home_assistant.base_url` と `actions` を環境に合わせて編集します。起動プロセスが `.env` を読むとは限らないため、実行環境では次の環境変数を明示してください。

```powershell
$env:HOME_CONTROL_CONFIG = "config/home-control.yaml"
$env:HOME_CONTROL_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:HOME_ASSISTANT_TOKEN = "Home Assistant の Long-lived access token"
uv run home-control-bridge
```

直接 uvicorn で起動する場合:

```powershell
uv run uvicorn home_control_bridge.main:app --host 127.0.0.1 --port 8787
```

## API / 入出力

`GET /health` は認証なしで使えます。設定状態、Home Assistant 疎通、action 件数を返します。

それ以外の API は `Authorization: Bearer <HOME_CONTROL_API_TOKEN>` または `X-API-Token: <HOME_CONTROL_API_TOKEN>` が必要です。

| Endpoint | 用途 |
| --- | --- |
| `GET /actions` | 許可済み action の `action_id`、表示名、確認要否、期待メタデータを返す。 |
| `POST /actions/{action_id}/preview` | 実行予定の内容を返す。Home Assistant は呼ばない。 |
| `POST /actions/{action_id}/execute` | 確認、dry-run、重複判定を通過した場合だけ Home Assistant を呼ぶ。 |

`preview` / `execute` の body は次の項目です。未知の項目は拒否します。

- `source`: 呼び出し元名。既定値は `unknown`。
- `request_id`: 呼び出し元が発行する実行単位 ID。同じ値は短時間重複扱い。
- `user_text`: ユーザー発話。ログには本文を保存しない。
- `dry_run`: `true` の場合は Home Assistant を呼ばない。
- `confirmed` / `confirmation_token`: 確認必須 action の二段階実行に使う。

レスポンスには `ok`、`action_id`、`executed`、`status`、`message`、`speak` を含めます。実際に Home Assistant へ命令を出した場合は `execution_id` と `issued_at` も返します。`status` は `preview`、`confirmation_required`、`dry_run`、`duplicate`、`submitted`、`failed` のいずれかです。

OpenAPI は [docs/dify-openapi.yaml](docs/dify-openapi.yaml) と FastAPI の `/openapi.json` を参照してください。手動実行、Dify HTTP Request node、確認必須操作、dry-run、重複実行防止は [docs/api-usage.md](docs/api-usage.md) にあります。

## 統合時の契約

- クライアントは `GET /actions` で得た `action_id` だけを `preview` / `execute` に渡す。
- クライアントは Home Assistant の service/entity 名を生成しない。
- `request_id` は外部ワークフローの実行ごとに一意にする。同じ `request_id` の再送は二重送信を避け、元の `execution_id` を返す。
- 確認必須 action は、確認トークンを受け取った後に `confirmed: true` と `confirmation_token` を付けて再実行する。
- `execution_id` は「Home Assistant へ命令を出した」単位の correlation id として扱う。観測結果やユーザー確認ラベルは、後続サービス側で `execution_id` と結合する。
- UDP イベントは任意の演出連携です。実送信が起きた `execute` でのみ `start` / `done` / `error` を送ります。`preview`、`dry_run`、確認待ちでは送らず、UDP 失敗は家電操作を止めません。

詳しい責務境界は [docs/module-responsibilities.md](docs/module-responsibilities.md)、外部サービスとの接続契約は [docs/integration-contract.md](docs/integration-contract.md)、UDP 通知は [docs/event-notifications.md](docs/event-notifications.md)、疑似障害テストは [docs/fault-injection.md](docs/fault-injection.md) を参照してください。

## セキュリティ注意

- 既定は `127.0.0.1:8787` で起動する。別マシンから呼ぶ場合だけ `0.0.0.0` を検討し、ルーター越しに公開しない。
- `HOME_CONTROL_API_TOKEN` は 32 文字以上のランダム値にする。`.env.example` のプレースホルダーや短い値は拒否される。
- 玄関、鍵、セキュリティ、暖房器具などの危険な操作は初期 allowlist に入れない。
- 操作ログは既定で `.cache/home_control/events.jsonl` に保存する。API token、Authorization、password、secret、ユーザー発話本文は保存しない。
- fault injection はローカル検証専用です。`faults.enabled: true` と `HOME_CONTROL_FAULT_MODE=1` の両方が揃った場合だけ有効になり、本番環境では使わない。

## 関連文書

Home Assistant script の例は [docs/home-assistant-scripts.example.yaml](docs/home-assistant-scripts.example.yaml) にあります。剪定前の長い README は [docs/archive/README-before-pruning-2026-05-07.md](docs/archive/README-before-pruning-2026-05-07.md) に退避しました。archive は履歴確認用であり、統合仕様の正本ではありません。
