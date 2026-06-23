"""System1 Agent: 過去判断パターンから直感応答（Gemini Flash, thinking_budget=0）。

仕様: docs/multi_agent_spec.md §3.1
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from ..prompts.system1 import PROMPT, format_attached_document, format_pairs
from ..retrieval.service import RetrievalService


MODEL = "gemini-2.5-flash"
MAX_OUTPUT_TOKENS = 768  # 質問生成を加えたので余裕を取る
TEMPERATURE = 0.3
THINKING_BUDGET = 0
TOP_K = 5
MAX_RETRIES = 5
INITIAL_BACKOFF_S = 1.0
PRE_CALL_SLEEP_S = 0.5


def _build_client(project: str | None = None, location: str | None = None):
    from google import genai
    proj = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    loc = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return genai.Client(vertexai=True, project=proj, location=loc)


def _extract_field(text: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", text)
    if not m:
        return ""
    return m.group(1).split("\n")[0].strip()


def _normalize_conclusion(s: str) -> str:
    for tag in ("採用", "却下", "条件付き保留"):
        if tag in s:
            return tag
    return "条件付き保留"


def _normalize_confidence(s: str) -> str:
    for c in ("高", "中", "低"):
        if c in s:
            return c
    return "中"


def _extract_questions(raw: str) -> list[dict]:
    """確認質問セクションから JSON 配列を抽出し、{question, options} のリストを返す。

    パース失敗時は legacy（番号付き行）の fallback でテキスト質問だけ拾い、
    options=[] を埋める（CLI 側で自由入力として扱える）。
    """
    # JSON ブロックを優先抽出（```json ... ``` または素の [...] 配列）
    json_block: str | None = None
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.S)
    if fenced:
        json_block = fenced.group(1)
    else:
        # 「確認質問」セクション以降の最初の JSON 配列
        section = re.search(r"確認質問[^\n]*\n+(.*?)$", raw, re.S)
        target = section.group(1) if section else raw
        bare = re.search(r"(\[\s*\{.*?\}\s*\])", target, re.S)
        if bare:
            json_block = bare.group(1)

    if json_block:
        try:
            arr = json.loads(json_block)
            if isinstance(arr, list):
                out: list[dict] = []
                for item in arr[:3]:
                    if not isinstance(item, dict):
                        continue
                    q = (item.get("question") or "").strip()
                    opts = item.get("options") or []
                    if isinstance(opts, list):
                        opts = [str(o).strip() for o in opts if str(o).strip()][:4]
                    else:
                        opts = []
                    if q:
                        out.append({"question": q[:200], "options": opts})
                if out:
                    return out
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: 番号付き行から拾う（options 無し）
    m = re.search(r"確認質問\s*[:：]?\s*\n(.+?)(?:\n\n|\Z)", raw, re.S)
    if not m:
        return []
    block = m.group(1)
    fallback: list[dict] = []
    for line in block.splitlines():
        line = line.strip()
        m2 = re.match(r"^(?:\d+[\.\)）]\s*|[\-・*]\s*)(.+)$", line)
        if m2:
            q = m2.group(1).strip(" 　[]\"`").strip()
            if q and len(q) >= 3:
                fallback.append({"question": q[:200], "options": []})
        if len(fallback) >= 3:
            break
    return fallback


def parse_system1_response(text: str) -> dict:
    """raw LLM 応答を State 構造体にパース（仕様書 §3.4 + Day3 Step 7-1 + Day4 Phase1 P1/P2 拡張）。"""
    raw = text or ""
    # 内部分類を優先（P2: 自然な言い回し + 内部分類の分離）→ 既存「直感的結論」「直感的判断」もfallback
    conclusion_raw = (
        _extract_field(raw, "内部分類")
        or _extract_field(raw, "直感的結論")
        or _extract_field(raw, "直感的判断")
    )
    conclusion = _normalize_conclusion(conclusion_raw)
    # 文脈適合の言い回しは別途保持
    intuitive_phrase = _extract_field(raw, "直感的判断") or _extract_field(raw, "直感的結論")
    confidence = _normalize_confidence(_extract_field(raw, "過去パターンとの一致度"))
    ref_cases = _extract_field(raw, "根拠となる過去ケース")
    concerns = _extract_field(raw, "直感的に気になる点")
    questions = _extract_questions(raw)
    return {
        "intuitive_conclusion": conclusion,
        "intuitive_phrase": intuitive_phrase,  # P2: 文脈適合の自然な言い回し
        "match_confidence": confidence,
        "reference_cases": [ref_cases] if ref_cases else [],
        "concerns": [c.strip(" ・,") for c in re.split(r"[、,。]|・|;|；", concerns) if c.strip()][:3],
        "questions": questions,
        "raw_response": raw,
    }


class System1Agent(BaseAgent):
    """RetrievalService と Vertex AI Gemini Flash を使った本実装。

    pydantic-based BaseAgent なので、追加属性は model_post_init で _attr に格納する。
    """

    def model_post_init(self, _ctx) -> None:
        # super の init は親で実行済み。ここで追加リソースを束ねる
        object.__setattr__(self, "_retrieval", None)
        object.__setattr__(self, "_genai_client", None)

    def set_retrieval(self, retrieval: RetrievalService) -> None:
        object.__setattr__(self, "_retrieval", retrieval)

    def _ensure_client(self):
        if getattr(self, "_genai_client", None) is None:
            object.__setattr__(self, "_genai_client", _build_client())
        return self._genai_client

    async def _call_llm(self, prompt: str) -> tuple[str, str, float]:
        client = self._ensure_client()
        if PRE_CALL_SLEEP_S > 0:
            await asyncio.sleep(PRE_CALL_SLEEP_S)
        backoff = INITIAL_BACKOFF_S
        t0 = time.perf_counter()
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.aio.models.generate_content(
                    model=MODEL,
                    contents=prompt,
                    config={
                        "max_output_tokens": MAX_OUTPUT_TOKENS,
                        "temperature": TEMPERATURE,
                        "thinking_config": {"thinking_budget": THINKING_BUDGET},
                    },
                )
                latency = time.perf_counter() - t0
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    return "", "empty", latency
                return text, "ok", latency
            except Exception as e:  # noqa: BLE001
                if attempt == MAX_RETRIES - 1:
                    return "", f"retry_exhausted:{type(e).__name__}", time.perf_counter() - t0
                await asyncio.sleep(backoff)
                backoff *= 2
        return "", "loop_exit", time.perf_counter() - t0

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state or {}
        user_query = state.get("user_query", "")
        attached_document = state.get("attached_document")
        retrieval: RetrievalService | None = getattr(self, "_retrieval", None)

        # 1) Retrieve similar pairs
        if retrieval is not None and user_query:
            try:
                pairs = await retrieval.get_similar_pairs(
                    user_query, tag_filter=["OK", "NG"], top_k=TOP_K
                )
            except Exception as e:  # noqa: BLE001
                pairs = []
                retrieval_error = f"{type(e).__name__}: {e}"
            else:
                retrieval_error = None
        else:
            pairs = []
            retrieval_error = "RetrievalService 未設定 または user_query 空"

        # 2) Build prompt
        prompt = PROMPT.format(
            user_query=user_query or "(empty)",
            attached_document=format_attached_document(attached_document),
            retrieved_pairs=format_pairs(pairs),
        )

        # 3) Call LLM
        raw, status, latency = await self._call_llm(prompt)

        # 4) Parse + write state_delta
        parsed = parse_system1_response(raw)
        parsed["_llm_status"] = status
        parsed["_latency_s"] = latency
        parsed["_retrieved_pairs_n"] = len(pairs)
        if retrieval_error:
            parsed["_retrieval_error"] = retrieval_error

        actions = EventActions(state_delta={"system1_output": parsed})
        summary_text = (
            f"[System1] {parsed['intuitive_conclusion']}（一致度: {parsed['match_confidence']}）"
            f" / pairs={len(pairs)} / latency={latency*1000:.0f}ms / status={status}"
        )
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=summary_text)]),
            actions=actions,
        )
