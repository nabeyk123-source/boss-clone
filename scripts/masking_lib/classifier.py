"""Vertex AI Gemini 2.5 Flash で human 発言を OK/NG/保留 に分類。

仕様: masking_pipeline_spec.md §4.8
- Vertex AI 経由必須（Free Credit）
- 並列8並行、入力500トークン上限、出力10トークン上限、temperature 0.1
- レート制限 → exponential backoff 最大3回、それでも失敗なら「保留」フォールバック
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Iterable

PROMPT = """以下は人間（タナカ部長）とAIアシスタント（クロ）の対話における人間の発言です。
人間の反応を「OK」「NG」「保留」のいずれかに分類してください。

OK: 提案・案を採用、肯定的な判断
NG: 提案を却下、別案を提示、強い反対
保留: 判断保留、追加情報要求、議論継続

人間の発言:
{human_text}

分類（OK/NG/保留 の1単語のみ、それ以外は出力しない）:"""

VALID_TAGS = {"OK", "NG", "保留"}


@dataclass
class ClassifyConfig:
    model: str = "gemini-2.5-flash"
    max_concurrency: int = 8
    max_retries: int = 3
    initial_backoff_s: float = 1.0
    input_char_cap: int = 500
    # Gemini 2.5 系は thinking モデルで、出力トークン予算が reasoning に使われると本文が空になる。
    # thinking_budget=0 で「考えずに即答する」モードにしてコスト・遅延・空応答リスクを抑える。
    thinking_budget: int = 0
    max_output_tokens: int = 16


def _build_client():
    """google-genai SDK を Vertex AI モードで初期化。.env の GOOGLE_GENAI_USE_VERTEXAI=TRUE 前提。"""
    from google import genai
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    if not use_vertex:
        raise RuntimeError(
            "GOOGLE_GENAI_USE_VERTEXAI=TRUE を要求します（Free Credit 経路）。.env を確認してください。"
        )
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT が未設定です。")
    return genai.Client(vertexai=True, project=project, location=location)


def _normalize_tag(text: str) -> str:
    if not text:
        return "保留"
    cleaned = text.strip().splitlines()[0].strip().strip("。 .")
    for tag in VALID_TAGS:
        if tag in cleaned:
            return tag
    return "保留"


async def _classify_one(client, sem: asyncio.Semaphore, cfg: ClassifyConfig, human_text: str) -> tuple[str, str, float]:
    """1ペアを分類。返り値 (tag, status, latency_s)。

    status: "ok" / "empty" / "retry_exhausted:{ExcType}" / "loop_exit"
    latency_s: API 呼び出しに要した壁時計時間（リトライ込み、semaphore 待ち時間は含まない）
    """
    prompt = PROMPT.format(human_text=(human_text or "")[: cfg.input_char_cap])
    async with sem:
        backoff = cfg.initial_backoff_s
        t_start = time.perf_counter()
        for attempt in range(cfg.max_retries):
            try:
                resp = await client.aio.models.generate_content(
                    model=cfg.model,
                    contents=prompt,
                    config={
                        "max_output_tokens": cfg.max_output_tokens,
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": cfg.thinking_budget},
                    },
                )
                latency = time.perf_counter() - t_start
                text = getattr(resp, "text", "") or ""
                if not text.strip():
                    return "保留", "empty", latency
                return _normalize_tag(text), "ok", latency
            except Exception as e:  # noqa: BLE001 — レート制限・一時障害含めて backoff
                if attempt == cfg.max_retries - 1:
                    return "保留", f"retry_exhausted:{type(e).__name__}", time.perf_counter() - t_start
                await asyncio.sleep(backoff)
                backoff *= 2
    return "保留", "loop_exit", time.perf_counter() - t_start


async def classify_many(
    human_texts: list[str],
    cfg: ClassifyConfig | None = None,
    progress_cb=None,
) -> list[tuple[str, str, float]]:
    """並列で全テキストを分類。返り値の各要素は (tag, status, latency_s)。"""
    cfg = cfg or ClassifyConfig()
    client = _build_client()
    sem = asyncio.Semaphore(cfg.max_concurrency)

    done = 0
    total = len(human_texts)

    async def wrapper(text: str) -> tuple[str, str, float]:
        nonlocal done
        r = await _classify_one(client, sem, cfg, text)
        done += 1
        if progress_cb and (done % 50 == 0 or done == total):
            progress_cb(done, total)
        return r

    return await asyncio.gather(*(wrapper(t) for t in human_texts))
