# Boss Clone — Lessons Log

このプロジェクト固有の作業ログ。気づいた時点で1件1ブロックで追記する。

## 運用ルール

- **番号体系**: `L-001` から連番。欠番は作らない。
- **昇格運用**: boss-clone 完了時、または他案件で同じ罠を踏んだ時点で `_standards/nfr-base.md` の「知見ログ（Lessons Log）」テーブルへ昇格させる。
- **昇格の判断軸**: 「複数サービスで再発しそう／判断の型として汎用化できる／一度ハマると痛い」（nfr-base.md R-5）。
- **横展開判断**:
  - `要` — 即時に正本へ昇格すべき。
  - `保留` — 1度しか踏んでいない、または環境固有。再現を待つ。
  - `不要` — このプロジェクト固有の事情。昇格しない。

各エントリの形式:

```
### L-XXX: 短いタイトル
- 日付:
- 症状:
- 原因:
- 解決:
- 適用範囲:
- 横展開判断: 要 / 保留 / 不要（理由）
```

---

## エントリ

### L-001: Windows + Python stdio で日本語入力が `\udcXX` サロゲート化し Gemini 呼び出しが落ちる
- 日付: 2026-06-20
- 症状: Windows + PowerShell から `python hello_agent.py` を起動して日本語を打ち込むと、`PydanticSerializationError: 'utf-8' codec can't encode character '\udc83' in position 9: surrogates not allowed` で Gemini への JSON シリアライズが失敗。同時に起動メッセージも `�N��: ����ɂ��́` のような文字化けで出力される。
- 原因: Windows コンソールの既定コードページが cp932。Python 3.x の `sys.stdin/stdout` がこれに従い、UTF-8 で来た日本語入力を cp932 で解釈できない部分を `surrogateescape` で `\udcXX` として保持する。その文字列がそのまま google-genai → pydantic → `model_dump_json` に渡され、UTF-8 シリアライズ時にサロゲートを拒否されて落ちる。出力側も cp932 で書き出すため化ける。
- 解決: スクリプト冒頭で標準入出力を UTF-8 に再構成する。
  ```python
  import sys
  for s in (sys.stdin, sys.stdout, sys.stderr):
      if hasattr(s, "reconfigure"):
          s.reconfigure(encoding="utf-8", errors="replace")
  ```
  ファイル外で対処するなら環境変数 `PYTHONIOENCODING=utf-8` でも同等。CI/Cloud Run 側では発生しないため、ローカル動作確認のためだけに必要。
- 適用範囲: Windows 環境でローカル実行する Python スクリプトのうち、日本語入力を受けて Claude / Gemini など外部 LLM API へ送るもの全般。CLI / 対話ループに限らず、CSV や JSON ファイルから日本語を読む処理でも encoding 未指定だと同型を踏む可能性あり。
- 横展開判断: **保留**（理由: Windows 特有の症状で、Mac / Linux / Cloud Run では再現しない。他案件で再現したら nfr-base.md の知見ログへ昇格して B-6 ログレベリングや C-7 周辺の補足として組み込む候補）。

### L-002: Claude エクスポートに含まれる PII / 業務機密を発見、対応戦略を確立
- 日付: 2026-06-21
- 症状: Day 2 タスク1 のデータ分析（[scripts/analyze_export.py](scripts/analyze_export.py)）で、`users.json` に氏名・メアド・電話番号、`memories.json` に勤務先（VD・親会社）・部署・役職・社長名・社内検討中ビジネス構想、`conversations.json` 本文にも同類の固有名詞が含まれることを確認。当初の CLAUDE.md 適用判断で `[PII] ☐` としていた前提が崩れた。
- 原因: Claude との日常対話は実名・実情報・現実の業務文脈で行われるため、エクスポートデータは**実運用ログそのもの**になる。ハッカソン提出物として公開リポジトリ + Cloud Run + Proto Pedia + 決勝プレゼンで公開する以上、生データを公開動線に乗せることは不可。
- 解決:
  1. **架空企業 Acme Corp 設計書**（[docs/acme_corp_spec.md](docs/acme_corp_spec.md)）を作成し、公開デモはこの設定のみで動作させる
  2. **変換辞書**（`docs/masking_dictionary.json`）で VD → Acme の固有名詞マッピングを定義。辞書自体も内部用語が逆引き可能なため `.gitignore` で保護
  3. **多層防御パイプライン**（[docs/pii_strategy.md](docs/pii_strategy.md)）で「ソース分離→ブラックリスト除外→マスキング→レビューキュー→出力フィルタ→監査ログ」を構造化
  4. CLAUDE.md にトリガー `[PII] ☑ / [SECRET] ☑ / [PUBLIC_DEMO] ☑` を新設・格上げ、`C-6 個人情報保護対応` を採用要件に追加
- 適用範囲: Claude / ChatGPT 等の LLM 対話ログをハッカソン・ポートフォリオ・公開デモなど **外部公開を伴うプロジェクト** で活用する全ケース。個人開発でログを再利用したくなる場面全般。「LLM 対話ログ = 実運用ログ」という前提認識自体が横展開価値の本質。
- 横展開判断: **昇格候補**（理由: 規制系プロジェクト・ポートフォリオ化したい個人開発・社内 PoC を社外発信する局面など、再発が確実に予想される。boss-clone 完了時 or 他案件で同型を踏んだ時点で `_standards/nfr-base.md` に「LLM 対話ログを学習データ化する際の境界設計」として E-2 補足 または C-6 補足で昇格させる）。
