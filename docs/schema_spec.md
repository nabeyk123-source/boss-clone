# Firestore + Vector Search スキーマ設計仕様

**Version**: 1.0.0  
**作成日**: 2026-06-21  
**対象**: Day 2 タスク4（データストア・検索基盤の設計）  
**前提**: masked_pairs.jsonl が 2,848 件で利用可能、辞書 v1.1.1、Acme Corp 仕様 v1.1.0

---

## 1. 目的

部長クローン Agent のマルチエージェント構成（5体）が、以下を実現するためのデータ基盤を設計する：

1. **過去判断の意味検索**：「似た状況での過去判断」をベクトル検索で即取得
2. **架空企業文脈の参照**：Acme Corp の 5 層データ（理念〜個人）を構造化保持
3. **判断パターンの統計分析**：タグ分布、トピック傾向、時系列変化を集計可能に
4. **マルチエージェント間のデータ共有**：各エージェントが同一データを参照、応答を統合

---

## 2. 全体構成

```
┌─────────────────────────────────────────────────┐
│                User Interface                    │
│  （Slack Bot / Web UI / CLI）                    │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│            Boss Clone Orchestrator               │
│  （ADK Coordinator Agent）                       │
└──┬──────────────────┬───────────────────────────┘
   │                  │
   │ 並列起動          │
   ▼                  ▼
┌──────────────┐  ┌──────────────────┐
│ 5 Sub-Agents │  │ Shared Resources │
└──────────────┘  └──────────────────┘
   │                  │
   │ 各エージェントが  │
   │ Shared Resources │
   │ を参照            │
   └──────┬───────────┘
          │
   ┌──────▼──────────────────────────────┐
   │   Firestore + Vector Search          │
   │                                       │
   │  ┌─────────────────────────────┐    │
   │  │ Vector Search (2 indexes)    │    │
   │  │  - pair_summaries            │    │
   │  │  - acme_kb_embeddings        │    │
   │  └─────────────────────────────┘    │
   │                                       │
   │  ┌─────────────────────────────┐    │
   │  │ Firestore (5 collections)    │    │
   │  │  - pairs                     │    │
   │  │  - conversations             │    │
   │  │  - acme_kb                   │    │
   │  │  - unknown_entities          │    │
   │  │  - session_history           │    │
   │  └─────────────────────────────┘    │
   └──────────────────────────────────────┘
```

---

## 3. Firestore コレクション設計

プロジェクト：`boss-clone-2026`  
データベース：`(default)` （Native モード、リージョン `us-central1`）

### 3.1 コレクション `pairs`（学習データ本体）

masked_pairs.jsonl の各ペアを 1 ドキュメントとして格納。

**ドキュメント ID 形式**：`{processing_id}_{conversation_uuid_short}_{pair_index}`  
例：`1abc8a6176581c1c_50d79dc6_0005`

**フィールド構造**：

```typescript
{
  // === Identity ===
  processing_id: string,           // "1abc8a6176581c1c_12c3e9ea25006ab5"
  conversation_uuid: string,        // 元対話UUID
  pair_index: number,               // 対話内のペア番号

  // === Content (masked) ===
  human_text: string,               // マスキング済み人間発言
  assistant_text: string,           // マスキング済みAI応答
  summary: string,                  // 1〜2文の要約（Geminiで生成）

  // === Classification ===
  tag: "OK" | "NG" | "保留",        // Day 2 タスク3で付与済み
  topic_tags: string[],             // ["kabe", "design", "k=3"] 等
  decision_type: "採用" | "却下" | "保留" | "方向転換" | "情報要求",

  // === Time ===
  created_at: Timestamp,            // 元対話の created_at
  ingested_at: Timestamp,           // Firestore への投入時刻

  // === Metadata ===
  had_pii: boolean,
  pii_types: string[],              // ["email", "phone", "card"]
  dictionary_categories: string[],  // ["company", "executives", "user_identity"]
  dictionary_replacement_count: number,

  // === Vector Search 連携 ===
  vector_id: string,                // Vector Search の datapoint ID
  embedding_model: string,          // "textembedding-gecko-multilingual-002"

  // === Status ===
  source: "masked_pairs" | "review_queue",
  needs_review: boolean,
  retry_exhausted: boolean,         // タグ判定が失敗した場合 true
}
```

**インデックス**（Firestore Composite Index）：

```
1. (tag, created_at desc) — タグ別の時系列検索
2. (topic_tags array-contains, tag, created_at desc) — トピック横断検索
3. (decision_type, created_at desc) — 判断種別検索
4. (needs_review, source) — レビュー管理
```

### 3.2 コレクション `conversations`（対話メタデータ）

**ドキュメント ID**：conversation_uuid

