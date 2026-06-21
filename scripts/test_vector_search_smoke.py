"""Vector Search Phase 1 検索動作確認（5サンプルクエリ）。

仕様: 各クエリで top-5 取得、類似度スコアとレイテンシ、Firestore で summary を引いて表示。
評価観点:
  - 類似度（DOT_PRODUCT_DISTANCE: 大きいほど近い、概ね正規化前提で 0.6+ が良）
  - 直感的に「似てる」か（summary を目視）
  - レイテンシ 500ms 以下が望ましい
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

STATE_FILE = ROOT / "scripts" / "test_vs_setup.state.json"

QUERIES: list[str] = [
    "録音アプリの開発",
    "議事録の保存方法",
    "マイク設定のエラー",
    "話者特定機能",
    "APIモデルの選択",
]

TOP_K = 5


def load_state() -> dict:
    if not STATE_FILE.exists():
        raise FileNotFoundError(
            f"{STATE_FILE} が見つかりません。先に scripts/test_vs_setup.py --setup を実行してください"
        )
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


async def _embed_query(text: str) -> list[float]:
    from embedding_lib import embedder as emb_mod
    cfg = emb_mod.EmbedConfig(max_concurrency=1, batch_size=1)
    vectors, _ = await emb_mod.embed_many([text], cfg=cfg)
    if not vectors or vectors[0] is None:
        raise RuntimeError("query embedding failed")
    return vectors[0]


def fetch_summaries(doc_ids: list[str]) -> dict[str, dict]:
    """Firestore の pairs コレクションから summary を引く。"""
    from google.cloud import firestore
    client = firestore.Client(project=PROJECT, database="(default)")
    out: dict[str, dict] = {}
    for did in doc_ids:
        snap = client.collection("pairs").document(did).get()
        if snap.exists:
            out[did] = snap.to_dict()
    return out


def main() -> int:
    state = load_state()
    info = state.get("pair_summaries")
    if not info:
        print("[error] state に pair_summaries が無い。teardown 済みの可能性")
        return 1

    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT, location=LOCATION)
    endpoint = aiplatform.MatchingEngineIndexEndpoint(info["endpoint_resource"])

    results_summary: list[dict] = []
    print(f"=== Vector Search smoke (top-{TOP_K}, deployed_id={info['deployed_index_id']}) ===\n")

    for q_index, q in enumerate(QUERIES, 1):
        print(f"--- Q{q_index}: {q} ---")
        t_emb = time.perf_counter()
        vec = asyncio.run(_embed_query(q))
        embed_ms = (time.perf_counter() - t_emb) * 1000

        t_search = time.perf_counter()
        results = endpoint.find_neighbors(
            deployed_index_id=info["deployed_index_id"],
            queries=[vec],
            num_neighbors=TOP_K,
        )
        search_ms = (time.perf_counter() - t_search) * 1000
        neighbors = results[0] if results else []

        doc_ids = [n.id for n in neighbors]
        details = fetch_summaries(doc_ids)
        distances = [n.distance for n in neighbors]

        print(f"  embed={embed_ms:.0f}ms  search={search_ms:.0f}ms  total={embed_ms + search_ms:.0f}ms  hits={len(neighbors)}")
        for i, n in enumerate(neighbors, 1):
            d = details.get(n.id, {})
            sm = (d.get("summary") or "")[:140]
            tags = d.get("topic_tags", [])
            dec = d.get("decision_type", "?")
            print(f"  #{i}  d={n.distance:.4f}  [{dec}|{','.join(tags)}]  {sm}")
        print()

        results_summary.append({
            "query": q,
            "embed_ms": embed_ms,
            "search_ms": search_ms,
            "total_ms": embed_ms + search_ms,
            "hits": len(neighbors),
            "top_distance": distances[0] if distances else None,
            "median_distance": sorted(distances)[len(distances) // 2] if distances else None,
        })

    print("=== 評価サマリ ===")
    over_500ms = sum(1 for r in results_summary if r["total_ms"] > 500)
    weak_top = sum(1 for r in results_summary if r["top_distance"] is not None and r["top_distance"] < 0.6)
    print(f"クエリ数: {len(results_summary)}")
    print(f"top distance < 0.6 のクエリ: {weak_top} / {len(results_summary)}")
    print(f"レイテンシ > 500ms のクエリ: {over_500ms} / {len(results_summary)}")
    print(f"top distance 平均: {sum((r['top_distance'] or 0) for r in results_summary) / len(results_summary):.4f}")
    print(f"レイテンシ平均: {sum(r['total_ms'] for r in results_summary) / len(results_summary):.0f} ms")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
