# マルチエージェント実装仕様書

**Version**: 1.0.0  
**作成日**: 2026-06-21（Day 2夜）  
**対象**: Day 3 マルチエージェント実装  
**前提**: 
- masked_pairs.jsonl 2,848件、辞書 v1.2.0（わたなべ実名）
- Firestore (pairs, acme_kb) 投入済み（Phase 2 朝方完了予定）
- Vector Search index/endpoint 構築済み（Phase 2）

---

## 1. 設計思想

### 1.1 マルチエージェントの本質的必然性

このシステムは「マルチ検索 + 統合」ではなく、「**独立した思考主体が並行推論し、結果を統合する**」設計を採る。これがハッカソン評価軸「AIエージェントが価値の中心になっているか」「マルチエージェントである必然性」に応える。

### 1.2 認知科学的基盤：System1 × System2

ノーベル経済学賞のダニエル・カーネマンが提示した、人間の意思決定の二重性：

| モード | 特性 | 例 |
|---|---|---|
| **System 1（直感）** | 速い、自動的、過去経験ベース、パターンマッチング | 「これ、過去のXX案件と同じだ。あの時はYYで判断した」 |
| **System 2（熟考）** | 遅い、努力的、論理的、構造化分析 | 「論点を整理しよう。規範はA、戦略はB、リスクはC...」 |

優れた意思決定者は両方を使い分け、両者の結論が一致すれば確信を持って判断、不一致なら理由を分析する。

**わたなべ部長クローン**は、この認知構造をそのままマルチエージェント設計に落とし込む。

### 1.3 単発LLMでは絶対に不可能な理由

1. **思考モードの同時並行**：1体のLLMに「直感と熟考の両方で考えて」と指示しても、結局どちらかに引っ張られる。独立エージェントとして並列実行することで両者の純度が保たれる
2. **モデル選定の最適化**：直感は Gemini Flash（速度）、熟考は Gemini Pro thinking（深さ）と使い分けられる
3. **デモでの可視化**：両エージェントの生の出力を別々に表示できる、思考プロセスが審査員に伝わる

---

## 2. 全体アーキテクチャ

```
┌─────────────────────────────────────────────────┐
│                User Interface                    │
│  （Slack Bot / Web UI / CLI）                    │
└─────────────────────┬───────────────────────────┘
                      │ user_query
                      ▼
┌─────────────────────────────────────────────────┐
│        Watanabe Boss Clone Coordinator           │
│        （ADK Sequential Coordinator）            │
└─────────────────────┬───────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │ Parallel Stage              │
        ▼             ▼               │
┌──────────────┐ ┌──────────────┐    │
│ System1      │ │ System2      │    │
│ Agent        │ │ Agent        │    │
│ （直感）     │ │ （熟考）     │    │
└──────┬───────┘ └──────┬───────┘    │
       │                │            │
       │ system1_output │ system2_output
       │                │            │
       └────────┬───────┘            │
                ▼                    │
       ┌──────────────────┐          │
       │  Synthesizer     │ ◀────────┘
       │  Agent           │
       │  （統合）        │
       └────────┬─────────┘
                │ final_response
                ▼
       ┌──────────────────┐
       │ Session History  │
       │ (Firestore)      │
       └──────────────────┘
```

---

## 3. エージェント定義

### 3.1 System1 Agent（直感）

**役割**：過去判断パターンから直感的・即決的に結論を出す

**入力**：
- `user_query`：ユーザーの相談内容
- （内部で）Vector Search（pair-summaries-v1）から類似ペア top-5 取得

**処理**：
1. user_query を embedding 化
2. pair-summaries-v1 で類似ペア取得（top-5、filter: tag in ["OK", "NG"]）
3. 取得ペアの human_text + assistant_text + tag を Gemini Flash に渡す
4. プロンプトで「直感的に即答せよ」と指示

**プロンプト骨格**：

```
あなたはわたなべ部長の「直感」を担当するエージェントです。

【ミッション】
過去の判断パターンから、相談内容に対する直感的な結論を即答してください。
- 分析は後回し、まず結論を出す
- 「同じパターンの過去判断があるか」を最優先で考える
- 1分以内に答えるイメージで

【ユーザーの相談】
{user_query}

【類似する過去の判断パターン（Vector Search top-5）】
{retrieved_pairs}

【出力形式】
- 直感的結論: [採用 / 却下 / 条件付き保留] のいずれか
- 過去パターンとの一致度: [高 / 中 / 低]
- 根拠となる過去ケース: 最も関連性の高い1〜2件を引用
- 直感的に気になる点: 1〜2点（理由はざっくりでよい）

簡潔に、150字以内で。
```

