"""text-multilingual-embedding-002 で 768 次元埋め込みを生成。

仕様: schema_spec.md §4.3
- Vertex AI ネイティブ
- バッチ呼び出しサポート（API 仕様で 1 回最大250 入力）
- 並列度は Gemini より緩く設定可（埋め込み API のレート制限は別枠）
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

EMBEDDING_MODEL = "text-multilingual-embedding-002"
EMBEDDING_DIM = 768


@dataclass
class EmbedConfig:
    model: str = EMBEDDING_MODEL
    batch_size: int = 50  # 1 リクエストあたり最大 250 だが、safe で 50
    max_concurrency: int = 4
    max_retries: int = 3
    initial_backoff_s: float = 1.0
    task_type: str = "SEMANTIC_SIMILARITY"
    char_cap: int = 2000


def _build_client():
    from google import genai
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT 未設定")
    return genai.Client(vertexai=True, project=project, location=location)


async def _embed_batch(
    client,
    sem: asyncio.Semaphore,
    cfg: EmbedConfig,
    texts: list[str],
) -> tuple[list[list[float] | None], str, float]:
    truncated = [(t or "")[: cfg.char_cap] for t in texts]
    async with sem:
        backoff = cfg.initial_backoff_s
        t_start = time.perf_counter()
        for attempt in range(cfg.max_retries):
            try:
                resp = await client.aio.models.embed_content(
                    model=cfg.model,
                    contents=truncated,
                    config={"task_type": cfg.task_type},
                )
                latency = time.perf_counter() - t_start
                vecs: list[list[float] | None] = []
                embeddings = getattr(resp, "embeddings", None) or []
                for e in embeddings:
                    vec = getattr(e, "values", None)
                    vecs.append(list(vec) if vec else None)
                # 件数が一致しない場合の安全装置
                while len(vecs) < len(texts):
                    vecs.append(None)
                return vecs[: len(texts)], "ok", latency
            except Exception as e:  # noqa: BLE001
                if attempt == cfg.max_retries - 1:
                    return [None] * len(texts), f"retry_exhausted:{type(e).__name__}", time.perf_counter() - t_start
                await asyncio.sleep(backoff)
                backoff *= 2
    return [None] * len(texts), "loop_exit", time.perf_counter() - t_start


async def embed_many(
    texts: list[str],
    cfg: EmbedConfig | None = None,
    progress_cb=None,
) -> tuple[list[list[float] | None], list[tuple[str, float]]]:
    """全テキストを埋め込み化。返り値: (vectors, [(batch_status, batch_latency)]).

    vectors の長さは入力テキストと一致。失敗箇所は None。
    """
    cfg = cfg or EmbedConfig()
    client = _build_client()
    sem = asyncio.Semaphore(cfg.max_concurrency)

    batches: list[list[int]] = []
    for i in range(0, len(texts), cfg.batch_size):
        batches.append(list(range(i, min(i + cfg.batch_size, len(texts)))))

    vectors: list[list[float] | None] = [None] * len(texts)
    batch_meta: list[tuple[str, float]] = []
    done = 0

    async def wrapper(indices: list[int]) -> tuple[list[int], list[list[float] | None], str, float]:
        nonlocal done
        batch_texts = [texts[i] for i in indices]
        vecs, status, latency = await _embed_batch(client, sem, cfg, batch_texts)
        done += len(indices)
        if progress_cb:
            progress_cb(done, len(texts))
        return indices, vecs, status, latency

    gathered = await asyncio.gather(*(wrapper(b) for b in batches))
    for indices, vecs, status, latency in gathered:
        batch_meta.append((status, latency))
        for idx, v in zip(indices, vecs):
            vectors[idx] = v

    return vectors, batch_meta
