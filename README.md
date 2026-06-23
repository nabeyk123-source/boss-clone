# Boss Clone Agent — わたなべ部長クローン

**DevOps × AI Agent Hackathon 2026 参加作品。**

過去の Claude 対話ログを学習データとして、わたなべ部長の判断パターンを再現する **マルチエージェント** を構築。
論点整理・前提質問・優先度判断・MUST/WANT 分離といった「考え方の OS」を AI で再現することが目標。

- **🌐 公開デモ**: https://boss-clone-web-710145835677.us-central1.run.app
- **題材**: 上司の判断OS（部長クローン）
- **提出期限**: 2026-07-10
- **ライセンス**: [MIT](LICENSE)

---

## マルチエージェントの必然性

ノーベル経済学賞ダニエル・カーネマンの **System 1（直感）× System 2（熟考）** モデルをそのままアーキテクチャに落とし込む。

```
                ┌─────────────────────────┐
ユーザー入力 →  │  Watanabe Boss Clone     │
                │  (ADK Coordinator)        │
                └────────────┬──────────────┘
                             │
                  ┌──────────┴──────────┐
                  ▼                      ▼
         ┌─────────────────┐    ┌─────────────────┐
         │ System1 (直感)  │    │ System2 (熟考)  │
         │ Flash, 即答     │    │ Pro thinking    │
         │ 過去判断パターン │    │ Acme KB 網羅    │
         └────────┬────────┘    └────────┬────────┘
                  │ 質問生成              │ 論点抽出
                  └──────────┬────────────┘
                             ▼
                    ┌─────────────────┐
                    │ Synthesizer     │
                    │ Pro thinking    │
                    │ 統合判断        │
                    └────────┬────────┘
                             ▼
                    わたなべ部長として 1 人の判断
```

3 体のエージェントが **独立に思考し、結果を統合する**。
直感と熟考の結論が割れた瞬間が、マルチエージェントの最大の価値。

---

## マルチターン質問機能 — 「待ち時間を消す」設計

```
Turn 1 (8〜15秒):
  あなた> 来月、新機能リリースしたいです
  🤖 System1 が即質問: 「3つ確認させて...」
      ↑ System2 はバックグラウンドで並列実行中（17秒）

Turn 2 (16〜24秒):
  あなた> 質問への回答を入力
  🤖 Synthesizer が統合: わたなべ部長として最終判断
      ↑ System2 はユーザー回答中に完走済み
```

**体感待ち時間 ≒ Turn 1 + Turn 2 = 25〜37 秒**
（ユーザー回答時間は「待ち」じゃない）

単発 LLM が 60 秒沈黙する設計とは、対話のリズムが根本的に違う。

---

## 技術スタック

