"""ローカル jsonl の embedding を Firestore の pairs / acme_kb ドキュメントに backfill。

経緯:
- Day 2 の embedding_pipeline.py は embedding を Vector Search 用 jsonl（GCS）に書いただけで
  Firestore docs には embedding フィールドが入っていなかった。
- Day 4 で Vector Search を捨てて Firestore + アプリ内コサイン類似度に移行するため、
  Firestore docs に embedding フィールドを後追いで埋める。

入力:
  data/processed/embedding/pair_summaries.jsonl  ({id, embedding[768], restricts})
  data/processed/embedding/acme_kb.jsonl         ({id, embedding[768], restricts})

出力:
  pairs/{id}.embedding      : list[float] (768)
  acme_kb/{id}.embedding    : list[float] (768)

使い方:
  python scripts/backfill_embeddings_to_firestore.py --dry-run   # 確認のみ
  python scripts/backfill_embeddings_to_firestore.py             # 本実行
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for s in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

PAIR_JSONL = ROOT / "data" / "processed" / "embedding" / "pair_summaries.jsonl"
KB_JSONL = ROOT / "data" / "processed" / "embedding" / "acme_kb.jsonl"

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
BATCH_SIZE = 50  # 768 dim × 50 = 約 1.5MB / transaction（10MB制限内）


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--collection", choices=["pairs", "acme_kb", "both"], default="both")
    args = ap.parse_args()

    print(f"Project: {PROJECT}")

    targets: list[tuple[str, Path]] = []
    if args.collection in ("pairs", "both"):
        targets.append(("pairs", PAIR_JSONL))
    if args.collection in ("acme_kb", "both"):
        targets.append(("acme_kb", KB_JSONL))

    from google.cloud import firestore
    fs = firestore.Client(project=PROJECT, database="(default)")

    total_written = 0
    total_skipped = 0
    total_missing = 0

    for collection_name, jsonl_path in targets:
        if not jsonl_path.exists():
            print(f"[{collection_name}] SKIP: {jsonl_path} not found")
            continue
        rows = load_jsonl(jsonl_path)
        print(f"[{collection_name}] {len(rows)} embeddings in jsonl")

        # 既存ドキュメントの id 一覧（embedding 後追い対象を確認）
        existing_ids = {snap.id for snap in fs.collection(collection_name).stream()}
        print(f"[{collection_name}] {len(existing_ids)} docs in Firestore")

        in_both = [r for r in rows if r["id"] in existing_ids]
        in_jsonl_only = [r for r in rows if r["id"] not in existing_ids]
        print(f"[{collection_name}] jsonl ∩ Firestore = {len(in_both)} (これを backfill 対象とする)")
        print(f"[{collection_name}] jsonl にあって Firestore に無い = {len(in_jsonl_only)} (スキップ)")
        total_missing += len(in_jsonl_only)

        if args.dry_run:
            for r in in_both[:3]:
                print(f"  would write {collection_name}/{r['id']}: embedding[768]")
            print(f"[{collection_name}] dry-run: 書き込みスキップ")
            total_skipped += len(in_both)
            continue

        # batch commit
        n_written = 0
        batch = fs.batch()
        batch_count = 0
        for r in in_both:
            ref = fs.collection(collection_name).document(r["id"])
            batch.update(ref, {"embedding": r["embedding"]})
            batch_count += 1
            n_written += 1
            if batch_count >= BATCH_SIZE:
                batch.commit()
                print(f"[{collection_name}] committed {n_written}/{len(in_both)}")
                batch = fs.batch()
                batch_count = 0
        if batch_count > 0:
            batch.commit()
            print(f"[{collection_name}] committed {n_written}/{len(in_both)} (final)")
        total_written += n_written

    print()
    print(f"=== 完了: written={total_written}, dry-skipped={total_skipped}, jsonl-only(skipped)={total_missing} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
