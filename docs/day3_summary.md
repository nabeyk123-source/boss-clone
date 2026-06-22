# Day 3 成果サマリ

**日付**: 2026-06-22 (Day 2 夜) 〜 2026-06-23 (Day 3 早朝)
**フェーズ**: マルチエージェント実装 + マルチターン化 + デモシナリオ整備

---

## 実装完了項目

### マルチエージェント設計（仕様書 §3）

| エージェント | モデル | thinking_budget | レイテンシ目標 | 実測 | 状態 |
|---|---|---:|---:|---:|---|
| System1（直感） | gemini-2.5-flash | 0 | 3-5 秒 | 3-15 秒 | ✅ |
| System2（熟考） | gemini-2.5-pro | 1000 | 8-15 秒 | 17-21 秒 | ✅ |
| Synthesizer（統合） | gemini-2.5-pro | 1000 | 5-10 秒 | 16-24 秒 | ✅ |

### ADK Coordinator 配線（Step 1）

- ParallelAgent + SequentialAgent で 3 エージェントを統合（[scripts/boss_clone_lib/coordinator.py](../scripts/boss_clone_lib/coordinator.py)）
- スタブ実装（BaseAgent 派生で固定文字列を返す）で配線を最初に検証 → LLM 呼び出しゼロでフロー確認
- state_delta による中間結果の伝播も実証

### RetrievalService（Step 2）

[scripts/boss_clone_lib/retrieval/service.py](../scripts/boss_clone_lib/retrieval/service.py)

- Vector Search（pair_summaries_v1）+ Firestore（pairs / acme_kb）の連携
- **embedding キャッシュ**：同じクエリで何回呼ばれても embed 1 回（cold start 4-6秒 → cached 100-500ms）
- KB 取得は Vector Search → Firestore layer-filter fallback の 2 段構え（acme_kb endpoint 未デプロイでも動作）

### System1 / System2 / Synthesizer の本実装（Step 3-5）

各エージェントが
- RetrievalService を介してデータ取得
- プロンプト構築（[prompts/system1.py](../scripts/boss_clone_lib/prompts/system1.py) / [system2.py](../scripts/boss_clone_lib/prompts/system2.py) / [synthesizer.py](../scripts/boss_clone_lib/prompts/synthesizer.py)）
- Vertex AI Gemini で生成
- raw 応答を pydantic-like dict にパース
- state_delta で次エージェントへ受け渡し

### マルチターン質問機能（Step 7）

[scripts/boss_clone_chat.py](../scripts/boss_clone_chat.py)

**設計の本質**：「考えてる時間」を「ユーザーが質問に答えてる時間」に置き換える。

```
Turn 1: ユーザー入力 → System1（8-15秒）で質問生成
                    → System2 はバックグラウンドで並列実行（asyncio.create_task）
Turn 2: ユーザー回答 → System2 await（既にほぼ完了）→ Synthesizer 統合
```

体感待ち時間 = 約 **25-37 秒**（実時間 37-52 秒のうち、ユーザー回答時間は「待ち」扱いしない）。

### Synthesizer 出力品質改善（P0、Day 3 終盤）

[prompts/synthesizer.py](../scripts/boss_clone_lib/prompts/synthesizer.py) を改訂し、
- **「直感」「熟考」「System1」「System2」「エージェント」を出力禁止**
- 1 人のわたなべ部長として、自分の頭の中の整理として書く
- 不一致は「ここがポイント」「一回立ち止まりたい」と自分の語彙で噛み砕く

3 シナリオで grep 検証 → 内部用語が **完全消去**、自然な部長語り（「OK、…の件、少し整理しようか」）。

---

## Day 3 で生まれた知見（lessons.md L-008〜候補）

> 正式追記は出張明けの「プロンプト改善ターン」と同時に行う。ここでは候補として記録。

### L-008 候補: Synthesizer が内部実装を漏らす罠

- **症状**: 統合エージェントが「直感は採用、熟考は保留」のような内部状態をユーザーに見せてしまう
- **原因**: プロンプトで「直感」「熟考」と呼んで参照させていたため、出力にもそのまま出る
- **解決**: プロンプトに **「内部の使い分け」**（モデル向け）と **「出力で絶対に使ってはいけない語彙」**（出力向け）を明示的に分離。良い例 / 悪い例も提示
- **適用範囲**: 複数エージェント出力を統合する Synthesizer/Aggregator 一般
- **横展開判断**: 昇格候補（マルチエージェント設計の頻発パターン）

### L-009 候補: マルチターン質問機能で体感速度を消す設計パターン

- **症状**: 単発 LLM 設計だと「ユーザーが 60 秒沈黙して待つ」体験になり、対話のリズムが死ぬ
- **解決**: Turn 1 で軽量モデル（System1=Flash）が即質問、Turn 2 まで重量モデル（System2=Pro thinking）をバックグラウンド並列実行
- **実装ポイント**: `asyncio.create_task` で System2 を先行起動し、Turn 2 で `await` する。ユーザーが質問に答えてる時間（10-30 秒）の間に System2（17 秒）が完走する確率が高い
- **適用範囲**: ユーザー対話を伴うあらゆるマルチエージェント / RAG システム
- **横展開判断**: 昇格候補（プロダクトの差別化要素になる、note 記事化候補）

### L-010 候補: Pro thinking_budget の品質トレードオフ

