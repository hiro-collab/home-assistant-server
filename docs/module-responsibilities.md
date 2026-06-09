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

`control_type` は「その家電操作が stateful target なのか、stateless toggle なのか、
position/mode/job command なのか」を表す分類です。`verification.mode` は、その action が
どの proof layer で確認できるかを表します。
`state_authority` は、状態の出所を表します。`ha_entity` は Home Assistant entity の現在 state、
`ha_inferred` は推定または last-command shadow state、`open_loop` は現在状態を持たない操作、
`submitted_only` は script submit だけを意味します。`ha_inferred` と `open_loop` は物理状態 proof
ではありません。

`expected_effect` は `verification.mode: ha_state` のときだけ、Home Assistant state proof に
使うメタデータです。実際に Home Assistant へ送る命令を変えるものではなく、実状態を証明する
ラベルでもありません。

bridge の `/actions/{action_id}/state` は、この `expected_effect.expected_state` と
Home Assistant の現在 state を読み取り専用で比較します。これは実行後または restore 後に
「期待した state になったか」を確認するための post-state check です。実行前の安全確認では、
`/actions` の catalog にある `control_type`、`state_authority`、`verification_mode`、`state_tracking` を見て、
対象 action が HA state tracking 可能かだけを確認します。実行前に `matched` でないことは、
bridge 起動失敗や action catalog 失敗を意味しません。

For actions with reliable HA state but slower transitions, `verification.accepted_states`
can list additional acceptable end states, and `settle_seconds` / `timeout_seconds`
document the wait window for the ticketed execute/wait/post-state procedure. These fields
are only meaningful with `verification.mode: ha_state` and a real `expected_effect`;
they must not be used to turn `switch:unknown`, script-wrapper state, or inferred
shadow state into physical proof.

Use separate proof labels in reports and API clients:

- `command accepted`: bridge/Home Assistant accepted the command; this is not appliance-state proof.
- `HA state matched`: the post-action or post-restore state matched expected/accepted states.
- `external observed`: camera, sensor, manual observation, or another independent source confirmed reality.
- `restored / reversible`: the action and its restore path were both proven at their own layers.

SwitchBot remote-style のライトのように、押すたびに物理状態だけが反転し Home Assistant では
現在 state が読めない機器は `stateless_toggle` として扱います。その場合、`light_on` /
`light_off` という名前であっても HA state proof は主張せず、`external_observation` や
manual confirmation の proof layer に分けます。

## Current live-action classification policy

The current live setup should use conservative metadata until Home Assistant state
authority is proven:

| Action family | Current HA state class | Bridge metadata | Later candidate |
| --- | --- | --- | --- |
| light on/off | `switch:unknown` | `stateless_toggle`, `open_loop`, `external_observation` | External sensor/camera/manual proof only |
| fan on/off | `switch:unknown` | `stateless_command`, `submitted_only`, `command_ack_only` | External observation if needed |
| aircon on/off | `switch:unknown`; a same-device `climate` candidate exists | `stateless_command`, `submitted_only`, `command_ack_only` | `mode_command` only after the climate service path is designed and proven |
| door open/close | `cover:open` observed | `position_command`, `submitted_only`, `command_ack_only` | `ha_state` after execute/wait proof confirms stable open/closed state |
| door stop | transient cover command | `position_command`, `submitted_only`, `command_ack_only` | External/manual confirmation, not simple HA state proof |
| vacuum return | target cloud-side vacuum entity `docked`; separate local vacuum entity also exists | `job_command`, target-specific `ha_entity`, `ha_state` after local config promotion | Check only the script target; report retry if the first wait does not reach `docked` |
| vacuum start/pause | job state uncertain | `job_command`, `submitted_only`, `command_ack_only` | Accepted states and settle/timeout windows required first |

Define vacuum start and return criteria independently. Start-side proof must name
which states count as progress, such as `cleaning`, `returning`, or explicitly
`not docked`, and why that is acceptable for the ticket. Return-side proof should
normally require `docked`. Do not broaden `accepted_states` just to make a live
pilot green.
If return only reaches `docked` after an extra return command, record the retry
count. Tracking metadata can still make the post-state check explicit, but it
does not prove single-command return reliability.
The 2026-06-09 live pilot also showed why proof must be target-specific: the HA
setup exposed more than one `vacuum` entity, while the script targeted only one
redacted cloud-side entity. The other vacuum entity appears to come from a
separate local integration path and must stay out of bridge proof unless a script
targets it. All-domain `vacuum` counts were too broad. A local `expected_effect`
may track the script target and require `docked`, but the start/return transition
can still be asynchronous. If the first wait mismatches and a second return is
needed, report `restored / reversible with retry`; do not collapse that into a
single-command green proof.

Do not add `expected_effect` to `switch:unknown` actions just to make `CheckState`
green. Script entity state `off` only means the script is not running; it is not
appliance off-state.
