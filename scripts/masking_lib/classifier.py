"""Vertex AI Gemini 2.5 Flash で human 発言を OK/NG/保留 に分類。

仕様: masking_pipeline_spec.md §4.8
- Vertex AI 経由必須（Free Credit）
- 並列8並行、入力500トークン上限、出力10トークン上限、temperature 0.1
- レート制限 → exponential backoff 最大3回、それでも失敗なら「保留」フォールバック
"""
from __future__ import annotations

import asyncio
import os
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


async def _classify_one(client, sem: asyncio.Semaphore, cfg: ClassifyConfig, human_text: str) -> tuple[str, str]:
    """1ペアを分類。返り値 (tag, status) status: "ok"/"retry_exhausted"/"empty"。"""
    prompt = PROMPT.format(human_text=(human_text or "")[: cfg.input_char_cap])
    async with sem:
        backoff = cfg.initial_backoff_s
        for attempt in range(cfg.max_retries):
            try:
                resp = await client.aio.models.generate_content(
                    model=cfg.model,
                    contents=prompt,
                    config={
                        "max_output_tokens": 10,
                        "temperature": 0.1,
                    },
                )
                text = getattr(resp, "text", "") or ""
                if not text.strip():
                    return "保留", "empty"
                return _normalize_tag(text), "ok"
            except Exception as e:  # noqa: BLE001 — レート制限・一時障害含めて backoff
                if attempt == cfg.max_retries - 1:
                    return "保留", f"retry_exhausted:{type(e).__name__}"
                await asyncio.sleep(backoff)
                backoff *= 2
    return "保留", "loop_exit"


async def classify_many(
    human_texts: list[str],
    cfg: ClassifyConfig | None = None,
    progress_cb=None,
) -> list[tuple[str, str]]:
    cfg = cfg or ClassifyConfig()
    client = _build_client()
    sem = asyncio.Semaphore(cfg.max_concurrency)

    done = 0
    total = len(human_texts)

    async def wrapper(text: str) -> tuple[str, str]:
        nonlocal done
        r = await _classify_one(client, sem, cfg, text)
        done += 1
        if progress_cb and (done % 50 == 0 or done == total):
            progress_cb(done, total)
        return r

    return await asyncio.gather(*(wrapper(t) for t in human_texts))
