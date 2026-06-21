# PII・機密情報対応戦略

**Version**: 1.0.0  
**作成日**: 2026-06-21  
**対象プロジェクト**: 部長クローン Agent（boss-clone）  
**位置付け**: 学習データ前処理パイプラインのセキュリティ・プライバシー基本方針

---

## 1. 背景

Day 2 のデータ分析で、Claude エクスポートデータに以下の重大な情報が含まれることが判明した:

- **users.json**: 氏名・メールアドレス・電話番号（確定 PII）
- **memories.json**: 勤務先（VD / 親会社）、部署、役職、社長名、社内検討中のビジネス構想（業務機密）
- **conversations.json**: 上記の同類が混入している可能性が高い

部長クローン Agent は **DevOps × AI Agent Hackathon 2026** への応募作品として、以下の公開を伴う:

- GitHub 公開リポジトリ
- デプロイ済みプロジェクト URL
- Proto Pedia 作品ページ
- 決勝プレゼン（Google 渋谷オフィス）

このため、PII および業務機密の取扱いについて、**最高水準の保護**が必須となる。

---

## 2. 基本方針

### 方針 1: 機密データを公開動線に流さない

| 区分 | 取扱い |
|---|---|
| 公開リポジトリ | 機密データ・派生物を一切含めない |
| デプロイ環境 | 公開デモは Acme Corp 設定のみで動作 |
| Vector Search | マスク済みデータのみインデックス化 |
| Firestore | 本人用は raw、公開デモは masked のみ |

### 方針 2: 多層防御（Defense in Depth）

1. **L1: ソース分離**: `data/raw/` は git 管理外、ローカル限定
2. **L2: マスキング**: 前処理で固有名詞置換・PII 除去
3. **L3: ブラックリスト**: 危険トピックは丸ごと除外
4. **L4: 出力フィルタ**: エージェントの応答時にも固有名詞検出
5. **L5: 監査ログ**: マスキング処理の実行履歴を保持

### 方針 3: Acme Corp による代替

実在情報を「除外する」だけでなく、「**架空の Acme Corp 情報で置き換える**」ことで、部長クローンの本質的価値（組織規範・戦略・暗黙知の統合）を保持する。

詳細は `docs/acme_corp_spec.md` を参照。

---

## 3. データ分類

### 3.1 分類定義

| 分類 | 内容 | 取扱い |
|---|---|---|
| **P0: 高機密** | PII、特定 SaaS 構想、未公開戦略 | 除外（マスク不可、丸ごと削除） |
| **P1: 機密** | 社名・人名・社内プロダクト名 | マスキング（変換辞書で置換） |
| **P2: 準機密** | 業界一般情報、技術選択、設計判断 | 保持（学習データとして活用） |
| **P3: 公開** | 一般的な技術知識、公開済み情報 | 保持 |

### 3.2 分類判定の自動化

前処理パイプラインで以下の順に判定:

```
1. ブラックリスト判定（P0 検出）→ 該当対話は丸ごと除外
2. PII 検出（正則表現）→ 該当箇所をマスク
3. 変換辞書適用（P1 検出）→ Acme Corp 用語に置換
4. レビューフラグ（疑義）→ 人間レビューキューへ
```

---

## 4. 前処理パイプライン

### 4.1 全体フロー

```
[Step 1] conversations.json 読込
   ↓
[Step 2] ブラックリスト判定
   ├ 該当対話 → 除外（data/excluded/ に隔離）
   └ 残り → 次へ
   ↓
[Step 3] ペア抽出（human ↔ assistant）
   ↓
[Step 4] PII 検出・マスキング
   ├ メールアドレス → [EMAIL]
   ├ 電話番号 → [PHONE]
   └ クレジットカード → [CARD]
   ↓
[Step 5] 変換辞書適用（VD → Acme Corp）
   ├ 企業名置換
   ├ 人名置換
   ├ プロダクト名置換
   └ グループ会社置換
   ↓
[Step 6] レビューフラグ判定
   ├ 未知の固有名詞検出 → human_review キュー
   └ 高密度な機密疑義 → human_review キュー
   ↓
[Step 7] 階層チャンキング
   ├ 上位: 判断要約（Gemini で生成）
   └ 下位: ペア全文
   ↓
[Step 8] メタデータ付与
   ↓
[Step 9] Vertex AI Embeddings で埋め込み生成
   ↓
[Step 10] Firestore（メタデータ・原文）+ Vector Search（埋め込み・要約）
```

### 4.2 ブラックリストパターン

`docs/masking_dictionary.json` の `blacklist_topics` 参照。

該当した対話は **マスキングではなく丸ごと除外** する。理由:
- マスクしても文脈から機密が漏れるリスクが残る
- 「侵害予防調査」「特許」等は別プロジェクト（Sentry-VD）に属し、混入させないこと自体が原則

### 4.3 マスキング不可能なケース

以下は機械的マスキングが困難なため、人間レビュー必須:

