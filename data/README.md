# data/

学習・推論に使うデータの置き場。**配下のデータ本体は `.gitignore` で git 管理外**。

| ディレクトリ | 用途 | git管理 |
|---|---|---|
| `data/raw/claude_export/` | Claude の対話エクスポート ZIP / JSON をそのまま置く | × |
| `data/interim/` | 前処理途中の中間成果物 | × |
| `data/processed/` | 学習・検索に使う最終形（チャンク済み、埋め込み済み等） | × |

`.gitkeep` だけ commit してディレクトリ存在を担保している。本体ファイルは `git status` に出ても add しないこと。

## Claude エクスポートの置き方

1. Claude の設定画面から会話履歴をエクスポート（ZIP）
2. `data/raw/claude_export/` 配下に展開
3. `git status` で誤って tracked になっていないことを確認
