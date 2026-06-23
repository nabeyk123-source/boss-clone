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

### L-003: マスキング辞書は「実データで一度回してから」改善する（反復設計）
- 日付: 2026-06-21
- 症状: 辞書 v1.0.0 で全件処理（3,371ペア）した結果、(a) 実在人物7名（渡邉さん / 吉田さん / 鵜月さん / 小池さん / 山崎さん / 久保さん / 本田さん）が辞書未登録のまま `name_with_particle` ではなく `honorific` でレポートに大量出現、(b) Acme 主人公の架空名「田中部長」が、対話に登場する**実在の「田中さん」**と衝突する設計ミスが判明した。
- 原因: 辞書設計（v1.0.0）を実データ未着手の状態で先に組み立ててしまった。Claude エクスポート 3,371ペアという実体に対して、辞書のカバー率（実在人物名のリストアップ）と架空名選定（衝突しない名前か）の両方が **推測ベース** だった。`docs/pii_strategy.md` のパイプライン設計には「未知固有名詞検出 → レビューキュー」はあったが、「初版辞書をそのまま完成扱いしない」という運用前提が明文化されていなかった。
- 解決:
  1. 全件処理 → `processing_report.md` の "真の unknown 上位リスト" を辞書追加候補として読む運用に転換
  2. v1.1.0 で実在人物7名を `user_identity` に追加（架空名は「佐々木 / 鈴木 / 中島 / 石井 / 後藤 / 岡田 / 森」）
  3. 主人公の架空名を「田中部長」→「加藤部長」へ全面リネーム（[docs/acme_corp_spec.md](docs/acme_corp_spec.md)）
  4. 実在「田中さん」は「斎藤さん」へマッピング（v1.1.0 で `user_identity` に追加）
  5. apply_dictionary の挙動を test_masking_pipeline.py に追加検証（長語句優先 + 新規 target が source と衝突しないことを single-line で確認）
- 適用範囲: 架空企業設定 + 実データマスキングを行う全プロジェクト。「マスキング辞書の初版は推測、フルランで実データ当ててから本物にする」を **2サイクル前提** で設計する。1サイクルで終わるつもりで作ると、辞書漏れ・衝突を実データを失わずに発見する機会が無い。
- 横展開判断: **昇格候補**（理由: マスキング系プロジェクト全般に効く知見。「初版辞書 → 全件処理 → unknown 上位リスト読込 → 辞書改善 → 再処理」の反復ループは、規制系・社内データ・LLM 対話ログ系すべてで使える共通パターン。`docs/pii_strategy.md` のパイプライン定義へ「v1.X.Y イテレーション運用」として組み込む候補。boss-clone 完了時に [[feedback-lessons-workflow]] と合わせて昇格を検討する）。

### L-004: AI 支援開発における「自己生成スクリプトの即時実行」リスクと permission rule 対策
- 日付: 2026-06-21
- 症状: Claude Code（私）が自分で書いた直後の `scripts/test_classifier_smoke.py` を実行しようとしたところ、Claude Code Auto Mode の分類器が「never written or shown in this transcript — running an unverified script that will call Vertex AI with credentials」として実行をブロック。
- 原因: 「LLM が自己生成したスクリプトを、レビュー前にそのままユーザーの credentials で外部 API（Vertex AI / OpenAI 等）に叩かせる」という流れは、(a) 意図しない API 呼び出しの無限ループ、(b) 想定外コスト発生、(c) 学習データ・credentials の漏洩、というリスクを抱える。Auto Mode の分類器はこの組み合わせを構造的に弾く設計になっている。
- 解決: `.claude/settings.local.json` の `permissions.allow` に **テストスクリプト限定の permission rule** を追加。
  ```
  Bash(.venv/Scripts/python.exe scripts/test_*)
  ```
  これにより `scripts/test_` プレフィックスの Python スクリプトは事前許可、本番スクリプト（`masking_pipeline.py` / 将来の `embedding_pipeline.py` 等）は引き続き個別ガード対象として残す。
