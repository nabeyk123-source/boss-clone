# マスキングパイプライン実装仕様

**Version**: 1.0.0  
**作成日**: 2026-06-21  
**対象**: Day 2 タスク3（マスキング処理本体の実装）  
**前提**: `docs/pii_strategy.md`、`docs/masking_dictionary.json`、`docs/acme_corp_spec.md` 配置済み

---

## 1. 目的

`data/raw/claude_export/conversations.json`（76 対話 / 6,808 メッセージ / 約 75MB）を、Vector Search に投入可能な形にマスキング・構造化する前処理パイプラインを実装する。

---

## 2. 入出力

### 入力

- `data/raw/claude_export/conversations.json`（オリジナル）
- `docs/masking_dictionary.json`（変換辞書、非公開）

### 出力

| ファイル | 内容 | git管理 |
|---|---|---|
| `data/interim/pair_extracted.jsonl` | ペア抽出後（マスキング前） | 管理外 |
| `data/excluded/blacklist_hits.jsonl` | ブラックリストヒットで除外された対話 | 管理外 |
| `data/processed/masked_pairs.jsonl` | マスキング済み学習データ（メイン成果物） | 管理外 |
| `data/processed/review_queue.jsonl` | 人間レビュー必要なペア | 管理外 |
| `data/processed/audit_log.jsonl` | マスキング処理ログ | 管理外 |
| `data/processed/unknown_entities.jsonl` | 未知固有名詞リスト（バッチ抽出） | 管理外 |
| `data/processed/processing_report.md` | 処理結果サマリ | 管理外 |

---

## 3. パイプライン全体構造

```
[Step 1] 設定読込
   ├ masking_dictionary.json
   └ べき等性チェック用ハッシュ

[Step 2] conversations.json 読込（メモリ読み、75MBなら問題なし）

[Step 3] 対話単位処理（外側ループ）
   ├ Step 3-1: ブラックリスト判定
   │  └ ヒット → blacklist_hits.jsonl へ、対話全体スキップ
   ├ Step 3-2: メッセージ並べ替え（parent_message_uuid で本ブランチを特定）
   ├ Step 3-3: human ↔ assistant ペア抽出 → pair_extracted.jsonl
   └ Step 3-4: 各ペア処理（内側ループ）

[Step 4] 各ペア処理（内側ループ）
   ├ Step 4-1: PII 検出（正則表現）
   ├ Step 4-2: 変換辞書適用（VD → Acme Corp）
   ├ Step 4-3: 未知固有名詞検出 → unknown_entities.jsonl
   ├ Step 4-4: 簡易タグ判定（Gemini Flash で OK/NG/保留）
   ├ Step 4-5: レビューフラグ判定
   │  ├ 低信頼度マスキング → review_queue.jsonl
   │  └ それ以外 → masked_pairs.jsonl
   └ Step 4-6: audit_log.jsonl 追記

[Step 5] 処理結果サマリ生成
   └ processing_report.md
```

---

## 4. 詳細仕様

### 4.1 Step 1: 設定読込

```python
import json
import hashlib

with open("docs/masking_dictionary.json", "r", encoding="utf-8") as f:
    dictionary = json.load(f)

# べき等性ハッシュ
dict_hash = hashlib.sha256(
    json.dumps(dictionary, sort_keys=True).encode("utf-8")
).hexdigest()[:16]

raw_file_hash = hashlib.sha256(
    open("data/raw/claude_export/conversations.json", "rb").read()
).hexdigest()[:16]

processing_id = f"{raw_file_hash}_{dict_hash}"
```

`processing_id` を全ての出力ファイルのメタデータに含める。これにより：
- 同じ入力 + 同じ辞書なら同じ `processing_id` → 既処理判定可能
- 辞書更新時は `dict_hash` 変化 → 再処理が必要と即判定

### 4.2 Step 3-1: ブラックリスト判定（対話単位）

```python
def is_blacklisted(conversation: dict, blacklist_patterns: list) -> tuple[bool, str]:
    """対話全体のテキストを連結してパターンマッチ"""
    full_text = "\n".join(
        msg.get("text", "") for msg in conversation.get("chat_messages", [])
    ).lower()
    
    for pattern in blacklist_patterns:
        if pattern.lower() in full_text:
            return True, pattern
    return False, ""
```

**重要**：
- 対話全体のテキストを結合して判定（メッセージ単位ではない）
- ヒットした対話は **対話まるごと除外**（前後文脈の漏洩防止）
- 除外ログは `blacklist_hits.jsonl` に記録（対話UUID + ヒットパターン）