- **観察**: thinking_budget を 4000 → 2000 → 1000 と段階的に下げた結果、Pro なら 1000 でも論点 2 個以上を維持できる。Flash は thinking_budget=2000 でも論点 1 個に崩れる
- **適用範囲**: Gemini 2.5 Pro/Flash の使い分け判断
- **横展開判断**: 保留（モデル仕様は短期で変わる、Gemini 3 出たら再評価）

### L-011 候補: ADK BaseAgent + pydantic の追加属性パターン

- **症状**: `BaseAgent` は pydantic ベースなので `self.x = y` が許されず、エージェントに RetrievalService 等を持たせるのに `model_post_init` + `object.__setattr__` が必要
- **適用範囲**: ADK で BaseAgent 継承するすべてのエージェント
- **横展開判断**: 保留（ADK バージョン依存）

---

## 残課題（出張明けに対応）

### プロンプト改善ターン候補

| ID | 内容 | 工数 | 優先度 |
|---|---|---:|---|
| TODO-P1 | System1 reference_cases 具体化 | 30分 | 中 |
| TODO-P2 | System1 3択語彙の文脈適合 | 30分 | 低 |
| TODO-P3 | App name mismatch warning 対処 | 10分 | 低 |
| TODO-P4 | System2 verification_items パース安定化 | 30分 | 中 |
| TODO-P5 | System2 論点数 1 個になるケース対策 | 30分 | 中 |
| TODO-P6 | Synthesizer parser 強化（issues_summary 3 件） | 45分 | 中 |
| TODO-P7 | System1 質問品質（具体性・粒度） | 30分 | 中 |
| **TODO-P8** | **System1 質問の選択肢化（フリー回答→選択肢2-4個）** | 60-90分 | **高** |
| TODO-P9 | ストリーミング表示（タイピング感） | 60-90分 | 中 |

### Day 4-5（出張明け）の優先順位

1. **TODO-P8 質問選択肢化** — ユーザー入力負荷を最小化、デモテンポ更に向上
2. **Web UI / Streamlit** — 思考プロセスの 3 カラム表示（System1 / System2 / Synthesizer の中身を見せる）
3. **デモ動画撮影** — Proto Pedia 提出用
4. **アーキテクチャ図** — Proto Pedia 提出用
5. **ストーリー文章** — Proto Pedia 必須 3 パート
6. TODO-P1〜P7 をまとめてプロンプト改善ターン

---

## 動作確認済み事項

### 単体テスト

| ファイル | 件数 | 状態 |
|---|---:|---|
| test_boss_clone_stubs.py | 6 検証 | ✅ |
| test_retrieval_service.py | 3 クエリ | ✅ |
| test_system1_agent.py | 3 クエリ | ✅ |
| test_system2_agent.py | 3 クエリ | ✅ |
| test_synthesizer_agent.py | 一致/不一致/e2e | ✅ |
| test_boss_clone_chat.py | 3 シナリオ e2e | ✅ |
| test_masking_pipeline.py | 35 unit | ✅ |
| test_embedding_pipeline.py | 22 unit | ✅ |

### マルチターン e2e（3 シナリオ）

| シナリオ | S1 | S2 | alignment | Turn1 | Turn2 | 体感 | 実時間 |
|---|---|---|---|---:|---:|---:|---:|
| A: セキュリティレビュー必要か | 条件付き保留 | 条件付き保留 | **aligned** | 8.4s | 17.7s | 26.2s | 37.5s |
| B: kabe 新機能リリース | 採用 | 条件付き保留 | **misaligned** | 14.9s | 22.3s | 37.2s | 52.2s |
| C: 顧客要望保留 | 却下 | 条件付き保留 | **misaligned** | 14.9s | 18.4s | 33.3s | 42.3s |

### Synthesizer 出力品質

- **「直感」「熟考」「System1」「System2」「エージェント」「内部分析」「並列実行」が完全消去**（3 シナリオで grep 検証済）
- わたなべ部長の口調（「OK、…の件、少し整理しようか」「ここからが大事なところだ」「持ってきてくれ」）
- MUST/WANT 分離、トレードオフ明示、論点先出し、問い返し型 — 5 特徴がすべて自然に出る

### Vector Search リソース（朝の判断対象）

- **Firestore**: `pairs` 2787 docs / `acme_kb` 46 docs（無料枠内）
- **Vector Search**: `pair_summaries_v1` endpoint 稼働中（月 $30-50 維持コスト）
- **GCS bucket**: `boss-clone-2026-vector-search`
- Day 3 完了時点で teardown せず維持（Day 4 のデモ撮影で使う）

---

## 次のフェーズ（Day 4-5、出張明け）

### Day 4（6/27 戻り直後）

1. CLI マニュアル試行で UX 最終調整
2. TODO-P8 質問選択肢化（最優先）
3. Web UI / Streamlit 検討（3 カラム表示）

### Day 5（提出直前）

1. デモ動画撮影
2. Proto Pedia 提出
3. README 最終化

---

## コスト累計（概算）

| フェーズ | コスト |
|---|---:|
| Day 1 hello agent | 〜$0.01 |
| Day 2 masking pipeline（フル処理 + retry） | $0.20 |
| Day 2 embedding pipeline（Phase 1+2） | $1.10 |
| Day 2 Vector Search 構築（Phase 1+2） | $0.00（構築自体は無料） |
| Day 3 マルチエージェント実装 + 試行 | $0.50 |
| **合計** | **約 $1.81** |

Free Credit 残高 $320 のうち約 0.6% 消費。**余裕で残予算内**。

---

**End of Day 3 Summary**
