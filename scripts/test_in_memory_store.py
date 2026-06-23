"""InMemoryVectorStore の動作確認 + Vector Search との結果比較。

LLM 呼ばずに、Firestore ロードと top-k 検索だけ確認。
ベクトル一致性は単体テスト（合成データ）で。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

for s in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from boss_clone_lib.retrieval.in_memory_store import InMemoryVectorStore  # noqa: E402


def main() -> int:
    passed = failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  ok   {name}  {detail}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    # ---------- 1. 合成データ単体テスト ----------
    print("[unit: 合成データ]")
    store = InMemoryVectorStore()
    # 直接 numpy で詰める
    np.random.seed(42)
    fake = np.random.randn(10, 768).astype(np.float32)
    fake /= np.linalg.norm(fake, axis=1, keepdims=True)  # L2 正規化
    store.pair_ids = [f"p{i}" for i in range(10)]
    store.pair_embeddings = fake
    store.pair_metadata = [
        {"tag": "OK" if i < 5 else "NG", "summary": f"item {i}"} for i in range(10)
    ]
    store.kb_ids = [f"k{i}" for i in range(3)]
    store.kb_embeddings = np.copy(fake[:3])
    store.kb_metadata = [
        {"layer": "L1_principles" if i == 0 else "L2_regulations", "title": f"kb{i}"} for i in range(3)
    ]
    store._loaded = True

    # クエリ = item 0 と同一 → top-1 が p0
    q = fake[0]
    res = store.search_pairs(q, top_k=3)
    check("top-1 = p0 (自己一致)", res and res[0]["id"] == "p0",
          f"got {[r['id'] for r in res]}")
    check("top-1 distance ≈ 1.0", abs(res[0]["distance"] - 1.0) < 1e-4,
          f"got {res[0]['distance']:.4f}")

    # tag_filter
    res = store.search_pairs(q, top_k=10, tag_filter=["OK"])
    check("tag_filter=OK で 5 件以下", len(res) <= 5)
    check("tag_filter=OK で全件 tag==OK", all(r["doc"]["tag"] == "OK" for r in res))

    res = store.search_pairs(q, top_k=10, tag_filter=["NG"])
    check("tag_filter=NG で全件 tag==NG", all(r["doc"]["tag"] == "NG" for r in res))

    # KB layer_filter
    res = store.search_kb(q, top_k=3, layer_filter="L1_principles")
    check("layer_filter=L1_principles → 1件", len(res) == 1)
    check("layer match", res[0]["doc"]["layer"] == "L1_principles")

    res = store.search_kb(q, top_k=3, layer_filter="L4_implicit_knowledge")
    check("該当 layer 無し → 空", len(res) == 0)

    # ---------- 2. 実 Firestore 読み込み ----------
    print()
    print("[integration: 実 Firestore]")
    real = InMemoryVectorStore()
    stats = real.load()
    print(f"  stats: {real.info()}")
    check("pairs ≥ 2700 件ロード", stats.pair_count >= 2700, f"got {stats.pair_count}")
    check("kb 46 件ロード", stats.kb_count == 46, f"got {stats.kb_count}")
    check("pair dim 768", stats.pair_dim == 768)
    check("kb dim 768", stats.kb_dim == 768)
    check("approx mem < 30MB", real.info()["approx_mem_mb"] < 30, f"got {real.info()['approx_mem_mb']}MB")

    # 実 embedding (Firestore からの 1 件目) でクエリ → 自己が top1 のはず
    q_real = real.pair_embeddings[0]
    res = real.search_pairs(q_real, top_k=3)
    check("real top-1 = 自己 id", res[0]["id"] == real.pair_ids[0],
          f"got {res[0]['id']} vs {real.pair_ids[0]}")
    check("real top-1 dist ≈ 1.0", abs(res[0]["distance"] - 1.0) < 1e-3,
          f"got {res[0]['distance']:.4f}")
    check("real top-k メタデータあり", "summary" in res[0]["doc"] or "human_text" in res[0]["doc"])

    # tag_filter で OK / NG / 保留 が混ざらないか
    res_ok = real.search_pairs(q_real, top_k=20, tag_filter=["OK"])
    check(f"tag=OK のみ ({len(res_ok)}件)", all(r["doc"].get("tag") == "OK" for r in res_ok))

    res_okng = real.search_pairs(q_real, top_k=20, tag_filter=["OK", "NG"])
    check(f"tag=OK|NG のみ ({len(res_okng)}件)",
          all(r["doc"].get("tag") in ("OK", "NG") for r in res_okng))

    # KB layer filter
    for layer in ("L1_principles", "L2_regulations", "L3_strategy", "L4_implicit_knowledge"):
        kb_res = real.search_kb(q_real, top_k=3, layer_filter=layer)
        check(f"kb layer={layer} 1件以上",
              len(kb_res) >= 1 and all(r["doc"].get("layer") == layer for r in kb_res),
              f"got {len(kb_res)} hits")

    print()
    print(f"=== {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
