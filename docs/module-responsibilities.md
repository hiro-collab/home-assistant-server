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

`expected_effect` は「この操作で期待する状態」を後続サービスへ伝えるためのメタデータです。実際に Home Assistant へ送る命令を変えるものではなく、実状態を証明するラベルでもありません。