### 4.3 Step 3-2: メッセージ並べ替え

`conversations.json` の `chat_messages` は時系列でフラットに並んでるが、`parent_message_uuid` で枝分かれしてる。**主要ブランチ（最終的に成立した会話）のみを抽出**。

```python
def extract_main_branch(messages: list) -> list:
    """parent_message_uuid を辿って最終ブランチを特定"""
    # 末端メッセージ（誰の親にもなってない最新のもの）を起点
    # parent を辿って先頭まで遡る
    by_uuid = {m["uuid"]: m for m in messages}
    parent_of = {m["uuid"]: m.get("parent_message_uuid") for m in messages}
    children_of = {}
    for m in messages:
        p = m.get("parent_message_uuid")
        if p:
            children_of.setdefault(p, []).append(m["uuid"])
    
    # 末端を探す（子供を持たない、または最新のもの）
    leaves = [m for m in messages if m["uuid"] not in children_of]
    if not leaves:
        return messages  # フォールバック
    
    # 最新の末端から遡る
    latest_leaf = max(leaves, key=lambda m: m.get("created_at", ""))
    chain = []
    current = latest_leaf
    while current:
        chain.append(current)
        parent_uuid = current.get("parent_message_uuid")
        current = by_uuid.get(parent_uuid) if parent_uuid else None
    
    return list(reversed(chain))
```

### 4.4 Step 3-3: human ↔ assistant ペア抽出

```python
def extract_pairs(messages: list) -> list:
    """連続する human → assistant のペアを抽出"""
    pairs = []
    i = 0
    while i < len(messages) - 1:
        if messages[i]["sender"] == "human" and messages[i+1]["sender"] == "assistant":
            pairs.append({
                "human_text": messages[i].get("text", ""),
                "assistant_text": messages[i+1].get("text", ""),
                "human_uuid": messages[i]["uuid"],
                "assistant_uuid": messages[i+1]["uuid"],
                "created_at": messages[i].get("created_at"),
            })
            i += 2
        else:
            i += 1
    return pairs
```

### 4.5 Step 4-1: PII 検出

辞書の `pii_patterns` を使った正則表現マスキング。

```python
import re

def mask_pii(text: str, pii_patterns: dict) -> tuple[str, list]:
    """PII を [EMAIL] [PHONE] [CARD] に置換"""
    masks_applied = []
    for pattern_name, pattern_def in pii_patterns.items():
        if pattern_name.startswith("_"):
            continue
        regex = pattern_def["regex"]
        replace = pattern_def["replace_with"]
        matches = re.findall(regex, text)
        if matches:
            text = re.sub(regex, replace, text)
            masks_applied.append({
                "type": pattern_name,
                "count": len(matches),
                "replace_with": replace,
            })
    return text, masks_applied
```

### 4.6 Step 4-2: 変換辞書適用

```python
def apply_dictionary(text: str, dictionary: dict) -> tuple[str, list]:
    """辞書の各カテゴリを順次適用"""
    replacements = []
    
    # preserve_as_is を一時マスキング（変換から保護）
    preserved = {}
    for term in dictionary["preserve_as_is"]["terms"]:
        placeholder = f"__PRESERVE_{len(preserved)}__"
        if term in text:
            preserved[placeholder] = term
            text = text.replace(term, placeholder)
    
    # カテゴリ別変換（長い語句から先に適用、部分マッチ防止）
    categories = ["company", "executives", "user_identity", 
                  "group_companies", "products", "departments", 
                  "external_companies"]
    
    for category in categories:
        if category not in dictionary:
            continue
        sorted_terms = sorted(
            dictionary[category].items(),
            key=lambda x: len(x[0]),
            reverse=True
        )
        for source, target in sorted_terms:
            if source in text:
                count = text.count(source)
                text = text.replace(source, target)
                replacements.append({
                    "category": category,
                    "source": source,
                    "target": target,
                    "count": count,
                })
    
    # preserve を元に戻す
    for placeholder, original in preserved.items():
        text = text.replace(placeholder, original)
    
    return text, replacements
```

**実装上の重要ポイント**：
- **長い語句から先に**：「VDグループ」を「Acme Corp グループ」に先に変換しないと、「VD」だけ変換されて「Acme Corp グループ」のはずが「Acme Corp Corp」になる
- **preserve_as_is は最初に保護**：「Claude」「クロ」等が誤変換されないよう一時マスキング
- **大文字小文字**: 辞書は大文字小文字混在で持つ、案件に応じて手当て

