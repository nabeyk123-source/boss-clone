"""Vector Search Phase 1: GCS バケット作成 → JSONL アップロード → Index 作成 → Endpoint デプロイ。

Phase 運用（lessons.md 反映予定）:
  Phase 1（smoke 10件） → Phase 2（本処理 2,848件） → Phase 3（提出直前）
各Phase終わりに endpoint を削除して維持コスト $0 にする。

使い方:
    python scripts/test_vs_setup.py --setup              # 構築・デプロイ
    python scripts/test_vs_setup.py --teardown           # 削除（コスト止め）
    python scripts/test_vs_setup.py --query "TEXT"       # 検索
    python scripts/test_vs_setup.py --status             # 構築済みリソースの確認
    python scripts/test_vs_setup.py --source pair_summaries  # 既定。acme_kb 用なら --source acme_kb

GCS バケット / Index ID / Endpoint ID は scripts/test_vs_setup.state.json に保存される。
"""
from __future__ import annotations

import argparse
import asyncio
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

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
GCS_BUCKET = f"{PROJECT}-vector-search"

# 入力ファイル（embedding_pipeline.py / acme_kb_loader.py の出力）
JSONL_PAIR = ROOT / "data" / "processed" / "embedding" / "pair_summaries.jsonl"
JSONL_ACME = ROOT / "data" / "processed" / "embedding" / "acme_kb.jsonl"

STATE_FILE = ROOT / "scripts" / "test_vs_setup.state.json"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vs_setup")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def ensure_bucket(bucket_name: str) -> None:
    """GCS バケットが無ければ作る。"""
    from google.cloud import storage
    client = storage.Client(project=PROJECT)
    bucket = client.bucket(bucket_name)
    if bucket.exists():
        log.info("[gcs] bucket exists: %s", bucket_name)
        return
    log.info("[gcs] creating bucket: %s (%s)", bucket_name, LOCATION)
    client.create_bucket(bucket, location=LOCATION)
    log.info("[gcs] bucket created")


def upload_jsonl(bucket_name: str, local_path: Path, blob_subdir: str) -> str:
    """JSONL を GCS にアップロードして gs:// URI（フォルダ）を返す。

    Vertex AI Vector Search は .json / .csv / .avro のみ受け付けるので、
    ローカルが .jsonl でも GCS 側では .json で保存する（中身は JSONL のまま）。
    """
    from google.cloud import storage
    client = storage.Client(project=PROJECT)
    bucket = client.bucket(bucket_name)
    remote_name = local_path.name
    if remote_name.endswith(".jsonl"):
        remote_name = remote_name[:-1]  # .jsonl → .json
    blob_name = f"{blob_subdir}/{remote_name}"

    # 既存ファイル（.jsonl など Vertex AI が読めない拡張子）を掃除してから再アップロード
    for old in client.list_blobs(bucket_name, prefix=f"{blob_subdir}/"):
        if old.name != blob_name:
            log.info("[gcs] cleanup stale blob: %s", old.name)
            old.delete()

    blob = bucket.blob(blob_name)
    log.info("[gcs] upload %s -> gs://%s/%s", local_path.name, bucket_name, blob_name)
    blob.upload_from_filename(str(local_path))
    return f"gs://{bucket_name}/{blob_subdir}"


def create_index(display_name: str, gcs_uri_folder: str, dim: int = 768):
    """MatchingEngineIndex を BATCH モードで作成。完了まで同期待機。"""
    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT, location=LOCATION)
    log.info("[vs] creating index '%s' from %s ...（10〜20分かかる）", display_name, gcs_uri_folder)
    t0 = time.perf_counter()
    index = aiplatform.MatchingEngineIndex.create_tree_ah_index(
        display_name=display_name,
        contents_delta_uri=gcs_uri_folder,
        dimensions=dim,
        approximate_neighbors_count=10,
        distance_measure_type="DOT_PRODUCT_DISTANCE",
        leaf_node_embedding_count=100,
        leaf_nodes_to_search_percent=50,
        index_update_method="BATCH_UPDATE",
        sync=True,
    )
    log.info("[vs] index created in %.1fs: %s", time.perf_counter() - t0, index.resource_name)
    return index


def create_endpoint(display_name: str):
    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT, location=LOCATION)
    log.info("[vs] creating endpoint '%s' ...", display_name)
    t0 = time.perf_counter()
    endpoint = aiplatform.MatchingEngineIndexEndpoint.create(
        display_name=display_name,
        public_endpoint_enabled=True,
        sync=True,
    )
    log.info("[vs] endpoint created in %.1fs: %s", time.perf_counter() - t0, endpoint.resource_name)
    return endpoint


def deploy_index(endpoint, index, deployed_id: str):
    log.info("[vs] deploying index → endpoint as '%s' ...（10〜20分）", deployed_id)
    t0 = time.perf_counter()
    endpoint.deploy_index(
        index=index,
        deployed_index_id=deployed_id,
        display_name=deployed_id,
        min_replica_count=1,
        max_replica_count=1,
    )
    log.info("[vs] deployed in %.1fs", time.perf_counter() - t0)