| レイヤ | 採用技術 |
|---|---|
| 言語 / フレームワーク | Python 3.14 / [Google ADK](https://google.github.io/adk-docs/) 2.3.0 |
| LLM | Gemini 2.5 Flash / Pro（Vertex AI 経由、Free Credit 対象） |
| Embedding | text-multilingual-embedding-002（768次元、L2正規化済） |
| ベクター検索 | **Firestore + numpy（アプリ内コサイン類似度）**（Day 4 で Vector Search から移行、L-009） |
| データストア | Firestore（pairs 2787件、acme_kb 46件、embedding 同梱） |
| デプロイ | Cloud Run（Day 4 以降） |

---

## セットアップ

### 1. 前提

- Python 3.11+
- GCP プロジェクト + `gcloud` CLI 認証済み
- Vertex AI / Firestore API 有効化

### 2. 仮想環境と依存

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install google-adk google-cloud-aiplatform google-cloud-firestore google-cloud-storage python-dotenv
```

### 3. GCP 認証

```powershell
gcloud auth application-default login
gcloud config set project <YOUR_GCP_PROJECT_ID>
```

### 4. 環境変数

リポジトリルートに `.env` を **UTF-8（BOMなし）** で作成する（PowerShell の `echo > .env` は UTF-16 で書くので使わない、[lessons.md L-001](lessons.md)）。

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<YOUR_GCP_PROJECT_ID>
GOOGLE_CLOUD_LOCATION=us-central1
```

### 5. データ準備（任意、学習データは非公開）

`data/raw/claude_export/` 配下に Claude エクスポートを置き、以下を順に実行：

```powershell
python scripts/masking_pipeline.py --force          # PII/機密マスキング
python scripts/embedding_pipeline.py --force        # 要約・分類・埋め込み・Firestore投入
python scripts/test_vs_setup.py --setup --source pair_summaries  # Vector Search 構築
python scripts/acme_kb_loader.py                    # Acme KB 投入
```

### 6. マルチターン CLI 起動

```powershell
python scripts/boss_clone_chat.py
```

### 7. ローカルで Streamlit Web UI 起動

```powershell
.\.venv\Scripts\streamlit.exe run scripts\boss_clone_web.py
```

→ `http://localhost:8501` でブラウザが開きます。

### 8. Cloud Run へのデプロイ

```bash
gcloud run deploy boss-clone-web \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi --cpu 1 \
  --min-instances 0 --max-instances 3 \
  --timeout 300 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=...,VS_PAIR_INDEX_RESOURCE=...,VS_PAIR_ENDPOINT_RESOURCE=...,VS_PAIR_DEPLOYED_ID=pair_summaries_v1,..."
```

[Dockerfile](Dockerfile) と [.dockerignore](.dockerignore) を同梱。Cloud Build で自動ビルドされます。
必須環境変数は `Dockerfile` のコメント参照。

---

## 実行例

```
🤖 わたなべ部長クローン（マルチターン CLI）
[init] embedding warmup... OK

あなた> 来月、kabe-anon の新機能をリリースしたいです

🤖 わたなべ部長クローンが考え中…

────── 🧠 System1 の直感 ──────
直感的結論: 採用（一致度: 高）
気になる点: リリース時期の妥当性 / 新機能の内容

────── 🙋 確認させてください ──────
  1. リリースする新機能の具体的な名称と、解決する課題は何ですか？
  2. リリース後の目標 KPI は何ですか？

  → 質問1への回答: kabe v3 の新規ダッシュボード、ユーザー数15%増
  → 質問2への回答: リリース日は来月25日

🤖 統合中…

────── 🎯 わたなべ部長として ──────
OK、kabe-anon の新機能リリースの件、少し整理しようか。
やりたい気持ちはよく分かった。方向性としては面白そうだし、
前に進めたい。ただ、いくつか論点があるから、一度立ち止まって考えたい。

論点1: なぜ「ユーザー数15%増」につながるのか？
- 状況: 新しいダッシュボードでユーザー体験が良くなるのは分かる...
- 確認すべき質問: この新機能によって、具体的に誰のどんな行動が変わり...

（論点2, 3 略）

まずはこの3点を整理して、もう一度話そうか。期待してるよ。
```

---

## ドキュメント

| ファイル | 用途 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code / クロが作業時に参照する運用文書 |
| [lessons.md](lessons.md) | このプロジェクトで踏んだ罠と対処ログ（L-001〜） |
| [docs/acme_corp_spec.md](docs/acme_corp_spec.md) | 公開デモ用の架空企業設定（5層データ） |
| [docs/pii_strategy.md](docs/pii_strategy.md) | PII / 業務機密の多層防御戦略 |
| [docs/schema_spec.md](docs/schema_spec.md) | Firestore + Vector Search スキーマ |
| [docs/multi_agent_spec.md](docs/multi_agent_spec.md) | マルチエージェント実装仕様 |
| [docs/demo_scenarios.md](docs/demo_scenarios.md) | ハッカソンデモシナリオ 3 パターン |
| [docs/day3_summary.md](docs/day3_summary.md) | Day 3 成果まとめ |

---

## 進捗

### Day 1 (2026-06-20) — Hello Agent + プロジェクト初期化

- ADK + Gemini 2.5 Flash + Vertex AI で日本語対話エージェント
- GitHub 公開リポジトリ作成、開発標準（nfr-base.md）の適用判断

### Day 2 (2026-06-21〜22) — データ基盤構築

- Claude エクスポート 76対話 / 6,808 メッセージを分析
- PII / 機密情報のマスキングパイプライン（辞書 v1.2.0、3,371 ペア処理、429 ストーム対策、チャンク化＋チェックポイント機構）
- Firestore（pairs 2787、acme_kb 46）+ Vector Search（pair_summaries_v1）構築
- マスキング再処理 + フル埋め込み（チェックポイント機構が PC スリープ復帰に救命浮輪として機能）

### Day 3 (2026-06-22 夜〜23 早朝) — マルチエージェント実装

- ADK ParallelAgent + SequentialAgent で 3 エージェント配線
- RetrievalService（embedding cache 含む）
- System1（直感 / Flash）+ System2（熟考 / Pro thinking）+ Synthesizer（統合 / Pro thinking）
- **マルチターン質問機能**: System1 即質問 + System2 バックグラウンド並列実行
- Synthesizer 出力品質改善（「直感」「熟考」「エージェント」など内部用語の完全排除）
- デモシナリオ 3 パターン作成（[docs/demo_scenarios.md](docs/demo_scenarios.md)）

### 次のステップ（Day 4 以降、出張明け）

- TODO-P8: System1 質問の選択肢化（フリー回答 → 選択肢 2-4 個）
- Web UI / Streamlit（3 カラム表示で思考プロセス可視化）
- Cloud Run デプロイ
- デモ動画撮影、Proto Pedia 提出

詳細は [docs/day3_summary.md](docs/day3_summary.md) を参照。

---

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照。