- 適用範囲: AI 支援開発全般、特に外部 API 呼び出しを伴う検証スクリプトを LLM に書かせる局面。`scripts/test_*` パターン以外（本番処理、デプロイ操作、credentials を直接扱う処理）は同じガードを維持する。「テストはテストフォルダ名で識別、本番は別」が運用の肝。
- 横展開判断: **昇格候補**（理由: AI 支援開発の標準運用として `_standards/nfr-base.md` の C-2「シークレット管理」または運用ルール R 系に組み込む価値あり。pdm_and_ai note 記事化候補：「Claude Code に LLM 検証スクリプトを書かせる時の permission rule 設計」）。
- 補足知見: smoke test 実行で **Gemini 2.5 Flash の thinking モデル特性** も判明した。`max_output_tokens=10` のままだと reasoning トークンに予算を全部使われて本文0（`status=empty`）になり、フォールバックの「保留」しか返らない。`thinking_config={"thinking_budget": 0}` で thinking 無効化、`max_output_tokens=16` に増設したところ 3/3 expected と完全一致。classifier.py の `ClassifyConfig` に同設定を恒久反映済み。

### L-005: 削除系スクリプトは「失敗したら state を消さない」設計にする（teardown 冪等性 + 課金リソース取り残し回避）
- 日付: 2026-06-21
- 症状: Vector Search Phase 1 後の teardown で、`endpoint.undeploy_index(deployed_index_id=..., sync=True)` が `TypeError: unexpected keyword argument 'sync'` で失敗。例外を catch して warning ログだけ出し、**ループ末尾で state.json をクリアして「完了」と表示**してしまった。結果として endpoint は deployed のまま残存（課金継続中）なのに state は空、運用上「削除済み」と誤認する状態に。
- 原因: 2つの設計ミスの重なり。
  1. `aiplatform.MatchingEngineIndexEndpoint.undeploy_index()` は `sync` 引数を受け取らない（同期 LRO、`sync=True` は API 仕様外）。SDK バージョンで挙動が違う可能性もあるが、ここではドキュメント未確認のまま付けたのが直接原因。
  2. teardown ロジックが「例外は log warning に降格 → 最後に必ず state を消す」フローだった。失敗時の state クリアは「次回 setup できない」だけでなく「課金リソースを誰も認識できない」最悪のサイレント失敗を生む。
- 解決:
  1. `undeploy_index(deployed_index_id=...)` から `sync=True` を削除
  2. teardown を **「全段階の delete が成功した時だけ state を消す」「失敗を1つでも検出したら state を残して return（次回 retry 可能）」** に変更
  3. state を手動で復元 → 修正版 teardown 実行 → 全リソース削除（5秒で完了）を確認
- 適用範囲: **削除系スクリプト全般**。特にクラウドリソース（Vertex AI Vector Search Endpoint・Cloud Run service・Firestore DB・GCS bucket）など「残ると課金が続く」リソースを操作する全 teardown。state ファイル（resource ID キャッシュ）を持つ運用なら、失敗時は **state を絶対に消さない** こと。
- 横展開判断: **昇格候補**（理由: AI 支援開発で「削除スクリプトの sync 引数」みたいな SDK 差異は今後も頻発する。`_standards/nfr-base.md` の C-2「シークレット/インフラ管理」周辺、または運用ルール R 系に「削除系スクリプトの冪等性原則」として追加候補）。
- 補足: SDK の引数互換性は **試す前に `inspect.signature()` で確認**する習慣も入れる。今回は5秒で teardown 完了したから良かったが、本来は smoke setup 後すぐ teardown を叩いて挙動を確認しておくべきだった（45分かけてデプロイした後に teardown でハマると痛い）。

