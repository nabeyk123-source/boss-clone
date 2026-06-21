"""Claude エクスポートデータのマスキングパイプライン本体。

仕様: `masking_pipeline_spec.md`
戦略: `docs/pii_strategy.md`

ステップ:
  1. 設定読込・べき等性ハッシュ・既処理判定
  2. conversations.json 読込
  3. 対話単位ループ
     3-1: ブラックリスト判定
     3-2: メインブランチ抽出
     3-3: ペア抽出
  4. ペア単位ループ
     4-1: PII マスク
     4-2: 辞書適用
     4-3: 未知固有名詞検出
     4-4: (任意) Gemini タグ分類
     4-5: レビューフラグ判定
     4-6: 監査ログ
  5. processing_report.md 生成

使い方:
    python scripts/masking_pipeline.py                # 通常実行（タグ分類あり）
    python scripts/masking_pipeline.py --no-classify  # タグ分類スキップ（コスト0）
    python scripts/masking_pipeline.py --test         # 最初の3対話のみ
    python scripts/masking_pipeline.py --force        # 既処理でも再実行
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from masking_lib import audit as audit_mod  # noqa: E402
from masking_lib import dictionary as dict_mod  # noqa: E402
from masking_lib import pair_extractor as pe  # noqa: E402
from masking_lib import pii as pii_mod  # noqa: E402
from masking_lib import unknown_detector as ud  # noqa: E402

RAW_JSON = ROOT / "data" / "raw" / "claude_export" / "conversations.json"
DICT_JSON = ROOT / "docs" / "masking_dictionary.json"

OUT_INTERIM = ROOT / "data" / "interim"
OUT_EXCLUDED = ROOT / "data" / "excluded"
OUT_PROCESSED = ROOT / "data" / "processed"

PATH_PAIR_EXTRACTED = OUT_INTERIM / "pair_extracted.jsonl"
PATH_BLACKLIST_HITS = OUT_EXCLUDED / "blacklist_hits.jsonl"
PATH_MASKED_PAIRS = OUT_PROCESSED / "masked_pairs.jsonl"
PATH_REVIEW_QUEUE = OUT_PROCESSED / "review_queue.jsonl"
PATH_AUDIT_LOG = OUT_PROCESSED / "audit_log.jsonl"
PATH_UNKNOWN_ENTITIES = OUT_PROCESSED / "unknown_entities.jsonl"
PATH_PROCESSING_REPORT = OUT_PROCESSED / "processing_report.md"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("masking_pipeline")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Claude エクスポートのマスキングパイプライン")
    p.add_argument("--test", action="store_true", help="最初の3対話のみ処理")
    p.add_argument("--force", action="store_true", help="既処理でも再実行")
    p.add_argument("--no-classify", action="store_true", help="Gemini タグ分類をスキップ（コスト0）")
    p.add_argument("--max-concurrency", type=int, default=8, help="Gemini 並列度（既定8）")
    return p.parse_args()


WEAK_UNKNOWN_TYPES: frozenset[str] = frozenset({"name_with_particle"})


def needs_review(pii_masks: list[dict], unknowns: list[dict], tag: str, tag_status: str) -> bool:
    """仕様 §4.9 のレビューフラグ判定（v1.2.0）。

    - PII 累計3件以上で review
    - 人名・社名候補（name_with_particle 以外）が5件以上で review
    - タグ分類が API 失敗（retry_exhausted/empty）した場合も人間レビューに回す
    """
    if sum(m.get("count", 0) for m in pii_masks) >= 3:
        return True
    strong_unknowns = [u for u in unknowns if u.get("type") not in WEAK_UNKNOWN_TYPES]
    if len(strong_unknowns) >= 5:
        return True
    if tag_status.startswith("retry_exhausted") or tag_status == "empty":
        return True
    return False


def render_report(stats: dict, out_path: Path) -> None:
    a = []
    def w(s: str = "") -> None: a.append(s)

    w("# Masking Pipeline Report")
    w("")
    w(f"- Processing ID: `{stats['processing_id']}`")
    w(f"- 実行時刻: {stats['started_at']}")
    w(f"- 完了時刻: {stats['finished_at']}")
    w(f"- 辞書バージョン: {stats['dict_version']}  (hash: `{stats['dict_hash']}`)")
    w(f"- raw ファイルハッシュ: `{stats['raw_hash']}`")
    w(f"- モード: {stats['mode']}")
    w("")
    w("## 入出力")
    w(f"- 入力対話数: {stats['total_convs']}")
    w(f"- 抽出ペア数: {stats['total_pairs']}")
    w("")
    w("## 振り分け結果")
    w(f"- masked_pairs: {stats['masked_pairs_count']}")
    w(f"- review_queue: {stats['review_queue_count']}")
    w(f"- excluded（ブラックリスト）: {stats['excluded_count']}（対話数）")
    w("")
    w("## マスキング統計")
    w(f"- PII 置換: {stats['pii_total']} 件 "
      f"(email: {stats['pii_by_type'].get('email', 0)}, "
      f"phone: {stats['pii_by_type'].get('phone_jp', 0)}, "
      f"card: {stats['pii_by_type'].get('credit_card', 0)})")
    w(f"- 辞書置換: {stats['dict_total']} 件")
    for cat, cnt in sorted(stats["dict_by_category"].items(), key=lambda x: -x[1]):
        w(f"  - {cat}: {cnt}")
    w("")
    w("## 未知固有名詞（人名・社名候補のみ、review 振り分けに利用）")
    w(f"- ユニーク数 (strong): {stats['strong_unknown_unique']}")
    w(f"- 上位10件:")
    if stats["strong_unknown_top10"]:
        for term, cnt in stats["strong_unknown_top10"]:
            w(f"  - {term} ({cnt}回)")
    else:
        w("  - (なし)")
    w("")
    w("## 参考: name_with_particle ヒット（review 振り分けには使わない、サマリのみ）")
    w(f"- ユニーク数: {stats['name_particle_unique']}")
    w(f"- 総ヒット数: {stats['name_particle_total']}")
    w("")
    w("### 人名候補集中対話 TOP10（事後レビュー用）")
    if stats["name_focus_top10"]:
        w("| # | conversation_uuid | title | hits |")
        w("|---|---|---|---:|")
        for i, e in enumerate(stats["name_focus_top10"], 1):
            w(f"| {i} | `{e['conversation_uuid']}` | {e['title']} | {e['name_with_particle_hits']} |")
    else:
        w("(なし)")
    w("")
    w("## タグ分布")
    if stats["mode_classify"]:
        for tag, cnt in stats["tag_dist"].items():
            w(f"- {tag}: {cnt}")
    else:
        w("- (このランでは --no-classify、タグ分類スキップ)")
    w("")
    w("## 処理時間・コスト")
    w(f"- 総処理時間: {stats['duration_s']:.1f} 秒")
    if stats["mode_classify"]:
        w(f"- Gemini 呼び出し: {stats['classify_calls']} 回 / 失敗 {stats['classify_failures']} 回 "
          f"(retry_exhausted: {stats['classify_retry_exhausted']}, empty: {stats['classify_empty']})")
        w(f"- classify wall-time: {stats['classify_wall_s']:.1f} 秒  "
          f"throughput: {stats['throughput_qps']:.2f} req/s")
        ls = stats["latency_stats"]
        w(f"- レイテンシ (api call): avg {ls['avg_s']*1000:.0f}ms / p50 {ls['p50_s']*1000:.0f}ms "
          f"/ p95 {ls['p95_s']*1000:.0f}ms / p99 {ls['p99_s']*1000:.0f}ms / max {ls['max_s']*1000:.0f}ms")
        w(f"- 概算入力トークン: {stats['est_input_tokens']:,} / 出力 {stats['est_output_tokens']:,}")
        w(f"- 推定コスト（参考値、Vertex AI Gemini 2.5 Flash 料金）: ${stats['est_cost_usd']:.2f}")
    else:
        w("- Gemini API: 未使用（--no-classify）")
    w("")
    w("## 出力ファイル")
    w(f"- `{PATH_MASKED_PAIRS.relative_to(ROOT).as_posix()}`")
    w(f"- `{PATH_REVIEW_QUEUE.relative_to(ROOT).as_posix()}`")
    w(f"- `{PATH_BLACKLIST_HITS.relative_to(ROOT).as_posix()}`")
    w(f"- `{PATH_AUDIT_LOG.relative_to(ROOT).as_posix()}`")
    w(f"- `{PATH_UNKNOWN_ENTITIES.relative_to(ROOT).as_posix()}`")
    w(f"- `{PATH_PAIR_EXTRACTED.relative_to(ROOT).as_posix()}`")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(a) + "\n", encoding="utf-8", newline="\n")


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    log.info("[load] dictionary")
    dictionary = dict_mod.load_dictionary(DICT_JSON)
    dh = dict_mod.dict_hash(dictionary)
    fh = dict_mod.file_hash(RAW_JSON)
    processing_id = f"{fh}_{dh}"
    log.info("processing_id=%s", processing_id)

    if not args.force and audit_mod.is_already_processed(PATH_AUDIT_LOG, processing_id):
        log.warning("既に同じ processing_id で処理済みです。--force で強制再実行できます。")
        return 0

    pii_compiled = pii_mod.compile_pii_patterns(dictionary.get("pii_patterns") or {})
    blacklist = (dictionary.get("blacklist_topics") or {}).get("patterns") or []
    known_terms = dict_mod.collect_known_terms(dictionary)

    log.info("[load] conversations.json")
    with RAW_JSON.open("r", encoding="utf-8") as f:
        convs = json.load(f)
    if args.test:
        convs = convs[:3]
        log.info("--test モード: 先頭 %d 対話のみ処理", len(convs))

    # 出力をクリアして書き始める
    OUT_INTERIM.mkdir(parents=True, exist_ok=True)
    OUT_EXCLUDED.mkdir(parents=True, exist_ok=True)
    OUT_PROCESSED.mkdir(parents=True, exist_ok=True)

    # 集計用カウンタ
    excluded_count = 0
    pii_by_type: Counter[str] = Counter()
    dict_by_category: Counter[str] = Counter()
    unknown_counter: Counter[str] = Counter()
    unknown_examples: dict[str, dict] = {}
    pair_extracted_count = 0
    # 対話単位の name_with_particle ヒット累計（人名候補集中対話の発見用）
    name_particle_per_conv: Counter[str] = Counter()
    conv_title: dict[str, str] = {c.get("uuid"): (c.get("name") or "(no title)") for c in convs}

    # 1次パスで pair_extracted / blacklist_hits を書き出しつつ、masking 済みペアを集める
    masked_pairs_buffer: list[dict] = []
    audit_buffer: list[dict] = []

    with audit_mod.JsonlWriter(PATH_PAIR_EXTRACTED) as wp, \
         audit_mod.JsonlWriter(PATH_BLACKLIST_HITS) as wb:

        for conv in convs:
            conv_uuid = conv.get("uuid")
            is_bl, term = pe.is_blacklisted(conv, blacklist)
            if is_bl:
                wb.write({
                    "conversation_uuid": conv_uuid,
                    "title": conv.get("name"),
                    "blacklist_hit": term,
                    "message_count": len(conv.get("chat_messages") or []),
                })
                excluded_count += 1
                continue

            msgs = conv.get("chat_messages") or []
            branch = pe.extract_main_branch(msgs)
            pairs = pe.extract_pairs(branch)

            for idx, pair in enumerate(pairs):
                # pair_extracted は素の状態でも記録（チェック用）
                wp.write({
                    "conversation_uuid": conv_uuid,
                    "pair_index": idx,
                    "human_uuid": pair["human_uuid"],
                    "assistant_uuid": pair["assistant_uuid"],
                    "created_at": pair["created_at"],
                    "human_chars": len(pair["human_text"] or ""),
                    "assistant_chars": len(pair["assistant_text"] or ""),
                })
                pair_extracted_count += 1

                # 4-1: PII マスク（human/assistant 両方）
                h_masked, h_pii = pii_mod.mask_pii(pair["human_text"] or "", pii_compiled)
                a_masked, a_pii = pii_mod.mask_pii(pair["assistant_text"] or "", pii_compiled)

                # 4-2: 辞書適用
                h_masked, h_repls = dict_mod.apply_dictionary(h_masked, dictionary)
                a_masked, a_repls = dict_mod.apply_dictionary(a_masked, dictionary)

                pii_masks = h_pii + a_pii
                for m in pii_masks:
                    pii_by_type[m["type"]] += m["count"]

                repls = h_repls + a_repls
                for r in repls:
                    dict_by_category[r["category"]] += r["count"]

                # 4-3: 未知固有名詞検出（マスク前の本文に対して、保護対象を除いて検出）
                unknowns_h = ud.detect_unknown_entities(pair["human_text"] or "", known_terms)
                unknowns_a = ud.detect_unknown_entities(pair["assistant_text"] or "", known_terms)
                unknowns = unknowns_h + unknowns_a
                for u in unknowns:
                    unknown_counter[u["term"]] += 1
                    unknown_examples.setdefault(u["term"], u)
                    if u.get("type") == "name_with_particle":
                        name_particle_per_conv[conv_uuid] += 1

                masked_pairs_buffer.append({
                    "conversation_uuid": conv_uuid,
                    "pair_index": idx,
                    "created_at": pair["created_at"],
                    "human_uuid": pair["human_uuid"],
                    "assistant_uuid": pair["assistant_uuid"],
                    "human_text": h_masked,
                    "assistant_text": a_masked,
                    "_pii_masks": pii_masks,
                    "_unknowns": unknowns,
                    "_dict_categories": sorted({r["category"] for r in repls}),
                    "_dict_count": sum(r["count"] for r in repls),
                })

    log.info("[stage1] excluded=%d, extracted=%d pairs", excluded_count, pair_extracted_count)

    # 4-4: タグ分類
    tag_dist: Counter[str] = Counter()
    classify_calls = 0
    classify_failures = 0
    classify_retry_exhausted = 0
    classify_empty = 0
    est_input_tokens = 0
    est_output_tokens = 0
    tags: list[tuple[str, str, float]] = []
    latency_stats: dict[str, float] = {"avg_s": 0.0, "max_s": 0.0, "p50_s": 0.0, "p95_s": 0.0, "p99_s": 0.0, "total_s": 0.0}
    classify_wall_s = 0.0

    if not args.no_classify and masked_pairs_buffer:
        log.info("[stage2] classify %d pairs via Vertex AI Gemini (concurrency=%d)",
                 len(masked_pairs_buffer), args.max_concurrency)
        from masking_lib.classifier import ClassifyConfig, classify_many

        cfg = ClassifyConfig(max_concurrency=args.max_concurrency)

        def progress(done: int, total: int) -> None:
            log.info("classify progress: %d/%d", done, total)

        human_texts = [p["human_text"] for p in masked_pairs_buffer]
        t_classify = time.perf_counter()
        tags = asyncio.run(classify_many(human_texts, cfg, progress_cb=progress))
        classify_wall_s = time.perf_counter() - t_classify

        classify_calls = len(tags)
        classify_failures = sum(1 for _, st, _ in tags if st != "ok")
        classify_retry_exhausted = sum(1 for _, st, _ in tags if st.startswith("retry_exhausted"))
        classify_empty = sum(1 for _, st, _ in tags if st == "empty")
        for tag, _, _ in tags:
            tag_dist[tag] += 1

        latencies = sorted(lat for _, _, lat in tags)
        if latencies:
            latency_stats["total_s"] = sum(latencies)
            latency_stats["avg_s"] = latency_stats["total_s"] / len(latencies)
            latency_stats["max_s"] = latencies[-1]
            latency_stats["p50_s"] = latencies[len(latencies) // 2]
            latency_stats["p95_s"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            latency_stats["p99_s"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))]

        # ざっくり概算（500 chars ≈ 333 tokens 程度の日本語見積もり）
        for human_text in human_texts:
            est_input_tokens += min(len(human_text) // 1.5, 333)
        est_input_tokens = int(est_input_tokens)
        est_output_tokens = classify_calls * 5
    else:
        log.info("[stage2] classify skipped (--no-classify)")
        tags = [("保留", "skipped", 0.0)] * len(masked_pairs_buffer)

    # 4-5/4-6: レビュー振り分け + 監査ログ
    log.info("[stage3] route + write outputs")
    with audit_mod.JsonlWriter(PATH_MASKED_PAIRS) as wm, \
         audit_mod.JsonlWriter(PATH_REVIEW_QUEUE) as wr, \
         audit_mod.JsonlWriter(PATH_AUDIT_LOG) as wa:

        for pair, (tag, tag_status, _lat) in zip(masked_pairs_buffer, tags):
            had_pii = bool(pair["_pii_masks"])
            pii_types = sorted({m["type"] for m in pair["_pii_masks"]})
            review = needs_review(pair["_pii_masks"], pair["_unknowns"], tag, tag_status)
            destination = "review_queue" if review else "masked_pairs"

            final_entry = {
                "processing_id": processing_id,
                "conversation_uuid": pair["conversation_uuid"],
                "pair_index": pair["pair_index"],
                "created_at": pair["created_at"],
                "human_uuid": pair["human_uuid"],
                "assistant_uuid": pair["assistant_uuid"],
                "human_text": pair["human_text"],
                "assistant_text": pair["assistant_text"],
                "tag": tag,
                "metadata": {
                    "had_pii": had_pii,
                    "pii_types": pii_types,
                    "dictionary_categories_applied": pair["_dict_categories"],
                    "dictionary_replacement_count": pair["_dict_count"],
                    "unknown_entities_count": len(pair["_unknowns"]),
                    "tag_status": tag_status,
                },
            }
            (wr if review else wm).write(final_entry)

            wa.write({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "processing_id": processing_id,
                "conversation_uuid": pair["conversation_uuid"],
                "pair_index": pair["pair_index"],
                "human_uuid": pair["human_uuid"],
                "assistant_uuid": pair["assistant_uuid"],
                "dictionary_replacements": [
                    {"category": r["category"], "source": "[REDACTED]", "target": "[REDACTED]", "count": r["count"]}
                    # 監査ログには source/target そのものを残さない（辞書情報そのものが機密のため）
                    for r in []  # 集計値を別フィールドで持つので、明細は categories と count のみで足りる
                ],
                "dictionary_categories": pair["_dict_categories"],
                "dictionary_replacement_count": pair["_dict_count"],
                "pii_masks": [{"type": m["type"], "count": m["count"]} for m in pair["_pii_masks"]],
                "unknown_entities_detected": len(pair["_unknowns"]),
                "tag": tag,
                "tag_status": tag_status,
                "review_required": review,
                "destination": destination,
            })

        masked_pairs_count = wm.count
        review_queue_count = wr.count

    # 未知固有名詞の集計出力
    with audit_mod.JsonlWriter(PATH_UNKNOWN_ENTITIES) as wu:
        for term, cnt in unknown_counter.most_common():
            ex = unknown_examples.get(term, {})
            wu.write({
                "term": term,
                "count": cnt,
                "type": ex.get("type"),
                "sample_context": ex.get("context"),
                "decision": "pending",
            })

    duration = time.perf_counter() - t0
    finished_at = datetime.now(timezone.utc).isoformat()

    # コスト概算（Vertex AI Gemini 2.5 Flash の標準価格を参考に：$0.30 / 1M 入力, $2.50 / 1M 出力 — 為替・改定で変動）
    est_cost = (est_input_tokens / 1_000_000) * 0.30 + (est_output_tokens / 1_000_000) * 2.50

    # 「真の unknown」(name_with_particle 除く) を集計
    strong_unknown_counter: Counter[str] = Counter()
    for term, cnt in unknown_counter.items():
        ex = unknown_examples.get(term, {})
        if ex.get("type") != "name_with_particle":
            strong_unknown_counter[term] = cnt
    name_particle_unique = sum(1 for term in unknown_counter
                               if unknown_examples.get(term, {}).get("type") == "name_with_particle")
    name_particle_total = sum(unknown_counter[term] for term in unknown_counter
                              if unknown_examples.get(term, {}).get("type") == "name_with_particle")

    # 人名候補集中対話 TOP10
    name_focus_top10 = [
        {
            "conversation_uuid": uuid,
            "title": conv_title.get(uuid, "(no title)"),
            "name_with_particle_hits": cnt,
        }
        for uuid, cnt in name_particle_per_conv.most_common(10)
    ]

    stats = {
        "processing_id": processing_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "dict_version": (dictionary.get("_meta") or {}).get("version", "?"),
        "dict_hash": dh,
        "raw_hash": fh,
        "mode": "test" if args.test else "full",
        "mode_classify": not args.no_classify,
        "total_convs": len(convs),
        "total_pairs": pair_extracted_count,
        "masked_pairs_count": masked_pairs_count,
        "review_queue_count": review_queue_count,
        "excluded_count": excluded_count,
        "pii_total": sum(pii_by_type.values()),
        "pii_by_type": dict(pii_by_type),
        "dict_total": sum(dict_by_category.values()),
        "dict_by_category": dict(dict_by_category),
        "unknown_unique": len(unknown_counter),
        "unknown_top10": unknown_counter.most_common(10),
        "strong_unknown_unique": len(strong_unknown_counter),
        "strong_unknown_top10": strong_unknown_counter.most_common(10),
        "name_particle_unique": name_particle_unique,
        "name_particle_total": name_particle_total,
        "name_focus_top10": name_focus_top10,
        "tag_dist": dict(tag_dist),
        "classify_calls": classify_calls,
        "classify_failures": classify_failures,
        "classify_retry_exhausted": classify_retry_exhausted,
        "classify_empty": classify_empty,
        "classify_wall_s": classify_wall_s,
        "latency_stats": latency_stats,
        "throughput_qps": (classify_calls / classify_wall_s) if classify_wall_s > 0 else 0.0,
        "est_input_tokens": est_input_tokens,
        "est_output_tokens": est_output_tokens,
        "est_cost_usd": est_cost,
        "duration_s": duration,
    }
    render_report(stats, PATH_PROCESSING_REPORT)
    log.info("[done] %.1fs / report=%s", duration, PATH_PROCESSING_REPORT.relative_to(ROOT))
    log.info("masked=%d  review=%d  excluded=%d  unknown_unique=%d",
             masked_pairs_count, review_queue_count, excluded_count, len(unknown_counter))
    return 0


if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(run(args))
    except KeyboardInterrupt:
        log.warning("中断されました")
        raise SystemExit(130)
