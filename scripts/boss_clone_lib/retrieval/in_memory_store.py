"""Firestore からベクトル + メタデータを一括ロードし、numpy で類似度計算する。

経緯:
- Vector Search endpoint は ¥80/h 連続課金。ハッカソン規模（2,800 件程度）には過剰スペック。
- 768 dim × 2,833 件 = 約 17MB（float32）。メモリに乗る。
- text-multilingual-embedding-002 は L2 正規化済 → dot product = cos similarity

責務:
- Firestore の pairs / acme_kb から embedding + メタデータをロード
- top-k cos 類似度検索
- restricts（tag, topic_tags, layer）でのフィルタ

ライフサイクル:
- アプリ起動時に一度ロード（Streamlit @st.cache_resource で重複防止）
- 以降はメモリ内検索のみ（Firestore reads ゼロ）
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class StoreStats:
    pair_count: int = 0
    pair_dim: int = 0
    pair_load_s: float = 0.0
    kb_count: int = 0
    kb_dim: int = 0
    kb_load_s: float = 0.0


class InMemoryVectorStore:
    """Firestore + アプリ内 cos 類似度のストア。"""

    def __init__(self, *, project: str = "boss-clone-2026"):
        self.project = project
        # pairs
        self.pair_ids: list[str] = []
        self.pair_embeddings: np.ndarray | None = None  # shape (N, 768), float32
        self.pair_metadata: list[dict] = []
        # acme_kb
        self.kb_ids: list[str] = []
        self.kb_embeddings: np.ndarray | None = None
        self.kb_metadata: list[dict] = []
        # stats
        self.stats = StoreStats()
        self._loaded = False
        self._fs_client = None

    def _ensure_fs(self):
        if self._fs_client is None:
            from google.cloud import firestore
            self._fs_client = firestore.Client(project=self.project, database="(default)")
        return self._fs_client

    def load(self) -> StoreStats:
        """Firestore から全件ロード（同期）。複数回呼んでも初回だけ実行。"""
        if self._loaded:
            return self.stats
        fs = self._ensure_fs()

        # ----- pairs -----
        t0 = time.perf_counter()
        ids: list[str] = []
        vecs: list[list[float]] = []
        metas: list[dict] = []
        skipped_no_embed = 0
        for snap in fs.collection("pairs").stream():
            d = snap.to_dict() or {}
            emb = d.get("embedding")
            if not emb or not isinstance(emb, list):
                skipped_no_embed += 1
                continue
            ids.append(snap.id)
            vecs.append(emb)
            # メタデータは embedding 抜きで保持（メモリ節約）
            d_meta = {k: v for k, v in d.items() if k != "embedding"}
            metas.append(d_meta)
        self.pair_ids = ids
        self.pair_embeddings = np.asarray(vecs, dtype=np.float32) if vecs else None
        self.pair_metadata = metas
        self.stats.pair_count = len(ids)
        self.stats.pair_dim = self.pair_embeddings.shape[1] if self.pair_embeddings is not None else 0
        self.stats.pair_load_s = time.perf_counter() - t0

        # ----- acme_kb -----
        t0 = time.perf_counter()
        ids = []
        vecs = []
        metas = []
        for snap in fs.collection("acme_kb").stream():
            d = snap.to_dict() or {}
            emb = d.get("embedding")
            if not emb or not isinstance(emb, list):
                continue
            ids.append(snap.id)
            vecs.append(emb)
            d_meta = {k: v for k, v in d.items() if k != "embedding"}
            metas.append(d_meta)
        self.kb_ids = ids
        self.kb_embeddings = np.asarray(vecs, dtype=np.float32) if vecs else None
        self.kb_metadata = metas
        self.stats.kb_count = len(ids)
        self.stats.kb_dim = self.kb_embeddings.shape[1] if self.kb_embeddings is not None else 0
        self.stats.kb_load_s = time.perf_counter() - t0

        self._loaded = True
        return self.stats

    # ---------- 検索 ----------

    @staticmethod
    def _normalize(vec: list[float] | np.ndarray) -> np.ndarray:
        v = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
        return v

    def _topk(
        self,
        emb_matrix: np.ndarray,
        query: np.ndarray,
        mask: np.ndarray | None,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """index 列 + cos 類似度を top_k 個返す。"""
        sims = emb_matrix @ query  # shape (N,)
        if mask is not None:
            sims = np.where(mask, sims, -np.inf)
        # 上位 top_k のインデックス
        k = min(top_k, sims.shape[0])
        if k <= 0:
            return []
        # argpartition は O(N) で top-k を取る
        idx_unsorted = np.argpartition(-sims, kth=k - 1)[:k]
        # その中で sims が -inf のものは外す
        idx_sorted = idx_unsorted[np.argsort(-sims[idx_unsorted])]
        out = []
        for i in idx_sorted:
            s = float(sims[i])
            if s == -np.inf:
                continue
            out.append((int(i), s))
        return out

    def search_pairs(
        self,
        query_embedding: list[float] | np.ndarray,
        *,
        top_k: int = 5,
        tag_filter: list[str] | None = None,
    ) -> list[dict]:
        """類似ペアを top_k で返す（メタデータ + distance 付き）。"""
        if not self._loaded:
            self.load()
        if self.pair_embeddings is None or len(self.pair_ids) == 0:
            return []

        q = self._normalize(query_embedding)
        mask: np.ndarray | None = None
        if tag_filter:
            tag_set = set(tag_filter)
            mask = np.array(
                [(m.get("tag") in tag_set) for m in self.pair_metadata],
                dtype=bool,
            )
        results = self._topk(self.pair_embeddings, q, mask, top_k)
        out = []
        for i, sim in results:
            meta = dict(self.pair_metadata[i])
            out.append({
                "id": self.pair_ids[i],
                "distance": sim,  # cos sim、大きいほど近い（VS の dot-product distance と同じ規約）
                "doc": meta,
            })
        return out

    def search_kb(
        self,
        query_embedding: list[float] | np.ndarray,
        *,
        top_k: int = 3,
        layer_filter: str | None = None,
    ) -> list[dict]:
        """類似 KB を top_k で返す（layer フィルタ付き）。"""
        if not self._loaded:
            self.load()
        if self.kb_embeddings is None or len(self.kb_ids) == 0:
            return []

        q = self._normalize(query_embedding)
        mask: np.ndarray | None = None
        if layer_filter:
            mask = np.array(
                [(m.get("layer") == layer_filter) for m in self.kb_metadata],
                dtype=bool,
            )
        results = self._topk(self.kb_embeddings, q, mask, top_k)
        out = []
        for i, sim in results:
            meta = dict(self.kb_metadata[i])
            out.append({
                "id": self.kb_ids[i],
                "distance": sim,
                "doc": meta,
            })
        return out

    # ---------- 互換 ----------

    def info(self) -> dict:
        return {
            "pair_count": self.stats.pair_count,
            "pair_dim": self.stats.pair_dim,
            "pair_load_s": round(self.stats.pair_load_s, 2),
            "kb_count": self.stats.kb_count,
            "kb_dim": self.stats.kb_dim,
            "kb_load_s": round(self.stats.kb_load_s, 2),
            "approx_mem_mb": round(
                ((self.pair_embeddings.nbytes if self.pair_embeddings is not None else 0)
                 + (self.kb_embeddings.nbytes if self.kb_embeddings is not None else 0))
                / (1024 * 1024), 2
            ),
        }
