"""RetrievalService: Firestore + アプリ内 cos 類似度（InMemoryVectorStore）+ embedding キャッシュ。

Day 4 改修（L-009）:
- 以前: Vertex AI Vector Search の Matching Engine endpoint を呼んでいた（24/7 で ¥80/h 課金）
- 以後: Firestore に backfill 済みの embedding を起動時に丸ごとメモリへ、numpy で類似度計算
- ハッカソン規模（2,800 件）には十分。Vector Search は数十万件超のときに復活させる

公開 API（既存と同じシグネチャを維持、agent コードは無改修で動く）:
- get_similar_pairs(query, tag_filter, top_k) -> list[RetrievedItem]
- get_relevant_kb(query, layer_filter, top_k)  -> list[RetrievedItem]
- embed_query(query) -> list[float]
- warmup() -> None
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .in_memory_store import InMemoryVectorStore


@dataclass
class RetrievedItem:
    """検索結果の 1 件。InMemoryVectorStore.search_* の dict と互換。"""
    id: str
    distance: float
    doc: dict = field(default_factory=dict)


class RetrievalService:
    """マルチエージェントから共有される検索ファサード。"""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self._embed_cache: dict[str, list[float]] = {}
        self._embed_client = None
        self._store: InMemoryVectorStore | None = None

    # ---------- store ----------

    def _ensure_store(self) -> InMemoryVectorStore:
        """InMemoryVectorStore の lazy init + 全件ロード（初回のみ）。"""
        if self._store is None:
            store = InMemoryVectorStore(project=self.project)
            store.load()  # 同期、~15s
            self._store = store
        return self._store

    def store_info(self) -> dict:
        return self._ensure_store().info()

    # ---------- embedding ----------

    def _ensure_embed_client(self):
        if self._embed_client is not None:
            return
        from google import genai
        self._embed_client = genai.Client(vertexai=True, project=self.project, location=self.location)

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
        """起動時に embed 1回 + ストアロードでコールドスタートを潰す（同期）。"""
        self._ensure_store()
        asyncio.run(self.embed_query("warmup"))

    # ---------- 検索 ----------

    async def get_similar_pairs(
        self,
        query: str,
        *,
        tag_filter: list[str] | None = None,
        top_k: int = 5,
    ) -> list[RetrievedItem]:
        emb = await self.embed_query(query)
        store = self._ensure_store()
        hits = store.search_pairs(emb, top_k=top_k, tag_filter=tag_filter)
        return [RetrievedItem(id=h["id"], distance=h["distance"], doc=h["doc"]) for h in hits]

    async def get_relevant_kb(
        self,
        query: str,
        *,
        layer_filter: str | None = None,
        top_k: int = 3,
    ) -> list[RetrievedItem]:
        emb = await self.embed_query(query)
        store = self._ensure_store()
        hits = store.search_kb(emb, top_k=top_k, layer_filter=layer_filter)
        return [RetrievedItem(id=h["id"], distance=h["distance"], doc=h["doc"]) for h in hits]
