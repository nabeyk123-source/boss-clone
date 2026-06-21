"""masked_pairs.jsonl → 要約 → トピック分類 → 判断種別 → 埋め込み → Firestore + VS JSONL。

仕様: `docs/schema_spec.md` §5（パイプライン）

使い方:
    # 10件で smoke、Firestore/VS は触らない（dry-run）
    python scripts/embedding_pipeline.py --test --sample=10 --dry-run

    # 10件で smoke、Firestore に投入 + VS 用 JSONL 書き出し
    python scripts/embedding_pipeline.py --test --sample=10

    # フル実行（2,848件）
    python scripts/embedding_pipeline.py --force
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from embedding_lib import decision_classifier as dec_mod  # noqa: E402
from embedding_lib import embedder as emb_mod  # noqa: E402
from embedding_lib import summarizer as sum_mod  # noqa: E402
from embedding_lib import topic_classifier as topic_mod  # noqa: E402
from embedding_lib import vector_search_writer as vsw  # noqa: E402

# 入出力パス
SRC_MASKED = ROOT / "data" / "processed" / "masked_pairs.jsonl"
SRC_REVIEW = ROOT / "data" / "processed" / "review_queue.jsonl"

OUT_DIR = ROOT / "data" / "processed" / "embedding"
PATH_PAIRS_DOCS = OUT_DIR / "pairs_docs.jsonl"           # Firestore docs（dry-run 時にローカル出力）
PATH_VS_JSONL = OUT_DIR / "pair_summaries.jsonl"          # Vector Search 用
PATH_RUN_REPORT = OUT_DIR / "embedding_report.md"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("embedding_pipeline")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="masked_pairs → 埋め込み → Firestore + Vector Search")
    p.add_argument("--test", action="store_true", help="test モード（短時間想定）")
    p.add_argument("--sample", type=int, default=0, help="先頭 N 件だけ処理（0 で全件）")
    p.add_argument("--include-review-queue", action="store_true", help="review_queue.jsonl も含める")
    p.add_argument("--dry-run", action="store_true", help="Firestore に書き込まず JSONL だけ出力")
    p.add_argument("--force", action="store_true", help="既存出力を上書き")
    p.add_argument("--max-concurrency", type=int, default=4, help="Gemini/Embedding の並列度（既定4）")
    return p.parse_args()


def short_uuid(uuid_str: str) -> str:
    return (uuid_str or "")[:8] if uuid_str else "noconv"


def make_doc_id(processing_id: str, conv_uuid: str, pair_index: int) -> str:
    return f"{processing_id}_{short_uuid(conv_uuid)}_{pair_index:04d}"


def load_input_pairs(args: argparse.Namespace) -> list[dict]:
    if not SRC_MASKED.exists():
        raise FileNotFoundError(f"{SRC_MASKED} が見つかりません。先に masking_pipeline.py を実行してください")
    pairs: list[dict] = []
    with SRC_MASKED.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                d["_source"] = "masked_pairs"
                pairs.append(d)
    if args.include_review_queue and SRC_REVIEW.exists():
        with SRC_REVIEW.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    d["_source"] = "review_queue"
                    pairs.append(d)
    if args.sample > 0:
        pairs = pairs[: args.sample]
    return pairs


def derive_processing_id(pairs: list[dict]) -> str:
    """ペア内の processing_id を採用。空なら masked_pairs.jsonl の hash[:16]。"""
    for p in pairs:
        pid = p.get("processing_id")
        if pid:
            return pid
    if SRC_MASKED.exists():
        h = hashlib.sha256(SRC_MASKED.read_bytes()).hexdigest()[:16]
        return f"src_{h}"
    return "unknown"


def run_stage(name: str, coro):
    """stage を実行し、所要秒数をログに残す。"""
    log.info("[stage] %s start", name)
    t = time.perf_counter()
    result = asyncio.run(coro)
    log.info("[stage] %s done (%.1fs)", name, time.perf_counter() - t)
    return result


def render_report(stats: dict, path: Path) -> None:
    a = []
    def w(s: str = "") -> None: a.append(s)
    w("# Embedding Pipeline Report")
    w("")
    w(f"- Processing ID: `{stats['processing_id']}`")
    w(f"- 実行時刻: {stats['started_at']}")
    w(f"- 完了時刻: {stats['finished_at']}")
    w(f"- モード: {stats['mode']}  / dry-run: {stats['dry_run']}")
    w("")
    w("## 入出力")
    w(f"- 入力ペア数: {stats['n_input']}")
    w(f"- 埋め込み成功: {stats['n_embedded']} / 失敗: {stats['n_embed_failed']}")
    w(f"- Firestore 投入: {stats['n_firestore_written']}")
    w(f"- VS JSONL 行数: {stats['n_vs_jsonl']}")
    w("")
    w("## ステージ別 所要時間")
    for k, v in stats["stage_times"].items():
        w(f"- {k}: {v:.1f}s")
    w("")
    w("## Gemini / Embedding 統計")
    w(f"- 要約: ok={stats['sum_ok']} retry_exhausted={stats['sum_retry']} empty={stats['sum_empty']}")
    w(f"- トピック: rule_based={stats['topic_rule']} ok={stats['topic_ok']} retry_exhausted={stats['topic_retry']} empty={stats['topic_empty']}")
    w(f"- 判断種別: ok={stats['dec_ok']} retry_exhausted={stats['dec_retry']} empty={stats['dec_empty']}")
    w(f"- 埋め込みバッチ: {stats['embed_batches']} 件 (失敗: {stats['embed_batch_failed']})")
    w("")
    w("## タグ分布（topic_tags フラット）")
    for t, c in sorted(stats["topic_freq"].items(), key=lambda x: -x[1])[:15]:
        w(f"- {t}: {c}")
    w("")
    w("## 判断種別 分布")
    for d, c in sorted(stats["decision_freq"].items(), key=lambda x: -x[1]):
        w(f"- {d}: {c}")
    w("")
    w("## 出力ファイル")
    w(f"- Firestore docs JSONL（dry-run用）: `{PATH_PAIRS_DOCS.relative_to(ROOT).as_posix()}`")
    w(f"- Vector Search JSONL: `{PATH_VS_JSONL.relative_to(ROOT).as_posix()}`")
    w(f"- 本レポート: `{path.relative_to(ROOT).as_posix()}`")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(a) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    stage_times: dict[str, float] = {}

    log.info("[load] %s", SRC_MASKED.relative_to(ROOT))
    pairs = load_input_pairs(args)
    log.info("loaded %d pairs (sample=%s, include_review=%s)",
             len(pairs), args.sample or "all", args.include_review_queue)
    if not pairs:
        log.error("入力 0 件です。終了")
        return 1

    processing_id = derive_processing_id(pairs)
    log.info("processing_id=%s", processing_id)

    # ===== Step 2: 要約 =====
    t = time.perf_counter()
    sum_cfg = sum_mod.SummarizeConfig(max_concurrency=args.max_concurrency)
    summaries = run_stage(
        "summarize",
        sum_mod.summarize_many(
            pairs,
            cfg=sum_cfg,
            progress_cb=lambda d, total: log.info("summarize %d/%d", d, total),
        ),
    )
    stage_times["summarize_s"] = time.perf_counter() - t

    # ===== Step 3: トピック =====
    t = time.perf_counter()
    topic_cfg = topic_mod.TopicConfig(max_concurrency=args.max_concurrency)
    summary_texts = [s for s, _, _ in summaries]
    topic_results = run_stage(
        "topic_classify",
        topic_mod.classify_topics_many(
            summary_texts,
            cfg=topic_cfg,
            progress_cb=lambda d, total: log.info("topic %d/%d (LLM)", d, total),
        ),
    )
    stage_times["topic_s"] = time.perf_counter() - t

    # ===== Step 4: 判断種別 =====
    t = time.perf_counter()
    dec_cfg = dec_mod.DecisionConfig(max_concurrency=args.max_concurrency)
    decision_results = run_stage(
        "decision_classify",
        dec_mod.classify_decisions_many(
            summary_texts,
            cfg=dec_cfg,
            progress_cb=lambda d, total: log.info("decision %d/%d", d, total),
        ),
    )
    stage_times["decision_s"] = time.perf_counter() - t

    # ===== Step 5: 埋め込み =====
    t = time.perf_counter()
    emb_cfg = emb_mod.EmbedConfig(max_concurrency=args.max_concurrency)
    vectors, batch_meta = run_stage(
        "embed",
        emb_mod.embed_many(
            summary_texts,
            cfg=emb_cfg,
            progress_cb=lambda d, total: log.info("embed %d/%d", d, total),
        ),
    )
    stage_times["embed_s"] = time.perf_counter() - t

    n_embedded = sum(1 for v in vectors if v is not None)
    n_embed_failed = sum(1 for v in vectors if v is None)

    # ===== Step 6: Firestore docs 組み立て =====
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    firestore_docs: list[tuple[str, dict]] = []
    vs_datapoints: list[dict] = []
    topic_freq: dict[str, int] = {}
    decision_freq: dict[str, int] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for pair, (summary, sum_status, _), (topic_tags, topic_status, _), (dec_type, dec_status, _), vec in zip(
        pairs, summaries, topic_results, decision_results, vectors
    ):
        conv_uuid = pair.get("conversation_uuid", "noconv")
        pair_index = int(pair.get("pair_index", 0))
        doc_id = make_doc_id(processing_id, conv_uuid, pair_index)

        for t_ in topic_tags:
            topic_freq[t_] = topic_freq.get(t_, 0) + 1
        decision_freq[dec_type] = decision_freq.get(dec_type, 0) + 1

        meta = pair.get("metadata") or {}
        doc = {
            "processing_id": processing_id,
            "conversation_uuid": conv_uuid,
            "pair_index": pair_index,
            "human_text": pair.get("human_text", ""),
            "assistant_text": pair.get("assistant_text", ""),
            "summary": summary,
            "tag": pair.get("tag", "保留"),
            "topic_tags": topic_tags,
            "decision_type": dec_type,
            "created_at": pair.get("created_at"),
            "ingested_at": now_iso,
            "had_pii": meta.get("had_pii", False),
            "pii_types": meta.get("pii_types", []),
            "dictionary_categories": meta.get("dictionary_categories_applied", []),
            "dictionary_replacement_count": meta.get("dictionary_replacement_count", 0),
            "vector_id": doc_id,
            "embedding_model": emb_mod.EMBEDDING_MODEL,
            "source": pair.get("_source", "masked_pairs"),
            "needs_review": (meta.get("tag_status") or "").startswith("retry_exhausted"),
            "retry_exhausted": (meta.get("tag_status") or "").startswith("retry_exhausted"),
            "_pipeline_status": {
                "summary": sum_status,
                "topic": topic_status,
                "decision": dec_status,
                "embed": "ok" if vec is not None else "failed",
            },
        }
        firestore_docs.append((doc_id, doc))

        if vec is not None:
            vs_datapoints.append(vsw.build_pair_datapoint(
                doc_id=doc_id,
                embedding=vec,
                tag=doc["tag"],
                topic_tags=topic_tags,
                decision_type=dec_type,
                created_at_iso=doc.get("created_at"),
            ))

    # ===== ローカルに必ず保存（dry-run でなくても残しておく） =====
    PATH_PAIRS_DOCS.parent.mkdir(parents=True, exist_ok=True)
    with PATH_PAIRS_DOCS.open("w", encoding="utf-8", newline="\n") as f:
        for doc_id, doc in firestore_docs:
            f.write(json.dumps({"_id": doc_id, **doc}, ensure_ascii=False) + "\n")
    n_vs_jsonl = vsw.write_index_jsonl(PATH_VS_JSONL, vs_datapoints)
    log.info("local jsonl: pairs=%d vs=%d", len(firestore_docs), n_vs_jsonl)

    # ===== Step 7: Firestore 投入（dry-run でなければ） =====
    n_firestore_written = 0
    if args.dry_run:
        log.info("[dry-run] Firestore 投入はスキップ")
    else:
        from embedding_lib import firestore_writer as fw
        t = time.perf_counter()
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        log.info("[stage] firestore_write start (project=%s, collection=pairs)", project)
        client = fw.get_client(project)
        n_firestore_written = fw.batch_write(client, "pairs", firestore_docs)
        stage_times["firestore_s"] = time.perf_counter() - t
        log.info("[stage] firestore_write done (%.1fs, %d docs)", stage_times["firestore_s"], n_firestore_written)

    # ===== 集計 =====
    sum_ok = sum(1 for _, st, _ in summaries if st == "ok")
    sum_retry = sum(1 for _, st, _ in summaries if st.startswith("retry_exhausted"))
    sum_empty = sum(1 for _, st, _ in summaries if st == "empty")

    topic_rule = sum(1 for _, st, _ in topic_results if st == "rule_based")
    topic_ok = sum(1 for _, st, _ in topic_results if st == "ok")
    topic_retry = sum(1 for _, st, _ in topic_results if st.startswith("retry_exhausted"))
    topic_empty = sum(1 for _, st, _ in topic_results if st == "empty")

    dec_ok = sum(1 for _, st, _ in decision_results if st == "ok")
    dec_retry = sum(1 for _, st, _ in decision_results if st.startswith("retry_exhausted"))
    dec_empty = sum(1 for _, st, _ in decision_results if st == "empty")

    embed_batches = len(batch_meta)
    embed_batch_failed = sum(1 for st, _ in batch_meta if st != "ok")

    finished_at = datetime.now(timezone.utc).isoformat()
    stats = {
        "processing_id": processing_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": "test" if args.test else "full",
        "dry_run": args.dry_run,
        "n_input": len(pairs),
        "n_embedded": n_embedded,
        "n_embed_failed": n_embed_failed,
        "n_firestore_written": n_firestore_written,
        "n_vs_jsonl": n_vs_jsonl,
        "stage_times": stage_times,
        "sum_ok": sum_ok, "sum_retry": sum_retry, "sum_empty": sum_empty,
        "topic_rule": topic_rule, "topic_ok": topic_ok, "topic_retry": topic_retry, "topic_empty": topic_empty,
        "dec_ok": dec_ok, "dec_retry": dec_retry, "dec_empty": dec_empty,
        "embed_batches": embed_batches,
        "embed_batch_failed": embed_batch_failed,
        "topic_freq": topic_freq,
        "decision_freq": decision_freq,
        "total_s": time.perf_counter() - t0,
    }
    render_report(stats, PATH_RUN_REPORT)
    log.info("[done] %.1fs  embedded=%d  firestore=%d  vs_jsonl=%d",
             stats["total_s"], n_embedded, n_firestore_written, n_vs_jsonl)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log.warning("中断")
        raise SystemExit(130)
