"""判断種別 5値分類。

仕様: schema_spec.md §3.1 / §5.1 Step 4
- decision_type: 採用 / 却下 / 保留 / 方向転換 / 情報要求
- masking pipeline の tag (OK/NG/保留) と部分的に重なるが、より粒度の細かい分類
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

VALID_TYPES = {"採用", "却下", "保留", "方向転換", "情報要求"}

PROMPT = """以下は対話ペアの要約です。人間（加藤部長）の判断種別を以下5つの **いずれか1つ** に分類してください。

- 採用: 提案・案を受け入れ、進める意思を示した
- 却下: 提案を明確に拒否、ストップをかけた
- 保留: 判断を後送り、追加情報待ち
- 方向転換: 別の案を提示、軌道修正を求めた
- 情報要求: 質問・確認・調査依頼が主目的

要約: {summary}

分類（上記5つから1つ、それ以外は出力しない）:"""


@dataclass
class DecisionConfig:
    model: str = "gemini-2.5-flash"
    max_concurrency: int = 2
    max_retries: int = 5
    initial_backoff_s: float = 1.0
    char_cap: int = 500
    thinking_budget: int = 0
    max_output_tokens: int = 20
    pre_call_sleep_s: float = 0.5


def _build_client():
    from google import genai
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT 未設定")
    return genai.Client(vertexai=True, project=project, location=location)


def _normalize(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return "保留"
    raw = stripped.splitlines()[0].strip().strip("。 .,")
    for t in VALID_TYPES:
        if t in raw:
            return t
    return "保留"


async def _classify_one(
    client,
    sem: asyncio.Semaphore,
    cfg: DecisionConfig,
    summary: str,
) -> tuple[str, str, float]:
    prompt = PROMPT.format(summary=(summary or "")[: cfg.char_cap])
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
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": cfg.thinking_budget},
                    },
                )
                latency = time.perf_counter() - t_start
                text = getattr(resp, "text", "") or ""
                if not text.strip():
                    return "保留", "empty", latency
                return _normalize(text), "ok", latency
            except Exception as e:  # noqa: BLE001
                if attempt == cfg.max_retries - 1:
                    return "保留", f"retry_exhausted:{type(e).__name__}", time.perf_counter() - t_start
                await asyncio.sleep(backoff)
                backoff *= 2
    return "保留", "loop_exit", time.perf_counter() - t_start


async def classify_decisions_many(
    summaries: list[str],
    cfg: DecisionConfig | None = None,
    progress_cb=None,
) -> list[tuple[str, str, float]]:
    cfg = cfg or DecisionConfig()
    client = _build_client()
    sem = asyncio.Semaphore(cfg.max_concurrency)
    done = 0
    total = len(summaries)

    async def wrapper(s: str) -> tuple[str, str, float]:
        nonlocal done
        r = await _classify_one(client, sem, cfg, s)
        done += 1
        if progress_cb and (done % 5 == 0 or done == total):
            progress_cb(done, total)
        return r

    return await asyncio.gather(*(wrapper(s) for s in summaries))
