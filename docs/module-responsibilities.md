# Home Control Safety Bridge の責務境界

この文書は、このモジュールが担当することと担当しないことを分けるための責務メモです。

## 担当すること

- Dify、AITuberKit、sword-voice-agent などからの家電操作リクエストを、Home Assistant へ安全に中継する。
- 外部クライアントへ公開する操作を、設定ファイルの `actions` に登録された `action_id` に限定する。
- Home Assistant へ送る実操作を `script.turn_on` に限定し、呼び出す script を `ha_script: script.*` から決める。
- 確認必須操作では、一度目の実行要求を止め、確認用の一時トークンを返す。
- 同じ実行要求が短時間に再送された場合、Home Assistant への二重送信を避ける。
- 実際に Home Assistant へ命令を出した単位に `execution_id` を付け、後続の観測やユーザー確認と結合できるようにする。
- API token、Home Assistant token、ユーザー発話本文を操作ログに残さない。

## 担当しないこと

- 外部クライアントから Home Assistant の service 名、entity 名、任意 URL を受け取って実行すること。
- `light.turn_on`、`lock.unlock` などの Home Assistant service を直接 allowlist にすること。
- Matter、SwitchBot、その他機器固有 API の分岐をこのモジュールに持つこと。
- カメラ観測、状態推定、学習用ラベルの確定をこのモジュールで行うこと。
- UDP 通知を家電操作の成否判定として扱うこと。

## Home Assistant 側に置く責務

Matter、SwitchBot Cloud API、機器別の entity 操作、危険操作に対する追加条件は Home Assistant 側の script に置きます。script の例は [home-assistant-scripts.example.yaml](home-assistant-scripts.example.yaml) を参照してください。

## 設定上の境界

`control_type` は操作形態、`verification.mode` は確認層、
`state_authority` は状態の出所を表す command metadata です。これらは
現在の家電 state そのものではありません。

HA-visible state proof 可能な action は、明示的な `state_authority: ha_entity`、
`verification.mode: ha_state`、および `expected_effect` を持つ必要があります。
`expected_effect` だけでは tracked になりません。`open_loop`、`submitted_only`、
`command_ack_only` は command submission だけで、HA-visible state proof ではありません。

`/actions/{action_id}/state` は post-action / post-restore の読み取り専用 state check です。
実行前に読む場合は、現在 state がすでに期待値かどうかを見るだけで、command が変化を起こした
proof にはなりません。cover / door のような position action は `verification.position`
の threshold も一致して初めて matched と扱います。

action family ごとの現在 row と proof ceiling は `config/home-control.example.yaml` を
単一の source/static 例にします。API surface の証明境界は
`docs/integration-contract.md` に集約し、この文書には重複した action-family 表や
個別実証例を置きません。
