# 通常導線外の情報

このファイルは README から外した情報の短い索引です。統合仕様の正本ではありません。復活させる場合は、実装、OpenAPI、統合リポジトリ側の契約文書を確認してから本文化してください。

## 2026-05-07 README 剪定

- 長い `curl.exe` 実行例は README から外しました。API の正確な形は [dify-openapi.yaml](dify-openapi.yaml) または FastAPI の `/openapi.json` を参照してください。
- Dify HTTP Request node の手順例は README から外しました。統合側には「認証ヘッダー、`request_id`、確認トークン、`execution_id` を保持する」契約だけを置く想定です。
- fault injection の scenario 一覧と match 条件の細部は README から外しました。これはローカル E2E 検証用で、本番統合の通常導線には含めません。
- UDP JSON payload の詳細サンプルは README から外しました。UDP は演出連携用の任意通知で、家電操作の成否判定の正本にはしません。
- `ActionResponse` の長い JSON 例は README から外しました。レスポンス契約は schema と OpenAPI を優先してください。

## archive

- [archive/README-before-pruning-2026-05-07.md](archive/README-before-pruning-2026-05-07.md): 剪定前 README の退避コピー。履歴確認用であり、仕様の正本として参照しないでください。