### L-006: Vertex AI Vector Search は `.jsonl` 拡張子を読まない（`.json`/`.csv`/`.avro` のみ）
- 日付: 2026-06-21
- 症状: GCS に JSONL ファイル（拡張子 `.jsonl`）をアップロードして `MatchingEngineIndex.create_tree_ah_index(contents_delta_uri=...)` を呼ぶと、`google.api_core.exceptions.FailedPrecondition: 400 Found file ... with unknown format` で即座に失敗。
- 原因: Vertex AI Vector Search の入力ファイル拡張子バリデーションが `.json`、`.csv`、`.avro` のみ受け付ける。中身が JSONL でも拡張子が `.jsonl` だと弾かれる。
- 解決: GCS にアップロードする時に拡張子を `.json` にリネーム。**ローカル側は `.jsonl` のままで OK**（中身は JSONL 形式が正解）、アップロード処理だけ `if name.endswith(".jsonl"): name = name[:-1]` で吸収。`upload_jsonl()` 内に古い `.jsonl` blob をクリーンアップする処理も追加。
- 適用範囲: Vertex AI Vector Search のインデックス構築全般。BATCH モードでも STREAM モードでも同じ。
- 横展開判断: **保留**（理由: Vector Search 固有の細かい仕様。同種を踏まないように仕様書 `docs/schema_spec.md` §5.1 Step 7 に注意書きを追記しておくレベル）。

### L-007: 作者本人の名前は架空企業設定でもマスキング不要（むしろ実名で出すべき）
- 日付: 2026-06-21
- 症状: 辞書 v1.0.0〜v1.1.1 では「わたなべ」を「田中部長」（v1.0）→「加藤部長」（v1.1）と架空名へマッピングしていた。Day 2 終盤で Acme Corp 主人公の架空化が「目的に対して過剰」だと判明し、v1.2.0 で **作者本人を実名のまま出す**方針に転換。
- 原因: 「マスキング = 登場する全人物を架空化」と過度に保守的に解釈していた。本来の機密リスクは **第三者の固有名詞**（実在の他社員、顧客、社長等）にあって、作者本人の名前は (1) 公開リポジトリで既に明らかにしており追加の漏洩リスクなし、(2) note ブランド `pdm_and_ai` ・GitHub `nabeyk123-source` として既に公開ブランド化されている、(3) むしろ「作者 × 自分の AI クローン」というデモストーリーが架空主人公より強い。最初の辞書設計で「全員マスク」を default にしたのが思考の出発点ミス。
- 解決:
  1. `masking_dictionary.json` v1.2.0 で `user_identity` から作者本人系（わたなべ・ワタナベ・渡辺・渡邊・渡邉・tenyw・nabeyk123・nabeyk123-source）を一括削除
  2. 実在他人（吉田・鵜月・小池・山崎・久保・本田・田中）は引き続き架空化（鈴木・野村・石井・後藤・岡田・森・斎藤）
  3. `docs/acme_corp_spec.md` v1.2.0 で主人公を「加藤 太郎」→「わたなべ」へ全面リネーム
  4. 単体テストに「v1.2.0: 作者本人 8 表記すべてが残ること」を明示テストとして追加
- 適用範囲: ハッカソン提出物・portfolio・公開デモ・OSS 副業全般。**作者本人の名前を判定する 2 条件**: (a) 既に公開ハンドル / ブランド化されているか、(b) その名前が出ることで作者のストーリーが強化されるか。両方 yes なら実名で出す。
- 横展開判断: **昇格候補**（理由: AI 支援開発の副産物として個人ブランドに紐づける作品が増える中、「架空 vs 実名」の判断基準は再発確実。`_standards/nfr-base.md` の C-6「個人情報保護対応」周辺に「作者本人の名前判定基準」として追加候補。pdm_and_ai note 記事化候補：「ハッカソン作品で作者の名前を出すかマスクするかの判断軸」）。
- 補足: この方針転換の **代償**: v1.0〜v1.1.1 で生成した `masked_pairs.jsonl` には「加藤部長」が大量に含まれ、v1.2.0 で再処理して上書き必須。8分の処理時間と $0.09 の追加コストで済む（マスキングは Gemini 分類込みでも 484s）。早期の方針決定が辞書のべき等性とトレードオフ、というのが運用上の教訓。

