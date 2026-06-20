# Boss Clone Agent

**DevOps × AI Agent Hackathon 2026 参加作品。**

過去の Claude 対話ログを学習データとして、上司（わたなべ部長）の判断パターンを再現するエージェント「クロ」を構築する。
論点整理・前提質問・優先度判断といった、わたなべが普段見せている "考え方の OS" を AI で再現することが目標。

- **題材**: 上司の判断OS（部長クローン）
- **提出期限**: 2026-07-10
- **ライセンス**: [MIT](LICENSE)

---

## 技術スタック

| レイヤ | 採用技術 |
|---|---|
| 言語 / フレームワーク | Python 3.14 / [Google ADK](https://google.github.io/adk-docs/) 2.3.0 |
| LLM | Gemini 2.5 Flash / Pro（Vertex AI 経由、Free Credit 対象） |
| デプロイ | Cloud Run |
| データストア | Firestore |
| ベクター検索 | Vertex AI Vector Search |

---

## セットアップ

### 1. 前提

- Python 3.11+
- GCP プロジェクト + `gcloud` CLI 認証済み
- Vertex AI API が有効化されていること

### 2. 仮想環境と依存

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install google-adk google-cloud-aiplatform python-dotenv
```

### 3. GCP 認証

```powershell
gcloud auth application-default login
gcloud config set project <YOUR_GCP_PROJECT_ID>
```

### 4. 環境変数

リポジトリルートに `.env` を **UTF-8（BOMなし）** で作成する。
PowerShell の `echo > .env` は UTF-16 で書くので使わないこと（`Set-Content -Encoding utf8` か Python から書く）。

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<YOUR_GCP_PROJECT_ID>
GOOGLE_CLOUD_LOCATION=us-central1
```

---

## 実行

```powershell
python hello_agent.py
```

「クロ」がわたなべ部長の壁打ち相手として日本語で応答する。`:q` で終了。

```
クロ: こんにちは、わたなべさん。今日は何を一緒に整理しましょう？
あなた> 今日は新機能のリリース判断で迷ってる…
クロ> 品質チームが求める「あと一週間」は、具体的に何のリスクを解消するためですか…
```

---

## ドキュメント

| ファイル | 用途 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code / クロが作業時に参照する運用文書（適用判断・チェックリスト・参照先） |
| [lessons.md](lessons.md) | このプロジェクトで踏んだ罠と対処ログ（L-001〜） |

---

## 進捗

### Day 1 (2026-06-20) — Hello, Agent + プロジェクト初期化

- [x] `hello_agent.py` 実装（ADK Agent + InMemoryRunner + 対話ループ）
- [x] Vertex AI 経由で Gemini 2.5 Flash が日本語応答することを確認
- [x] Windows コンソール (cp932) で日本語入力が `\udcXX` サロゲート化する問題に対処（[lessons.md L-001](lessons.md)）
- [x] 開発標準（`_standards/nfr-base.md`）の適用判断を [CLAUDE.md](CLAUDE.md) に整理
- [x] `.gitignore` で `.env` / 学習データを保護
- [x] GitHub リポジトリ作成 + 初期 push

### 次のステップ

- [ ] 過去の Claude 対話ログ（エクスポート ZIP）を `data/raw/claude_export/` へ取り込む
- [ ] Firestore に対話ログを格納するスキーマ設計
- [ ] Vertex AI Vector Search でセマンティック検索の最小構成
- [ ] 検索結果を踏まえて「クロ」のプロンプトを動的に組み立てる RAG 化
- [ ] Cloud Run へのデプロイと A-5 実環境スモーク

---

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
