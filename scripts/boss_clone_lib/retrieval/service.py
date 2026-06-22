"""RetrievalService: Vector Search + Firestore + embedding キャッシュ。

仕様: docs/multi_agent_spec.md §4.1

- get_similar_pairs(query, tag_filter, top_k): pair_summaries_v1 で類似ペア取得 + Firestore enrich
- get_relevant_kb(query, layer_filter, top_k): acme_kb_v1 で関連 KB 取得 + Firestore enrich
- session 内 embedding キャッシュ（同じ query で何回呼ばれても embed 1回）
- Vertex AI client は long-lived 化（コールドスタートの 1回だけ吸収）

state.json から index/endpoint resource を読む（test_vs_setup.py の出力と整合）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[3]
VS_STATE_FILE = ROOT / "scripts" / "test_vs_setup.state.json"


@dataclass
class RetrievedItem:
    """Vector Search + Firestore 結合後の 1 件分の検索結果。"""
    id: str
    distance: float
    doc: dict = field(default_factory=dict)


def _load_state() -> dict:
    if not VS_STATE_FILE.exists():
        raise FileNotFoundError(
            f"{VS_STATE_FILE} が空 / 不在です。先に test_vs_setup.py --setup を実行してください"
        )
    state = json.loads(VS_STATE_FILE.read_text(encoding="utf-8"))
    if not state:
        raise RuntimeError("VS state ファイルが空です。setup されていないか、teardown 済みです")
    return state


class RetrievalService:
    """マルチエージェントから共有される検索ファサード。"""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        pair_source: str = "pair_summaries",
        kb_source: str = "acme_kb",
    ) -> None:
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.pair_source = pair_source
        self.kb_source = kb_source

        self._embed_cache: dict[str, list[float]] = {}
        self._embed_client = None  # lazy init
        self._aiplatform_inited = False
        self._pair_endpoint = None
        self._pair_deployed_id: str | None = None
        self._kb_endpoint = None
        self._kb_deployed_id: str | None = None
        self._firestore_client = None  # lazy

    # ---------- lazy init ----------

    def _init_aiplatform(self) -> None:
        if self._aiplatform_inited:
            return
        from google.cloud import aiplatform
        aiplatform.init(project=self.project, location=self.location)
        self._aiplatform_inited = True

    def _ensure_pair_endpoint(self):
        if self._pair_endpoint is not None:
            return
        state = _load_state()
        info = state.get(self.pair_source)
        if not info:
            raise RuntimeError(f"VS state に {self.pair_source} が無い")
        self._init_aiplatform()
        from google.cloud import aiplatform
        self._pair_endpoint = aiplatform.MatchingEngineIndexEndpoint(info["endpoint_resource"])
        self._pair_deployed_id = info["deployed_index_id"]

    def _ensure_kb_endpoint(self):
        """acme_kb 用 endpoint は Day 3 時点で未デプロイの可能性があるので optional。"""
        if self._kb_endpoint is not None:
            return True
        try:
            state = _load_state()
        except Exception:
            return False
        info = state.get(self.kb_source)
        if not info:
            return False
        self._init_aiplatform()
        from google.cloud import aiplatform
        self._kb_endpoint = aiplatform.MatchingEngineIndexEndpoint(info["endpoint_resource"])
        self._kb_deployed_id = info["deployed_index_id"]
        return True

    def _ensure_firestore(self):
        if self._firestore_client is not None:
            return
        from google.cloud import firestore
        self._firestore_client = firestore.Client(project=self.project, database="(default)")

    def _ensure_embed_client(self):
        if self._embed_client is not None:
            return
        from google import genai
        self._embed_client = genai.Client(vertexai=True, project=self.project, location=self.location)

    # ---------- embedding ----------

    async def embed_query(self, query: str) -> list[float]:
        """単発の query を埋め込みベクトルへ。同じ query は cache から返す。"""
        if query in self._embed_cache:
            return self._embed_cache[query]
        self._ensure_embed_client()
        from embedding_lib.embedder import EMBEDDING_MODEL
        resp = await self._embed_client.aio.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[query[:2000]],
            config={"task_type": "SEMANTIC_SIMILARITY"},
        )
        embeddings = getattr(resp, "embeddings", None) or []
        if not embeddings:
            raise RuntimeError("query embedding failed (empty response)")
        vec = list(getattr(embeddings[0], "values", []) or [])
        if not vec:
            raise RuntimeError("query embedding failed (no values)")
        self._embed_cache[query] = vec
        return vec

    def warmup(self) -> None:
        """起動時に embed 1回叩いてコールドスタートを潰す（同期版）。"""
        asyncio.run(self.embed_query("warmup"))

    # ---------- search ----------

    async def _find_neighbors(
        self,
        endpoint,
        deployed_index_id: str,
        emb: list[float],
        top_k: int,
        restricts: list[dict] | None,
    ) -> list[tuple[str, float]]:
        # find_neighbors は同期 API なので asyncio.to_thread でラップ
        def _call():
            kwargs = {
                "deployed_index_id": deployed_index_id,
                "queries": [emb],
                "num_neighbors": top_k,
            }
            if restricts:
                # filter 形式: list[Namespace]
                from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import Namespace
                kwargs["filter"] = [Namespace(name=r["namespace"], allow_tokens=r["allow"]) for r in restricts]
            return endpoint.find_neighbors(**kwargs)
        results = await asyncio.to_thread(_call)
        if not results or not results[0]:
            return []
        return [(n.id, float(n.distance)) for n in results[0]]

    def _fetch_docs(self, collection: str, ids: list[str]) -> dict[str, dict]:
        self._ensure_firestore()
        coll = self._firestore_client.collection(collection)
        out: dict[str, dict] = {}
        # Firestore SDK の get_all で batch 取得
        refs = [coll.document(i) for i in ids]
        for snap in self._firestore_client.get_all(refs):
            if snap.exists:
                out[snap.id] = snap.to_dict()
        return out

    async def get_similar_pairs(
        self,
        query: str,
        *,
        tag_filter: list[str] | None = None,
        top_k: int = 5,
    ) -> list[RetrievedItem]:
        self._ensure_pair_endpoint()
        emb = await self.embed_query(query)
        restricts = [{"namespace": "tag", "allow": tag_filter}] if tag_filter else None
        pairs = await self._find_neighbors(self._pair_endpoint, self._pair_deployed_id, emb, top_k, restricts)
        if not pairs:
            return []
        ids = [p[0] for p in pairs]
        docs = self._fetch_docs("pairs", ids)
        return [RetrievedItem(id=i, distance=d, doc=docs.get(i, {})) for i, d in pairs]

    async def get_relevant_kb(
        self,
        query: str,
        *,
        layer_filter: str | None = None,
        top_k: int = 3,
    ) -> list[RetrievedItem]:
        """acme_kb_v1 endpoint が未デプロイなら、Firestore の acme_kb から layer フィルタで取る fallback。"""
        if self._ensure_kb_endpoint():
            emb = await self.embed_query(query)
            restricts = [{"namespace": "layer", "allow": [layer_filter]}] if layer_filter else None
            kbs = await self._find_neighbors(self._kb_endpoint, self._kb_deployed_id, emb, top_k, restricts)
            if kbs:
                ids = [k[0] for k in kbs]
                docs = self._fetch_docs("acme_kb", ids)
                return [RetrievedItem(id=i, distance=d, doc=docs.get(i, {})) for i, d in kbs]

        # Fallback: Firestore の acme_kb を layer_filter で全取得し、先頭 top_k を返す
        # （Vector Search が無くても layer ベースで一定の網羅性は確保できる）
        self._ensure_firestore()
        coll = self._firestore_client.collection("acme_kb")
        query_ref = coll
        if layer_filter:
            query_ref = coll.where("layer", "==", layer_filter)
        out: list[RetrievedItem] = []
        for snap in query_ref.limit(top_k).stream():
            out.append(RetrievedItem(id=snap.id, distance=0.0, doc=snap.to_dict()))
        return out