### L-008: Streamlit の `st.session_state` は worker スレッドから触ると `AttributeError`（"session_id" がありません）になる
- 日付: 2026-06-23
- 症状: ローカル `localhost:8501` / 公開 Cloud Run どちらでも、入力直後の Turn 1 実行で `AttributeError: st.session_state には属性 "session_id" がありません。初期化を忘れていませんか？` が発生。`_init_state()` は呼んでいて、main スレッドで `session_state.session_id` を print すれば値が見える。
- 原因: Streamlit の `st.session_state` は **ScriptRunContext を持つスレッド（= Streamlit が起動したスクリプトランナースレッド）からしか参照できない**。本プロジェクトでは「動的 spinner（TODO-P11）」のために `run_with_dynamic_status` が `threading.Thread` で `asyncio.run(coro_factory())` を実行する設計を入れていた（メインスレッドで進捗ラベルを更新するため）。その worker スレッドの内側で `_run_single_agent` が `st.session_state.session_id` を読みに行っていたため、ScriptRunContext を持たないスレッドでの session_state 参照になり例外。Streamlit 1.36 前後で `st.session_state` のクロススレッド参照が「黙って空 dict / 警告」から「`AttributeError` で硬く落とす」に変わった（このプロジェクトでは 1.58.0 で確実に落ちる）。Day 4 後半の資料添付実装ターンでは _run_single_agent 自体は触っていなかったが、たまたまそのコードパスを通る相談（「ふるまちPayで苦しんでる」のような Vector Search が遅めの入力）でユーザーが踏んだ。
- 解決: worker スレッドに渡す前に **メインスレッド側で session_id / cache_resource をぜんぶ抜き取り、引数として closure に閉じ込める**。具体的には:
  ```python
  # NG（worker thread で session_state 参照）
  lambda: run_turn1(current_query, current_doc)
  # 内側で st.session_state.session_id, get_retrieval() を呼ぶ → AttributeError

  # OK（メインスレッドで解決して引数で渡す）
  session_id_base = st.session_state.session_id
  retrieval_svc = get_retrieval()
  lambda: run_turn1(current_query, current_doc, session_id_base, retrieval_svc)
  ```
  並せて `_run_single_agent(..., session_id_base)`, `run_turn1(..., session_id_base, retrieval)`, `run_turn2(..., session_id_base)` のシグネチャを「依存を全部受け取る」形に書き直した。worker 側は Streamlit を完全に知らない関数になる。
- 適用範囲: Streamlit + 任意の worker スレッド / asyncio.run / executor を組み合わせる全ての場面。動的進捗表示・並列 LLM 呼び出し・バックグラウンドポーリングなど、Streamlit のシングルスレッド前提を破る瞬間が境界になる。`@st.cache_resource` / `@st.cache_data` も内部で ScriptRunContext を見るため、worker からの初回呼び出しは同じ罠を踏む — **必ずメインスレッドで一度呼んで戻り値だけ持ち込む**。
- 横展開判断: **要**（理由: Streamlit を「動的進捗表示」用に少しでも非同期化した瞬間に踏む。Streamlit + LLM の組み合わせは今後も増える定番構成で、`pdm_and_ai` 系 / 副業の社内ツール開発でも再発する確度が高い。`_standards/nfr-base.md` の E 系（AI実装）か、新カテゴリ「F: フロントエンド統合」を立てて『Streamlit / Gradio 等のシングルスレッドフレームワークで worker thread を使う時は session/cache を必ずメインで解決してから渡す』として昇格候補）。
- 補足: 同じ症状が "data" の名前で出ることがあるが（"data has no attribute X"）、根本原因は同じ ScriptRunContext 不在。デバッグ時は worker 関数の中に `import streamlit as st; print(hasattr(st, 'runtime'))` などを仕込み、Streamlit 側が「自分が居る」と認識しているかを確認できる。

