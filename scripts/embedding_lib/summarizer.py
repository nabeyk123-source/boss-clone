"""ペアを 1〜2 文の要約に縮約する。

仕様: schema_spec.md §5.1 Step 2
- Vertex AI Gemini 2.5 Flash 経由
- thinking_budget=0、temperature=0.2（要約は決定論寄り）
- 並列度はユーザー指定（既定4、retry_exhausted リスクを抑える）
- 失敗時は元 human_text の先頭120字を fallback summary とする
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

PROMPT = """以下は人間（加藤部長）とAIアシスタント（クロ）の対話ペアです。
このペアで部長が何を判断・要求・確認したかを **1〜2文（合計100字以内）** の日本語で要約してください。
具体名詞は残し、メタな前置き（「以下は…」「対話の要約は…」）は禁止。

人間の発言:
{human_text}

AIの応答:
{assistant_text}

要約（100字以内、日本語）:"""


@dataclass
class SummarizeConfig:
    model: str = "gemini-2.5-flash"
    max_concurrency: int = 2
    max_retries: int = 5
    initial_backoff_s: float = 1.0
    human_char_cap: int = 600
    assistant_char_cap: int = 600
    thinking_budget: int = 0
    max_output_tokens: int = 200
    # レート保護: semaphore 取得後・API 呼び出し直前に sleep。
    # 並列2 × 1req/0.5s = 秒間最大 4 req にしてレート制限を回避（L-008 候補）
    pre_call_sleep_s: float = 0.5


def _build_client():
    from google import genai
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    if not use_vertex:
        raise RuntimeError("GOOGLE_GENAI_USE_VERTEXAI=TRUE を要求します")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT が未設定です")
    return genai.Client(vertexai=True, project=project, location=location)


def _fallback_summary(human_text: str) -> str:
    h = (human_text or "").strip().replace("\n", " ")
    return h[:120] if h else "(empty)"


async def _summarize_one(
    client,
    sem: asyncio.Semaphore,
    cfg: SummarizeConfig,
    human_text: str,
    assistant_text: str,
) -> tuple[str, str, float]:
    """1ペアを要約。返り値 (summary, status, latency_s)。"""
    prompt = PROMPT.format(
        human_text=(human_text or "")[: cfg.human_char_cap],
        assistant_text=(assistant_text or "")[: cfg.assistant_char_cap],
    )
    async with sem:
        if cfg.pre_call_sleep_s > 0:
            await asyncio.sleep(cfg.pre_call_sleep_s)
        backoff = cfg.initial_backoff_s
        t_start = time.perf_counter()
        for attempt in range(cfg.max_retries):
            try:
                resp = await client.aio.models.generate_content(
                    model=cfg.model,
                    contents=prompt,
                    config={
                        "max_output_tokens": cfg.max_output_tokens,
                        "temperature": 0.2,
                        "thinking_config": {"thinking_budget": cfg.thinking_budget},
                    },
                )
                latency = time.perf_counter() - t_start
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    return _fallback_summary(human_text), "empty", latency
                # 念のため改行を整え、長すぎる出力を切る
                return text.splitlines()[0][:200], "ok", latency
            except Exception as e:  # noqa: BLE001
                if attempt == cfg.max_retries - 1:
                    return (
                        _fallback_summary(human_text),
                        f"retry_exhausted:{type(e).__name__}",
                        time.perf_counter() - t_start,
                    )
                await asyncio.sleep(backoff)
                backoff *= 2
    return _fallback_summary(human_text), "loop_exit", time.perf_counter() - t_start


async def summarize_many(
    pairs: list[dict],
    cfg: SummarizeConfig | None = None,
    progress_cb=None,
) -> list[tuple[str, str, float]]:
    cfg = cfg or SummarizeConfig()
    client = _build_client()
    sem = asyncio.Semaphore(cfg.max_concurrency)

    done = 0
    total = len(pairs)

    async def wrapper(p: dict) -> tuple[str, str, float]:
        nonlocal done
        r = await _summarize_one(client, sem, cfg, p.get("human_text", ""), p.get("assistant_text", ""))
        done += 1
        if progress_cb and (done % 5 == 0 or done == total):
            progress_cb(done, total)
        return r

    return await asyncio.gather(*(wrapper(p) for p in pairs))