```typescript
{
  uuid: string,
  title: string,                    // マスキング済みタイトル
  created_at: Timestamp,
  pair_count: number,               // この対話のペア数
  pair_index_range: [number, number],  // [0, 29] 等

  // === 集計値（事後計算） ===
  tag_distribution: {
    ok: number,
    ng: number,
    hold: number,
  },
  topic_summary: string[],          // この対話の主要トピック（自動抽出）
  importance_score: number,         // 重要度（0.0-1.0）

  // === 関連 ===
  has_blacklist_hit: boolean,
  excluded: boolean,
}
```

### 3.3 コレクション `acme_kb`（Acme Corp 知識ベース）

5 層データを構造化保持。`acme_corp_spec.md` の内容をプログラム可読な形に正規化。

**ドキュメント ID 形式**：`{layer}_{category}_{key}`  
例：`L1_value_customer_first`, `L4_decision_pattern_morita_ceo`

```typescript
{
  layer: "L1_principles" | "L2_regulations" | "L3_strategy" 
       | "L4_implicit_knowledge" | "L5_personal_judgment",
  category: string,                 // "values", "rules", "kpi" 等
  key: string,                      // 「forgable」な識別子
  
  title: string,                    // 表示用タイトル
  content: string,                  // 本文
  
  // === セマンティック検索用 ===
  embedding_text: string,           // 埋め込み生成用に整形したテキスト
  vector_id: string,                // Vector Search ID
  
  // === 関連付け ===
  references: string[],             // 関連する他の kb_id
  applies_to: string[],             // "all" | "executive_decisions" 等
  
  // === メタ ===
  version: string,                  // "1.0.0"
  last_updated: Timestamp,
  active: boolean,
}
```

### 3.4 コレクション `unknown_entities`（未知固有名詞レビュー）

**ドキュメント ID**：term をハッシュ化（`hash(term)[:16]`）

```typescript
{
  term: string,                     // "雜賀さん"
  count: number,                    // 検出回数
  type: "honorific" | "title" | "company_jp" | "company_en" | "name_with_particle",
  sample_contexts: string[],        // 上位5件のコンテキスト
  
  decision: "pending" | "added_to_dict" | "ignore" | "false_positive",
  decision_at: Timestamp | null,
  decision_note: string | null,
  
  conversation_uuids: string[],     // 出現対話一覧
  first_seen: Timestamp,
  last_seen: Timestamp,
}
```

### 3.5 コレクション `session_history`（対話セッション履歴）

部長クローンとユーザーの対話履歴を保持。マルチターン対応 + Lessons 蓄積のため。

**ドキュメント ID**：`session_{uuid}_{turn_index}`

```typescript
{
  session_uuid: string,
  user_id: string,                  // 認証ユーザー識別子
  turn_index: number,
  
  user_message: string,
  agent_response: string,
  
  // === 使用したリソース ===
  referenced_pairs: string[],       // pairs のドキュメント ID
  referenced_acme_kb: string[],     // acme_kb のドキュメント ID
  
  // === エージェント情報 ===
  triggered_agents: string[],       // 起動したエージェント名
  agent_traces: object,             // 各エージェントの中間出力（デバッグ用）
  
  created_at: Timestamp,
  mode: "dialogue" | "document_review",
}
```

---

## 4. Vector Search インデックス設計

### 4.1 インデックス1：`pair_summaries`

**用途**：過去の判断ペアをセマンティック検索  
**入力**：各ペアの `summary` フィールド

| 項目 | 値 |
|---|---|
| インデックスID | `pair-summaries-v1` |
| 埋め込みモデル | `text-multilingual-embedding-002` |
| 次元 | 768 |
| 距離指標 | DOT_PRODUCT_DISTANCE |
| アルゴリズム | TREE_AH（中規模データ向け） |
| シャード数 | 1（2,848件なら十分） |
| 更新方式 | BATCH（ハッカソン期間中は再構築型） |

**メタデータフィルタ**（Vector Search 側で持つ）：
- `tag`: "OK" / "NG" / "保留"
- `topic_tags`: array
- `decision_type`: string
- `created_at_year_month`: "2026-04" 等（時期フィルタ用）

### 4.2 インデックス2：`acme_kb_embeddings`

**用途**：Acme Corp の規範・戦略・暗黙知を意味検索  
**入力**：`acme_kb` の `embedding_text`

| 項目 | 値 |
|---|---|
| インデックスID | `acme-kb-v1` |
| 埋め込みモデル | `text-multilingual-embedding-002` |
| 次元 | 768 |
| 距離指標 | DOT_PRODUCT_DISTANCE |
| アルゴリズム | TREE_AH |
| シャード数 | 1 |
| 更新方式 | BATCH |

**メタデータフィルタ**：
- `layer`: L1〜L5
- `category`: string
- `applies_to`: array