def setup(source: str) -> None:
    state = load_state()

    if source == "pair_summaries":
        local_jsonl = JSONL_PAIR
        index_name = "boss-clone-pair-summaries-smoke"
        endpoint_name = "boss-clone-pair-summaries-endpoint"
        deployed_id = "pair_summaries_v1"
        gcs_subdir = "pair_summaries"
    elif source == "acme_kb":
        local_jsonl = JSONL_ACME
        index_name = "boss-clone-acme-kb-smoke"
        endpoint_name = "boss-clone-acme-kb-endpoint"
        deployed_id = "acme_kb_v1"
        gcs_subdir = "acme_kb"
    else:
        raise ValueError(f"unknown source: {source}")

    if not local_jsonl.exists():
        raise FileNotFoundError(f"{local_jsonl} が見つかりません")

    ensure_bucket(GCS_BUCKET)
    gcs_uri = upload_jsonl(GCS_BUCKET, local_jsonl, gcs_subdir)

    index = create_index(index_name, gcs_uri)
    endpoint = create_endpoint(endpoint_name)
    deploy_index(endpoint, index, deployed_id)

    state[source] = {
        "index_resource": index.resource_name,
        "endpoint_resource": endpoint.resource_name,
        "deployed_index_id": deployed_id,
        "gcs_uri": gcs_uri,
        "bucket": GCS_BUCKET,
        "setup_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    log.info("[done] setup '%s' complete, state saved", source)


def teardown(source: str) -> None:
    """undeploy → endpoint delete → index delete を順に実行。

    どれかが失敗したら state は削除しない（リソースが残っているかも、再実行で復帰）。
    `--force-state-clear` で state だけクリアしたい場合は別実装。
    """
    state = load_state()
    info = state.get(source)
    if not info:
        log.warning("no state for source=%s, nothing to teardown", source)
        return

    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT, location=LOCATION)

    failures: list[str] = []

    if info.get("endpoint_resource"):
        try:
            endpoint = aiplatform.MatchingEngineIndexEndpoint(info["endpoint_resource"])
            deployed_list = list(endpoint.deployed_indexes or [])
            log.info("[vs] endpoint has %d deployed index(es)", len(deployed_list))
            for deployed in deployed_list:
                log.info("[vs] undeploy %s ...", deployed.id)
                t0 = time.perf_counter()
                # undeploy_index は同期 LRO（sync 引数なし）
                endpoint.undeploy_index(deployed_index_id=deployed.id)
                log.info("[vs] undeployed in %.1fs", time.perf_counter() - t0)
            log.info("[vs] delete endpoint %s", info["endpoint_resource"])
            endpoint.delete(sync=True)
            log.info("[vs] endpoint deleted")
        except Exception as e:  # noqa: BLE001
            failures.append(f"endpoint: {e}")
            log.error("endpoint teardown failed: %s", e)

    if info.get("index_resource") and not failures:
        try:
            index = aiplatform.MatchingEngineIndex(info["index_resource"])
            log.info("[vs] delete index %s", info["index_resource"])
            index.delete(sync=True)
            log.info("[vs] index deleted")
        except Exception as e:  # noqa: BLE001
            failures.append(f"index: {e}")
            log.error("index teardown failed: %s", e)

    if failures:
        log.error("teardown FAILED, state is kept for retry: %s", failures)
        return

    del state[source]
    save_state(state)
    log.info("[done] teardown '%s' complete, state cleared", source)


async def _embed_query(text: str) -> list[float]:
    from embedding_lib import embedder as emb_mod
    cfg = emb_mod.EmbedConfig(max_concurrency=1, batch_size=1)
    vectors, _ = await emb_mod.embed_many([text], cfg=cfg)
    if not vectors or vectors[0] is None:
        raise RuntimeError("query embedding failed")
    return vectors[0]


def query(source: str, query_text: str, top_k: int = 5) -> None:
    state = load_state()
    info = state.get(source)
    if not info:
        log.error("source=%s 未セットアップ", source)
        return

    log.info("[query] embedding: %s", query_text[:80])
    vec = asyncio.run(_embed_query(query_text))

    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT, location=LOCATION)
    endpoint = aiplatform.MatchingEngineIndexEndpoint(info["endpoint_resource"])

    t0 = time.perf_counter()
    results = endpoint.find_neighbors(
        deployed_index_id=info["deployed_index_id"],
        queries=[vec],
        num_neighbors=top_k,
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    log.info("[query] %d results in %.0f ms", len(results[0]) if results else 0, latency_ms)

    if not results or not results[0]:
        print("(no results)")
        return
    print(f"\n=== top {top_k} for: {query_text}  (latency {latency_ms:.0f}ms) ===")
    for i, n in enumerate(results[0], 1):
        print(f"  #{i}  distance={n.distance:.4f}  id={n.id}")


def status() -> None:
    state = load_state()
    if not state:
        print("(state empty, nothing set up)")
        return
    for source, info in state.items():
        print(f"--- {source} ---")
        for k, v in info.items():
            print(f"  {k}: {v}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="pair_summaries", choices=["pair_summaries", "acme_kb"])
    p.add_argument("--setup", action="store_true")
    p.add_argument("--teardown", action="store_true")
    p.add_argument("--query", type=str, default=None, help="検索クエリ文字列")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        status()
    elif args.setup:
        setup(args.source)
    elif args.teardown:
        teardown(args.source)
    elif args.query:
        query(args.source, args.query, args.top_k)
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