### 4.7 Step 4-3: 未知固有名詞検出（バッチ）

このステップは**各ペア処理時には検出だけ**、全体処理後にバッチ集計。

人名・社名「らしさ」を持つ構造をパターンで6種に絞り込む（カタカナ単独検出はしない）。`known_terms` には `preserve_as_is` および辞書登録済み source/target を含める。

```python
import re

# 各エントリ: (compiled_regex, type_label, term_group)
# term_group: マッチ全体を取るなら 0、特定キャプチャだけ取るなら 1〜
PATTERNS = [
    # 1. 日本語敬称
    (re.compile(r'([一-龥]{1,4}|[ァ-ヴー]{2,8})(さん|くん|ちゃん|氏)'),
     'honorific', 0),

    # 2. 役職付き
    (re.compile(r'([一-龥]{1,4}|[ァ-ヴー]{2,8})(社長|部長|課長|係長|主任|取締役|常務|専務|副社長|会長|CEO|CTO|CFO)'),
     'title', 0),

    # 3. 企業形態（前置）
    (re.compile(r'(株式会社|有限会社|合同会社)[一-龥ァ-ヴー\s]{1,15}'),
     'company_jp_prefix', 0),

    # 4. 企業形態（後置）
    (re.compile(r'[一-龥ァ-ヴー\s]{1,15}(株式会社|有限会社|合同会社)'),
     'company_jp_suffix', 0),

    # 5. 英文社名候補（大文字始まり、企業っぽい接尾辞付き）
    (re.compile(r'\b[A-Z][a-zA-Z]{3,15}(?:field|labs|tech|systems|solutions|pay|payments?|Inc\.?|Corp\.?|LLC|Ltd\.?)\b'),
     'company_en', 0),

    # 6. 主語っぽい人名候補（カタカナ + 助詞）— 助詞は除いて term に記録
    # 注: 末尾の \b は ASCII 語境界のみで日本語コンテキストでは常偽になるため付けない（v1.1.1）
    (re.compile(r'([ァ-ヴー]{3,8})(が|は|を|に|から|と)'),
     'name_with_particle', 1),
]


def detect_unknown_entities(text: str, known_terms: set) -> list:
    if not text:
        return []
    seen = set()
    out = []
    for pat, kind, group in PATTERNS:
        for m in pat.finditer(text):
            term = (m.group(group) or '').strip()
            if not term or term in known_terms:
                continue
            key = (term, kind)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                'term': term,
                'type': kind,
                'context': text[max(0, m.start()-20):min(len(text), m.end()+20)],
            })
    return out
```

**「主語+助詞」パターンの誤検出ポリシー**:
一般カタカナ語（「データを送る」「ブラウザがフリーズ」など）も `name_with_particle` で拾われるが、これは設計上の許容範囲。レビュー振り分けは **未知固有名詞5件以上** のしきい値（§4.9）で吸収する。1〜2件の誤検出は通常通り通し、5件以上集中する対話はそれ自体が「固有名詞が多い＝機密濃度が高い」サインなのでレビューに回す。

全ペア処理後、`unknown_entities.jsonl` に集計：
```json
{
  "term": "鈴木さん",
  "count": 12,
  "type": "honorific",
  "sample_context": "...と鈴木さんから連絡が...",
  "decision": "pending"
}
```

#### 変更履歴

- **v1.0.0 初版**: 「カタカナ連続3〜10文字」を一律 `type='katakana'` で検出。技術対話を試したところ「ブラウザ・データ・ファイル・アプリ…」等の一般語まで未知扱いになり、3対話18ペアのうち17ペアがレビュー行きで実質運用不能だった。
- **v1.1.0 パターン特化方式**（段階4テスト 2026-06-21）: カタカナ単独検出を廃止し、上記6パターンに置換。「人名・社名らしい構造」だけを未知扱いする方針へ転換。
- **v1.1.1 微修正**: パターン6末尾の `\b`（ASCII 語境界）を削除。日本語コンテキストでは常偽になり「ヤマダが連絡」を取りこぼすため。
- **v1.2.0 振り分け責務分離**（段階4再テスト 2026-06-21）:
  - `name_with_particle` は **検出は継続**するが、人名候補としての弱さ（カタカナ一般語と区別困難）を踏まえ、レビュー振り分けロジック（§4.9）から除外する
  - `name_with_particle` のヒットは `unknown_entities.jsonl` と `processing_report.md` の「人名候補集中対話 TOP10」セクションにサマリとして残し、事後の人名集中対話発見に活用する
  - 段階4再テストで判明: 技術対話だと `name_with_particle` 単独でしきい値5を簡単に超え、review queue が18件中14件と過多になるため