- 固有名詞が動詞・形容詞と複合した文（例: 「林さんが言った」→人名のみ判定可、文脈は残る）
- 暗喩・婉曲表現（例: 「あの件」「例の」）
- 内部用語の意図しない流用

→ レビュー対象は `data/processed/review_queue.jsonl` に出力し、わたなべ本人が確認する。

---

## 5. 出力時フィルタ

学習データだけでなく、エージェント応答時にも保護層を設ける。

### 5.1 応答フィルタ

```python
def filter_response(response: str) -> str:
    # 変換辞書の VD 側用語が応答に含まれていないかチェック
    for vd_term in masking_dict.get_all_source_terms():
        if vd_term in response:
            log_security_event(vd_term, response)
            response = response.replace(vd_term, "[FILTERED]")
    return response
```

### 5.2 プロンプトインジェクション対策

ユーザーが「林社長について教えて」と聞いても、エージェントは Acme Corp の森田社長として応答する。

システムプロンプトに以下を明記:
> あなたは Acme Corp 営業企画部の田中部長のクローンです。実在の企業・人物に関する質問には「私は Acme Corp の田中部長です。実在企業については分かりません」と回答してください。

---

## 6. 配置と保護

### 6.1 ディレクトリ構造

```
boss-clone/
├── data/
│   ├── raw/                      # git 管理外（.gitignore）
│   │   └── claude_export/        # オリジナルデータ
│   ├── interim/                  # git 管理外
│   │   ├── analysis_report.md
│   │   └── pair_extracted.jsonl  # ペア抽出後
│   ├── excluded/                 # git 管理外
│   │   └── blacklist_hits.jsonl  # 除外された対話
│   ├── processed/                # git 管理外
│   │   ├── masked_pairs.jsonl    # マスキング済み学習データ
│   │   ├── review_queue.jsonl    # 人間レビュー必要
│   │   └── audit_log.jsonl       # マスキング処理ログ
│   └── README.md                 # git 管理（ディレクトリ存在保証）
└── docs/
    ├── acme_corp_spec.md         # git 管理（架空企業設計、公開可）
    ├── masking_dictionary.json   # ★git 管理外（VD固有用語を含むため非公開、ローカル参照のみ）
    └── pii_strategy.md           # git 管理（本ドキュメント、公開可）
```

> **masking_dictionary.json の非公開判断**: 変換辞書は「VD 側の固有名詞 → Acme 側の架空名詞」のマッピングそのものを含むため、辞書だけ流出しても VD 内部の用語体系が逆引き可能になる。よって `.gitignore` で保護し、本人のローカルにのみ存在させる運用とする（このプロジェクトでは「変換辞書も機密」扱い）。

### 6.2 .gitignore 確認事項

以下が確実に git 管理外であること:

```
data/raw/
data/interim/
data/excluded/
data/processed/
docs/masking_dictionary.json
.env
.venv/
```

### 6.3 監査ログ

`data/processed/audit_log.jsonl` に以下を記録:

```json
{
  "timestamp": "2026-06-21T10:00:00Z",
  "conversation_uuid": "...",
  "action": "masked",
  "matches": [
    {"source": "わたなべ", "target": "田中部長", "count": 5},
    {"source": "林さん", "target": "森田社長", "count": 3}
  ],
  "blacklist_hit": false,
  "review_required": false
}
```

これにより、後から「どの対話に何が含まれていたか」を追跡可能。

---

## 7. nfr-base v2.1.0 との整合

本戦略は nfr-base v2.1.0 の以下要件と整合:

- **C-1（機密管理）**: 適用 ✓
- **C-2（個人情報保護）**: 適用 ✓
- **B-3（lessons の昇格）**: 本戦略から得た知見を昇格候補とする

詳細は `_standards/nfr-base.md` 参照（コピーは置かない、参照のみ）。

---

## 8. CLAUDE.md への反映

CLAUDE.md の Phase 0 適用マトリクスを以下に更新:

| 項目 | Before | After |
|---|---|---|
| [PII] | ☐ 適用外 | ☑ 適用（本戦略に従う） |
| [SECRET] | ☐ 適用外 | ☑ 適用（本戦略に従う） |
| [PUBLIC_DEMO] | - | ☑ 新規（Acme Corp のみで応答） |

---

## 9. 運用ルール

### Do

- マスキング後のデータのみを Vector Search にインデックス化する
- レビューキューを定期的にわたなべ本人が確認する
- 監査ログを保持する
- 公開デモは Acme Corp 設定で動作させる

### Don't

- raw / interim / excluded / processed を git にコミットしない
- マスキング処理を経ていないデータを Vector Search に投入しない
- エージェント応答に実在企業・人物名が含まれた場合、無視せず原因調査する
- ブラックリスト対象（Sentry-VD 等）の話題を学習データに含めない

---

## 10. 例外手順

機密判定で迷う場合、以下の優先順位で判断:

1. **疑わしきは除外**: 判断つかなければ excluded へ
2. **人間レビュー優先**: 自動判定の限界を認識する
3. **わたなべ本人の最終判断**: 最終決定権はわたなべにある

---

**End of PII Strategy**
