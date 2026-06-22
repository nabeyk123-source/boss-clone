"""RetrievalService 動作確認。

サンプルクエリ3件で:
- get_similar_pairs (pair_summaries_v1) の結果
- get_relevant_kb (Firestore fallback、acme_kb endpoint 未デプロイ時)
- embedding キャッシュが効くこと
- レイテンシ
を確認。
"""
from __future__ import annotations

import asyncio
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

from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402

QUERIES = [
    "来月、新機能リリースしたいです",
    "kabe の設計判断について教えて",
    "セキュリティレビューが必要な判断",
]


async def main() -> int:
    svc = RetrievalService()
    print("[warmup] embed 1回目（cold start）")
    t0 = time.perf_counter()
    _ = await svc.embed_query("warmup")
    print(f"  cold start: {(time.perf_counter() - t0)*1000:.0f}ms")

    print()
    for i, q in enumerate(QUERIES, 1):
        print(f"=== Q{i}: {q} ===")
        # similar pairs
        t = time.perf_counter()
        pairs = await svc.get_similar_pairs(q, tag_filter=["OK", "NG"], top_k=3)
        t_pairs = (time.perf_counter() - t) * 1000
        print(f"  get_similar_pairs (top-3, tag_filter=[OK,NG]): {t_pairs:.0f}ms, hits={len(pairs)}")
        for j, p in enumerate(pairs, 1):
            sm = (p.doc.get("summary") or "")[:80]
            tag = p.doc.get("tag", "?")
            dec = p.doc.get("decision_type", "?")
            print(f"    #{j} d={p.distance:.4f} [{tag}/{dec}] {sm}")

        # KB layer fallback
        t = time.perf_counter()
        kb_l4 = await svc.get_relevant_kb(q, layer_filter="L4_implicit_knowledge", top_k=3)
        t_kb = (time.perf_counter() - t) * 1000
        print(f"  get_relevant_kb (L4, top-3): {t_kb:.0f}ms, hits={len(kb_l4)}")
        for j, k in enumerate(kb_l4, 1):
            title = (k.doc.get("title") or "")[:60]
            print(f"    #{j} d={k.distance:.4f} {title}")

        # 2回目は embed cache が効くか確認
        t = time.perf_counter()
        _ = await svc.get_similar_pairs(q, top_k=3)
        t_cached = (time.perf_counter() - t) * 1000
        print(f"  get_similar_pairs (cached embed): {t_cached:.0f}ms")
        print()

    print("=== 評価 ===")
    print(f"  embed cache サイズ: {len(svc._embed_cache)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