**モデル設定**：
- model: `gemini-2.5-flash`
- thinking_budget: 0（速度重視）
- max_output_tokens: 256
- temperature: 0.3

**期待レイテンシ**：3〜5秒（embed 含む、コールドスタート時は遅い）

---

### 3.2 System2 Agent（熟考）

**役割**：規範・戦略・暗黙知を網羅的に検討し、論理的な分析結論を出す

**入力**：
- `user_query`：ユーザーの相談内容
- （内部で）Vector Search（acme-kb-v1）から関連 KB 取得

**処理**：
1. user_query を embedding 化
2. acme-kb-v1 で関連 KB 取得：
   - L1（理念）top-3
   - L2（規程）top-3
   - L3（戦略）top-3
   - L4（暗黙知）top-3
3. 取得した KB を Gemini Pro thinking に渡す
4. プロンプトで「網羅的に検討せよ」と指示

**プロンプト骨格**：

```
あなたはわたなべ部長の「熟考」を担当するエージェントです。

【ミッション】
Acme Corp の理念・規程・戦略・暗黙知を網羅的に検討し、相談内容を論理的に分析してください。
- 論点を全て洗い出す
- トレードオフを明示する
- 不足情報があれば「確認すべき項目」として明示する

【ユーザーの相談】
{user_query}

【関連する Acme Corp 知識ベース】

理念・バリュー（L1）:
{retrieved_l1}

規程・コンプライアンス（L2）:
{retrieved_l2}

戦略・KPI（L3）:
{retrieved_l3}

暗黙知・組織文化（L4）:
{retrieved_l4}

【出力形式】

論点1: [タイトル]
- 関連する規範/戦略/暗黙知: [箇条書き]
- 検討すべきポイント: [構造化]
- トレードオフ: [もしあれば]

論点2: [タイトル]
（同様の構造）

論点3: [タイトル]
（同様の構造）

熟考的結論: [採用 / 却下 / 条件付き保留]
- 条件付きの場合、必要な条件を明示
- 確認すべき項目: [リスト]

500字以内で。論点は2〜4個に絞る。
```

**モデル設定**：
- model: `gemini-2.5-pro`
- thinking_budget: 4000（深く考えさせる）
- max_output_tokens: 2048
- temperature: 0.2

**期待レイテンシ**：8〜15秒（thinking のため）

---

### 3.3 Synthesizer Agent（統合判断）

**役割**：System1 と System2 の結論を統合し、わたなべ部長のスタイルで最終回答を生成

**入力**：
- `user_query`：ユーザーの相談内容
- `system1_output`：System1 Agent の出力
- `system2_output`：System2 Agent の出力

**処理**：
1. 両者の結論を比較
2. 一致／不一致を判定
3. わたなべ部長のスタイル（5特徴）で最終回答を生成

**プロンプト骨格**：

```
あなたはわたなべ部長です。営業企画部の部長として、部下からの相談に答えます。

【あなたの判断スタイル】
1. 構造化思考: 問題を分解してから判断する
2. トレードオフ明示: 「これを取ると、これを諦める」を必ず言語化
3. 論点先出し: 結論より、論点の整理を優先
4. 問い返し型: すぐ答えず、相談者の思考を深める質問を返す
5. MUST/WANT分離: 絶対必要なものと、あったらいいものを分ける

【部下の相談】
{user_query}

【あなたの「直感」の結論】
{system1_output}

【あなたの「熟考」の結論】
{system2_output}

【統合判断の方針】
- 直感と熟考が一致 → 確信を持って提案
- 直感と熟考が不一致 → 不一致の理由を分析し、どちらを優先するか判断
- 不一致時は「直感はXX、熟考はYY、私はZZと判断する。理由は...」と明示

【出力形式】

[最初に論点を3つ提示]

論点1: [タイトル]
- 状況: 直感と熟考の見立て
- 確認すべき質問: [具体的に]

論点2: [タイトル]
（同様）

論点3: [タイトル]
（同様）

[直感と熟考の整合性についてコメント]

[最後に締めの一言（わたなべ部長らしい簡潔さで）]

全体で400〜600字。わたなべ部長の口調（フランク、構造化、論点先出し）で。
```

**モデル設定**：
- model: `gemini-2.5-pro`
- thinking_budget: 2000
- max_output_tokens: 1500
- temperature: 0.4

**期待レイテンシ**：5〜10秒

---

### 3.4 Coordinator（オーケストレーション）

**役割**：3エージェントを ADK で統合し、並列・直列を制御

**ADK 実装**：