### 4.8 Step 4-4: 簡易タグ判定（Gemini Flash）

各ペアに「OK / NG / 保留」のタグを付ける。**並列処理 + バッチ化** でコスト最適化。

```python
PROMPT = """以下は人間（タナカ部長）とAIアシスタント（クロ）の対話です。
人間の反応を「OK」「NG」「保留」のいずれかに分類してください。

OK: 提案・案を採用、肯定的な判断
NG: 提案を却下、別案を提示、強い反対
保留: 判断保留、追加情報要求、議論継続

人間の発言: {human_text}

分類（OK/NG/保留 の1単語のみ）:"""

def classify_pair(human_text: str, model) -> str:
    response = model.generate_content(
        PROMPT.format(human_text=human_text[:500]),
        generation_config={"max_output_tokens": 10, "temperature": 0.1},
    )
    tag = response.text.strip()
    if tag in ["OK", "NG", "保留"]:
        return tag
    return "保留"  # フォールバック
```

**コスト試算**：
- 3,404ペア × Gemini 2.5 Flash 軽量呼び出し
- 1ペア = 入力500トークン + 出力5トークン
- 推定コスト：$5〜$10 程度

### 4.9 Step 4-5: レビューフラグ判定

以下のいずれかに該当したら `review_queue.jsonl` へ：

1. **マスキング不確実性**：preserveとの衝突、辞書外の固有名詞密度が高い
2. **PII検出回数が多い**：1ペアに3件以上のPIIヒット
3. **人名・社名候補の集中**：`name_with_particle` を除く unknown が5件以上
4. **タグ判定失敗**：Gemini が `retry_exhausted` または空応答

```python
WEAK_UNKNOWN_TYPES = {"name_with_particle"}  # 一般カタカナ語と区別困難なため振り分けから除外

def needs_review(pii_masks: list, unknowns: list, tag: str, tag_status: str) -> bool:
    # PII 多発
    if sum(m.get("count", 0) for m in pii_masks) >= 3:
        return True

    # 人名・社名候補の集中検出（name_with_particle は除外）
    strong_unknowns = [u for u in unknowns if u.get("type") not in WEAK_UNKNOWN_TYPES]
    if len(strong_unknowns) >= 5:
        return True

    # API 失敗時は人間レビューに回す（タグが「保留」（議論継続）は通常通り通す）
    if tag_status.startswith("retry_exhausted") or tag_status == "empty":
        return True

    return False
```

**変更履歴**:
- v1.0.0 初版: `len(unknowns) >= 5` で振り分け。技術対話で `name_with_particle` 由来のノイズが大量に乗り、review 過多になることが段階4再テストで判明。
- v1.2.0: `name_with_particle` を `WEAK_UNKNOWN_TYPES` として振り分け対象から除外。検出自体は継続（§4.7 v1.2.0 参照）。

### 4.10 Step 4-6: 監査ログ

```python
audit_entry = {
    "timestamp": datetime.utcnow().isoformat() + "Z",
    "processing_id": processing_id,
    "conversation_uuid": conv_uuid,
    "pair_index": idx,
    "human_uuid": pair["human_uuid"],
    "assistant_uuid": pair["assistant_uuid"],
    "dictionary_replacements": replacements,  # Step 4-2 の結果
    "pii_masks": pii_masks,  # Step 4-1 の結果
    "unknown_entities_detected": len(unknowns),
    "tag": tag,
    "review_required": review_flag,
    "destination": "masked_pairs" if not review_flag else "review_queue",
}
```

### 4.11 マスキング済みペアの最終形

`masked_pairs.jsonl` の各行：

```json
{
  "processing_id": "abc123_def456",
  "conversation_uuid": "...",
  "pair_index": 5,
  "created_at": "2026-04-15T10:23:45Z",
  "human_text": "田中部長がAcme Corpで森田社長と...",
  "assistant_text": "なるほど、それは...",
  "tag": "OK",
  "metadata": {
    "had_pii": true,
    "pii_types": ["email"],
    "dictionary_categories_applied": ["company", "executives", "user_identity"],
    "dictionary_replacement_count": 8
  }
}
```

---

## 5. エラーハンドリング