### L-009: 数千件規模の検索なら Vertex AI Vector Search より「Firestore + numpy + メモリ常駐」が圧倒的にコスト効率良い
- 日付: 2026-06-23
- 症状: Vector Search endpoint を Day 2 にセットアップしたあと寝かせていたつもりが、`automaticResources, minReplicas=1` で **24/7 で 1 node 維持**され、約 **¥80-100/h（月 ¥60,000 規模）** で課金継続。ハッカソン Free Credit ¥47,966 を 1 ヶ月で焼き切るペース。Day 4 着手時点 ¥0 → 20 時間で ¥2,400。
- 原因: Vertex AI Vector Search は数十万件以上のエンタープライズ検索を想定した SKU。最小構成でも「serving node 常時 1 台」がベースライン。**ハッカソン規模（2,787 pair + 46 KB = 2,833 件 × 768 次元 = 約 8MB）にはオーバースペックで、性能要件より価格モデルが先に壊れる**。
- 解決:
  1. 全 embedding（768-dim, L2 正規化済 → dot product = cos sim）を Firestore に backfill（`scripts/backfill_embeddings_to_firestore.py`、2,833 docs / コスト $0.005）
  2. アプリ起動時に `InMemoryVectorStore.load()` で全件を numpy float32 行列にロード（~15 秒、~8MB）
  3. クエリ時は `emb_matrix @ query` で全件ドットプロダクト → `argpartition` で top-k（全件 2,800 でも ~1ms）
  4. tag / topic / layer フィルタは bool マスクで `np.where(mask, sims, -inf)`
  5. RetrievalService の公開シグネチャ（`get_similar_pairs` / `get_relevant_kb`）は無改修で差し替え可能
  6. Vector Search endpoint + index を teardown して課金停止（Day 2 で書いた `--teardown` フローが効いた → L-005 の積み立て）
- 適用範囲: 「PoC / ハッカソン / 個人プロダクト / 中小規模社内ツール」のセマンティック検索全般。ベクトル件数の閾値ざっくり:
  | 件数 | 推奨 |
  |---|---|
  | ~10,000 | **Firestore + numpy 一択**（メモリ ~30MB、ロード ~30s 以内、検索 数ms） |
  | 10,000-100,000 | numpy + メモリマップ or sklearn / faiss (ローカル) |
  | 100,000~ | Vector Search / pgvector / Weaviate 等の専用基盤 |
  - ベクトル次元数も効く（768→1536 でメモリ 2 倍）。1M 件 × 768 で約 3GB なので、Cloud Run 標準 2GB 上限を超えるあたりが Firestore + numpy の物理上限。
- 横展開判断: **必ず昇格**（理由: AI 支援開発で「セマンティック検索を試したい」要件は今後も頻発。最初から VS を選ぶ罠を回避できる判断軸として `_standards/nfr-base.md` の E 系か新カテゴリ「F: AI Infra選定」に「ベクトル検索基盤の選定はまず件数で考える。10,000 件未満は Firestore + numpy、それ以上で専用基盤を検討」として昇格候補。pdm_and_ai note 記事化候補：「ハッカソン規模なら Vector Search は罠 — ベクトル件数 × 価格モデルの 1 次マッチング指針」）。
- 補足: 検索品質はほぼ同等（同じ embedding model、dot product = cos sim、L2 正規化済）。Day 3 ログの `distance=0.7551` vs 新実装の `distance=0.7356` 等、誤差は 0.02 以内で sub-second スコアリングが既に変わる範囲。再現性 (E-1) の観点では VS の方が「サービス保証された等しい計算」だが、ハッカソンには numpy で十分。Vector Search を「正答セット側」として残し、定期的に対照検証する運用も可能（が、課金停止後はそれもできないので、品質回帰は Day 3 のシナリオログを RAW スコアと突き合わせる形に縮退）。