```python
from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

# System1 / System2 を並列実行
parallel_thinking = ParallelAgent(
    name="parallel_thinking",
    sub_agents=[system1_agent, system2_agent],
)

# Sequential: 並列実行 → 統合
boss_clone = SequentialAgent(
    name="watanabe_boss_clone",
    sub_agents=[parallel_thinking, synthesizer_agent],
)
```

**State 構造**：

```python
{
    "user_query": str,
    "session_id": str,
    "user_id": str,
    "system1_output": {
        "intuitive_conclusion": "採用 / 却下 / 条件付き保留",
        "match_confidence": "高 / 中 / 低",
        "reference_cases": [...],
        "concerns": [...],
        "raw_response": str,
    },
    "system2_output": {
        "thoughtful_conclusion": "...",
        "issues": [...],
        "trade_offs": [...],
        "verification_items": [...],
        "raw_response": str,
    },
    "final_response": {
        "issues_summary": [...],
        "questions": [...],
        "alignment_comment": str,
        "closing_remark": str,
        "raw_response": str,
    },
}
```

---

## 4. データ取得層（Vector Search ラッパー）

### 4.1 RetrievalService

各エージェントが呼び出す共通サービス：

```python
class RetrievalService:
    def __init__(self, embedder, vs_pair_index, vs_kb_index, firestore_client):
        self.embedder = embedder
        self.vs_pair = vs_pair_index
        self.vs_kb = vs_kb_index
        self.fs = firestore_client
        self._embed_cache = {}  # セッション内キャッシュ
    
    def get_similar_pairs(self, query: str, tag_filter=None, top_k=5):
        """過去判断ペアの取得（System1用）"""
        emb = self._embed_with_cache(query)
        results = self.vs_pair.find_neighbors(
            queries=[emb],
            num_neighbors=top_k,
            restricts=[{"namespace": "tag", "allow": tag_filter}] if tag_filter else None,
        )
        # Firestore で詳細取得
        return self._enrich_with_firestore(results, collection="pairs")
    
    def get_relevant_kb(self, query: str, layer_filter=None, top_k=3):
        """Acme KB の取得（System2用）"""
        emb = self._embed_with_cache(query)
        results = self.vs_kb.find_neighbors(
            queries=[emb],
            num_neighbors=top_k,
            restricts=[{"namespace": "layer", "allow": [layer_filter]}] if layer_filter else None,
        )
        return self._enrich_with_firestore(results, collection="acme_kb")
    
    def _embed_with_cache(self, query: str):
        if query not in self._embed_cache:
            self._embed_cache[query] = self.embedder.embed([query])[0]
        return self._embed_cache[query]
```

**重要設計**：
- セッション内 embedding キャッシュ（同じ user_query で複数回呼ばれてもembed一回）
- Firestore 詳細取得は Vector Search 結果の document_id で batch get

### 4.2 レイテンシ最適化

S7 で判明したレイテンシ課題への対策：

| 課題 | 対策 |
|---|---|
| embed コールドスタート 5-6秒 | client を long-lived 化、起動時にウォームアップ呼び出し |
| 1クエリで複数回 embed が走る | RetrievalService 内でキャッシュ |
| System1 / System2 が逐次実行で遅い | ParallelAgent で並列実行 |
| 全体レイテンシが累積する | timeout 設定で最大保証（例：60秒） |

**目標レイテンシ**：
- System1 単体：3〜5秒
- System2 単体：8〜15秒
- 並列実行：max(System1, System2) ≒ 8〜15秒
- Synthesizer：5〜10秒
- **合計：15〜25秒**

これは「即答」じゃないが、**人間が真剣に考える時間としては妥当**。デモでは「考えてる演出」も込みで成立する。

---

## 5. UI 実装

### 5.1 MVP：CLI モード

Day 3 では CLI で動かす。Day 4 以降で Web UI / Slack Bot を検討。

```bash
python scripts/boss_clone_chat.py
```