### 4.3 埋め込みモデルの選定理由

`text-multilingual-embedding-002` を選んだ理由：

1. **日本語性能**：multilingual-002 は日本語タスクで高評価
2. **次元数 768**：精度と速度のバランス良好
3. **コスト**：1M入力トークン $0.025、安価
4. **Vertex AI ネイティブ**：API 連携が楽

英語専用の `text-embedding-3` は不採用。日本語混じりデータでは性能落ちる。

---

## 5. データ投入パイプライン

### 5.1 全体フロー

```
[Step 1] masked_pairs.jsonl 読込（2,848件）
   ↓
[Step 2] 要約生成（summary フィールド作成）
   ├ Gemini 2.5 Flash で 1〜2 文要約
   ├ 並列8、約 5〜7 分
   └ 推定コスト: $0.50
   ↓
[Step 3] トピック分類（topic_tags 付与）
   ├ ルールベース優先（プロジェクト名キーワード）
   ├ 漏れたものを Gemini で分類
   └ 推定コスト: $0.20
   ↓
[Step 4] 判断種別分類（decision_type 付与）
   ├ Gemini 2.5 Flash で 5 値分類
   ├ 推定コスト: $0.30
   ↓
[Step 5] 埋め込み生成
   ├ text-multilingual-embedding-002
   ├ summary を入力
   └ 推定コスト: $0.05
   ↓
[Step 6] Firestore へ投入
   ├ batch_writer で 500 件ずつ
   └ 約 2 分
   ↓
[Step 7] Vector Search インデックス構築
   ├ 埋め込み + メタデータを Cloud Storage にアップロード
   ├ Vector Search でインデックス作成（10〜15 分）
   └ デプロイ（5 分）
   ↓
[Step 8] 動作確認（最小ロード実験）
   ├ サンプルクエリ 5 件で検索
   └ 類似度スコアと取得ペアの品質確認
```

### 5.2 Acme KB の投入

`acme_corp_spec.md` を構造化スクリプトで `acme_kb` コレクションに分解投入。

```python
def parse_acme_spec(md_path: str) -> list[dict]:
    """
    acme_corp_spec.md を読み、5層×N項目のドキュメントに分解。
    各セクション（### 6.1 層1: 理念・ビジョン層 等）を識別し、
    その配下の見出しをドキュメント化。
    """
    # 実装は manual mapping が無難（自動パースは脆い）
```

推定 30〜50 ドキュメント生成。埋め込み生成コスト $0.01 以下。

---

## 6. クエリパターン設計

マルチエージェント運用での典型クエリ。

### 6.1 対話モード：「似た過去判断を検索」

```python
# Step 1: ユーザー入力を埋め込み化
query_embedding = embed_text(user_message)

# Step 2: Vector Search で類似 summary を検索（k=10）
similar_pairs = vector_search.match(
    index="pair-summaries-v1",
    query_embedding=query_embedding,
    num_neighbors=10,
    filters={
        "tag": ["OK", "NG"],  # 「保留」は除外
    },
)

# Step 3: Firestore で詳細取得
pair_details = firestore.collection("pairs").where(
    "__name__", "in", [p.id for p in similar_pairs]
).get()

# Step 4: 関連する Acme KB も検索
acme_relevant = vector_search.match(
    index="acme-kb-v1",
    query_embedding=query_embedding,
    num_neighbors=5,
)
```

### 6.2 資料モード：「資料に対する指摘パターンを検索」

```python
# Step 1: 資料テキストを分割（長文の場合）
doc_chunks = split_document(uploaded_doc)

# Step 2: 各チャンクで類似指摘を検索
review_patterns = []
for chunk in doc_chunks:
    chunk_emb = embed_text(chunk)
    similar = vector_search.match(
        index="pair-summaries-v1",
        query_embedding=chunk_emb,
        num_neighbors=5,
        filters={"decision_type": "却下", "tag": "NG"},
    )
    review_patterns.extend(similar)

# Step 3: 規範チェック（acme_kb の L2 規程層）
regulatory_check = vector_search.match(
    index="acme-kb-v1",
    query_embedding=embed_text(uploaded_doc),
    num_neighbors=10,
    filters={"layer": "L2_regulations"},
)
```

### 6.3 暗黙知参照：「経営層の判断パターン」

```python
# 「森田社長ならどう判断するか」型クエリ
exec_patterns = firestore.collection("acme_kb").where(
    "layer", "==", "L4_implicit_knowledge"
).where(
    "category", "==", "decision_pattern"
).where(
    "key", ">=", "morita_"
).where(
    "key", "<", "morita_z"
).get()
```

---

## 7. コスト試算

### 7.1 初期構築（一度きり）