| エラー | 対応 |
|---|---|
| Gemini API レート制限 | exponential backoff（最大3回リトライ） |
| Gemini API クォータ超過 | 該当ペアの tag を "保留" にして続行、ログ記録 |
| JSON パースエラー | 該当対話をスキップ、エラーログに記録 |
| 文字エンコーディング異常 | 該当ペアをスキップ、`encoding_errors.jsonl` に記録 |

**重要**：1ペアの失敗で全体停止しない。`continue` で次へ。

---

## 6. パフォーマンス・コスト

### 想定処理時間

- 全6,808メッセージ → 3,404ペア
- マスキング処理（CPU処理）: 1ペア = 数ミリ秒 → 全体で約30秒
- Gemini Flash 分類: 並列8並行で1ペア200ms → 全体で約1〜2分
- **合計：3分以内**

### コスト見積

- Gemini 2.5 Flash 呼び出し: 3,404回
- 入力トークン: 500 × 3,404 = 約170万トークン
- 出力トークン: 5 × 3,404 = 約1.7万トークン
- **推定コスト：$3〜$8（Free Creditから消費）**

---

## 7. べき等性確保

```python
def is_already_processed(processing_id: str) -> bool:
    """同じ processing_id で処理済みかチェック"""
    audit_log_path = "data/processed/audit_log.jsonl"
    if not os.path.exists(audit_log_path):
        return False
    with open(audit_log_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("processing_id") == processing_id:
                return True
    return False

# 実行時
if is_already_processed(processing_id):
    print(f"Already processed: {processing_id}. Use --force to re-run.")
    sys.exit(0)
```

`--force` フラグで強制再実行可能。

---

## 8. 実装ファイル構成

```
boss-clone/
├── scripts/
│   ├── masking_pipeline.py        # メインスクリプト
│   ├── masking_lib/
│   │   ├── __init__.py
│   │   ├── dictionary.py          # 辞書読込・適用
│   │   ├── pii.py                 # PII検出
│   │   ├── pair_extractor.py      # ペア抽出
│   │   ├── classifier.py          # Geminiタグ判定
│   │   ├── unknown_detector.py    # 未知固有名詞検出
│   │   └── audit.py               # 監査ログ
│   └── test_masking_pipeline.py   # テスト
```

---

## 9. テスト要件

### 単体テスト

- 辞書適用の長語句優先（VDグループ → Acme Corp グループ、VD → Acme Corp の順序）
- preserve_as_is の保護（「クロ」が「Acme Corp」に誤変換されない）
- ブラックリスト検出（Sentry-VD を含む対話の除外）
- PII検出（メール・電話番号・カード番号）
- べき等性（同じ入力で同じ出力）

### 統合テスト

- 5サンプル対話を入力 → 期待される出力件数の検証
- masked_pairs / review_queue / excluded の振り分け確認

---

## 10. 実行コマンド

```bash
# 通常実行
python scripts/masking_pipeline.py

# 強制再実行
python scripts/masking_pipeline.py --force

# テストモード（最初の3対話のみ）
python scripts/masking_pipeline.py --test

# Gemini分類スキップ（タグなしで高速処理）
python scripts/masking_pipeline.py --no-classify
```

---

## 11. 完了報告フォーマット

実行完了後、`data/processed/processing_report.md` を生成：

```markdown
# Masking Pipeline Report

- Processing ID: abc123_def456
- 実行時刻: 2026-06-21T14:30:00Z
- 辞書バージョン: 1.0.0

## 入出力
- 入力対話数: 76
- 抽出ペア数: 3,404

## 振り分け結果
- masked_pairs: 3,201
- review_queue: 89
- excluded（ブラックリスト）: 114

## マスキング統計
- PII置換: 47件（email: 31, phone: 14, card: 2）
- 辞書置換: 12,847件
  - company: 3,421
  - executives: 1,892
  - user_identity: 6,234
  - ...

## 未知固有名詞
- ユニーク数: 47件
- 上位5件: ...

## タグ分布
- OK: 1,832
- NG: 487
- 保留: 882

## 処理時間・コスト
- 総処理時間: 2分43秒
- Gemini API コスト: $4.21
```

---

## 12. nfr-base v2.1.0 適合

- **C-6 個人情報保護対応**: 本パイプラインで実装 ✓
- **E-2 出力検証**: 監査ログによる事後検証可能 ✓
- **L-001 サロゲート対策**: ファイル冒頭で UTF-8 設定 ✓
- **L-002 PII戦略**: 本仕様書が実装 ✓

---

**End of Masking Pipeline Spec**