```
🤖 わたなべ部長クローン（CLI mode）

あなた> 来月、新機能リリースしたいです

[System1 思考中...] (4.2s)
[System2 思考中...] (12.5s)
[Synthesizer 統合中...] (6.8s)

────────────────────────────
🧠 System1（直感）の見立て:
直感的結論: 条件付き保留
過去パターンとの一致度: 中
根拠: kabe v3 リリース時の議論と類似
気になる点: セキュリティレビューが事前にされてるか不明

🤔 System2（熟考）の見立て:
論点1: KPI整合性
- 2026年度 ARR 50億円達成の道筋にこの機能はどう寄与？
論点2: ガバナンス
- セキュリティ規程上、新機能リリース前にレビュー必須
論点3: ROI
- 開発工数 vs 想定収益の試算は？

熟考的結論: 条件付き保留
確認すべき項目:
- KPI寄与の数値見積もり
- セキュリティレビュー実施計画
- 3年ROI試算

🎯 わたなべ部長として:
直感と熟考、両方が「条件付き保留」で一致しました。
来月リリースを目指す前に、以下3つだけ確認させてください。

論点1: KPI寄与
- この機能、データ活用ソリューションPoC 30社の達成に直結しますか？

論点2: セキュリティ
- 顧客データの取扱いは変わりますか？変わるならレビュー申請を

論点3: ROI試算
- 開発工数と想定収益の試算、3年累計シナリオが必要です

整ったら、すぐ進めましょう。
────────────────────────────

あなた> [次の相談 or 'exit']
```

### 5.2 デモ用：思考プロセスの可視化

ハッカソンデモで効くのは「**System1とSystem2が違うことを言ってる瞬間**」。これを CLI でも分けて表示することで、マルチエージェントの動作が可視化される。

### 5.3 Web UI（Day 4 以降検討）

時間あれば：
- Streamlit / Gradio で簡易 Web UI
- System1 / System2 / Synthesizer の出力を3カラム表示
- 「思考中」アニメーション
- セッション履歴ブラウズ

---

## 6. 実装ファイル構成

```
boss-clone/
├── scripts/
│   ├── boss_clone_chat.py            # CLI エントリポイント
│   ├── boss_clone_lib/
│   │   ├── __init__.py
│   │   ├── coordinator.py            # ADK Coordinator
│   │   ├── agents/
│   │   │   ├── system1_agent.py     # 直感エージェント
│   │   │   ├── system2_agent.py     # 熟考エージェント
│   │   │   └── synthesizer_agent.py # 統合エージェント
│   │   ├── retrieval/
│   │   │   ├── service.py            # RetrievalService
│   │   │   └── embed_cache.py        # embedding キャッシュ
│   │   ├── prompts/
│   │   │   ├── system1.py            # プロンプトテンプレート
│   │   │   ├── system2.py
│   │   │   └── synthesizer.py
│   │   └── session/
│   │       └── history.py            # Firestore session_history 書込
│   └── test_boss_clone.py            # 統合テスト
```

---

## 7. 実装段階

### Step 1: スタブ実装（30分）

各エージェントを「固定文字列を返す」スタブで実装、ADK の Coordinator が動くことを確認。
**目的**：ADKの動きを理解、配線が正しいか確認。

### Step 2: RetrievalService 実装（45分）

Vector Search + Firestore の連携、embedding キャッシュ実装。
単体テスト：embed → search → enrich の動作確認。

### Step 3: System1 実装 + 単体テスト（45分）

実際のプロンプトと Gemini Flash で動作確認。
サンプルクエリ3件で出力品質確認。

### Step 4: System2 実装 + 単体テスト（60分）

Gemini Pro thinking で動作確認。
サンプルクエリ3件で出力品質確認。

### Step 5: Synthesizer 実装 + 単体テスト（45分）

System1/System2 のモック出力を入れて統合判断の品質確認。

### Step 6: 統合動作確認（30分）

実際の Coordinator で end-to-end 動作。
レイテンシ計測。

### Step 7: CLI 実装（30分）

`boss_clone_chat.py` でインタラクティブ動作。
session_history への保存。

### Step 8: デモシナリオ整備（30分）

ハッカソン用デモシナリオを3つ用意：
- A: 直感と熟考が一致するケース
- B: 直感と熟考が不一致するケース（メインデモ）
- C: 複雑な戦略判断ケース

**合計推定時間：5〜6時間**

Day 3（6/22月）1日で完走可能なスコープ。

---

## 8. 評価基準

### 8.1 機能評価

| 項目 | 目標 |
|---|---|
| Coordinator が3エージェントを正しく起動 | ✓ |
| System1 が過去判断パターンを参照 | ✓ |
| System2 が Acme KB を網羅的に参照 | ✓ |
| Synthesizer が両者を統合 | ✓ |
| わたなべ部長スタイルの応答 | ✓ |
| 一致／不一致を明示的に扱う | ✓ |

### 8.2 性能評価

| 項目 | 目標 |
|---|---|
| 総レイテンシ | 30秒以内 |
| 並列実行が動作 | ✓ |
| Vector Search 検索品質 | distance > 0.6 |

### 8.3 デモインパクト