| 項目 | 推定コスト |
|---|---:|
| 要約生成（2,848件 × Gemini Flash） | $0.50 |
| トピック分類（500件 × Gemini Flash） | $0.20 |
| 判断種別分類（2,848件 × Gemini Flash） | $0.30 |
| ペア埋め込み生成（2,848件） | $0.05 |
| Acme KB 埋め込み（50件） | $0.01 |
| Firestore 投入 | $0.00（無料枠内） |
| Vector Search インデックス構築 | $0.00（構築自体は無料） |
| **合計** | **$1.06** |

### 7.2 維持コスト（月額）

| 項目 | 月額 |
|---|---:|
| Vector Search インデックス維持（10MB × 2） | $30〜$50 |
| Firestore ストレージ（〜100MB） | $0.18 |
| Firestore 読み書き（無料枠内想定） | $0.00 |
| **合計** | **$30〜$50** |

### 7.3 クエリコスト（1リクエストあたり）

| 項目 | 単価 |
|---|---:|
| 埋め込み生成（クエリ） | $0.0001 |
| Vector Search マッチ | $0.0010 |
| Firestore 読み込み（10件） | $0.00006 |
| 合計 | **$0.0011** |

ハッカソン期間中のデモ・審査トラフィック（推定 1,000 リクエスト）：**$1.10**

### 7.4 ハッカソン期間総コスト見積もり

- 初期構築：$1.06
- 維持（6/21 〜 7/30、約40日）：$40〜$67
- クエリ：$1.10

**合計：$42〜$69**

Free Credit ¥47,966（約 $320）の範囲内で完全に収まる。

---

## 8. 実装方針

### 8.1 実装ファイル構成

```
boss-clone/
├── scripts/
│   ├── embedding_pipeline.py       # 要約 + 埋め込み + 投入の全工程
│   ├── embedding_lib/
│   │   ├── __init__.py
│   │   ├── summarizer.py           # 要約生成
│   │   ├── topic_classifier.py     # トピック分類
│   │   ├── decision_classifier.py  # 判断種別分類
│   │   ├── embedder.py             # 埋め込み生成
│   │   ├── firestore_writer.py     # Firestore 投入
│   │   └── vector_search_writer.py # Vector Search 投入
│   ├── acme_kb_loader.py           # Acme KB 構造化投入
│   └── test_*.py                   # 各種テスト
├── docs/
│   └── schema_spec.md              # 本ドキュメント
```

### 8.2 段階的実装

```
段階1: 設計凍結（本ドキュメント承認）
段階2: 小規模試験（10件で end-to-end）
  ├ embedding_pipeline.py を --test --sample=10 で実行
  ├ Firestore に 10件投入
  ├ Vector Search インデックス作成（小規模）
  └ 検索動作確認
段階3: コスト試算検証
  ├ 段階2の実コストから本処理コストを再見積もり
段階4: 本処理（2,848件）
  └ フル実行
段階5: 検索品質確認
  └ サンプルクエリ 10 件で類似度と取得品質確認
```

### 8.3 nfr-base v2.1.0 適合

- **A-1 環境変数**: .env から読み込み、ハードコード禁止 ✓
- **C-1 機密管理**: pii_strategy.md に従う ✓
- **C-6 個人情報保護**: マスキング済みデータのみ投入 ✓
- **E-1 ログ**: 投入・検索の audit log 保持 ✓
- **E-2 出力検証**: 投入後の件数・スキーマ検証必須 ✓

---

## 9. リスクと対策

| リスク | 影響 | 対策 |
|---|---|---|
| Vector Search の構築失敗 | 進行停止 | 段階2の試験で早期検出 |
| 埋め込み精度の不足 | 検索品質低下 | 段階5で品質確認、必要なら別モデル試行 |
| Firestore 投入失敗 | 部分データ欠損 | batch_writer + retry、audit log で追跡 |
| コスト超過 | Free Credit 圧迫 | 予算アラート（既設定）+ 段階的実行 |
| Vector Search デプロイ時間 | 開発ブロック | 並行作業可能なタスクを準備 |

---

## 10. 次タスクへの引き継ぎ

### Day 2 タスク5（最小ロード実験）への引き継ぎ事項

1. masked_pairs.jsonl から 10 件をサンプル抽出
2. 段階2の実装で end-to-end 動作確認
3. 検索動作で類似度スコア・取得品質を評価
4. 問題なければフル処理（タスク6）へ

### Day 3 タスク（マルチエージェント実装）への引き継ぎ事項

本スキーマが提供する：
- `pairs.summary` の意味検索 API
- `acme_kb` の構造化参照 API
- `session_history` の対話履歴管理 API

これらを 5 体のエージェントが利用してマルチエージェント協調を実現。

---

**End of Schema Specification**