| 項目 | 評価軸 |
|---|---|
| System1 / System2 の出力が明確に異なる | デモで衝撃が出る |
| 不一致時の Synthesizer の説明 | 「マルチエージェントの必然性」が伝わる |
| わたなべ部長らしさが出る | 作者ブランドと整合 |

---

## 9. コスト試算

### 9.1 1リクエストあたり

| エージェント | モデル | 入力tokens | 出力tokens | コスト |
|---|---|---:|---:|---:|
| System1 | gemini-2.5-flash | 2,000 | 256 | $0.0007 |
| System2 | gemini-2.5-pro | 5,000 | 2,048 | $0.0095 |
| Synthesizer | gemini-2.5-pro | 3,000 | 1,500 | $0.0060 |
| Embedding | multilingual-002 | 100 | - | $0.0001 |
| **合計** | | | | **$0.016** |

### 9.2 ハッカソン期間総コスト

- 開発中の動作確認：300〜500リクエスト → $5〜$8
- デモ・審査トラフィック：1,000〜2,000リクエスト → $16〜$32
- **合計：$21〜$40**

Free Credit 残高で余裕で収まる。

---

## 10. リスクと対策

| リスク | 影響 | 対策 |
|---|---|---|
| ADK の理解不足で実装詰まる | 進捗ブロック | Step 1 のスタブ実装で早期検出 |
| Gemini レート制限（並列実行時） | エラー多発 | 並列度2に抑制、retry実装 |
| Vector Search のレイテンシ | UXの低下 | embedding キャッシュ、long-lived client |
| プロンプト調整が想定以上に時間取る | 完成が遅れる | Step 3-5 で品質確認、不足あれば Day 4 に持ち越し |
| 出張で6/24-26 触れない | スケジュール圧迫 | Day 3 でMVP完成、Day 4で磨き |

---

## 11. nfr-base 適合

- **A-1 環境変数**: .env から読み込み ✓
- **C-1 機密管理**: Vector Search に投入済みデータのみ参照 ✓
- **C-6 個人情報保護**: マスキング済みデータのみ参照 ✓
- **E-1 ログ**: session_history に全対話保存 ✓
- **E-2 出力検証**: 単体テスト + 統合テスト ✓
- **L-001 サロゲート**: 全スクリプト冒頭で reconfigure ✓
- **L-004 自己生成スクリプト**: test_*.py は permission rule で許可済 ✓

---

## 12. Day 3 開始時のチェックリスト

朝起きたらClaude Codeに以下を順に確認させる：

### 確認1: Phase 2 完走確認
- [ ] embedding_pipeline.py 完了報告
- [ ] Firestore pairs 2,848件投入確認
- [ ] Vector Search Phase 2 index/endpoint READY
- [ ] test_vector_search_smoke.py の品質確認
- [ ] コスト確認（Free Credit残高）

### 確認2: 仕様書理解
- [ ] 本仕様書（multi_agent_spec.md）を読む
- [ ] 不明点があれば質問

### 確認3: Day 3 着手準備
- [ ] boss_clone_lib/ ディレクトリ構造作成
- [ ] Step 1（スタブ実装）から着手

---

## 13. 次タスクへの引き継ぎ

### Day 4 への引き継ぎ事項

Day 3 で MVP（CLI動作）完成後、Day 4 でやること：

1. **デモ動画用シナリオ整備**
   - 3つのシナリオ（一致／不一致／複雑）
   - 各シナリオの想定セリフ、想定システム応答
   
2. **Web UI / Slack Bot 検討**
   - Streamlit で簡易 Web UI 作成
   - 思考プロセスの可視化

3. **プロンプトチューニング**
   - わたなべ部長らしさの精度向上
   - エッジケース対応

4. **README 整備**
   - 公開リポジトリとして見られる前提
   - セットアップ手順、デモ手順

### Day 5（出張明け）への引き継ぎ事項

- **デモ動画撮影・編集**（Proto Pedia 提出用）
- **アーキテクチャ図作成**（Proto Pedia 提出用）
- **ストーリー文章作成**（Proto Pedia の必須3パート）

---

## 14. 仕様書の自己評価

### 強み
- 「マルチエージェントの必然性」が認知科学的に説明可能
- System1/System2 の独立性が明確
- Synthesizer の役割（不一致時の分析）が機能要件として明示
- 段階的実装で Day 3 完走可能

### 残課題
- ADK での ParallelAgent の挙動を Step 1 で実証する必要
- レイテンシが30秒前後でデモテンポへの影響を Step 6 で確認
- プロンプト品質は Step 3-5 で実装しながら調整

---

**End of Multi-Agent Implementation Spec**
