"""Synthesizer Agent: System1/System2 の結論をわたなべ部長スタイルで統合。

仕様: docs/multi_agent_spec.md §3.3
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from ..prompts.synthesizer import format_prompt


MODEL = "gemini-2.5-pro"
MAX_OUTPUT_TOKENS = 1500
TEMPERATURE = 0.4
THINKING_BUDGET = 1000  # 2000→1000（レイテンシ改善、わたなべ部長スタイル維持を確認）
MAX_RETRIES = 5
INITIAL_BACKOFF_S = 1.0
PRE_CALL_SLEEP_S = 0.5


def _build_client(project: str | None = None, location: str | None = None):
    from google import genai
    proj = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    loc = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return genai.Client(vertexai=True, project=proj, location=loc)


def _strip_md(s: str) -> str:
    s = re.sub(r"\*\*|__|`+", "", s)
    return s.lstrip(" 　#*-・").strip()


_ISSUE_HEADER = re.compile(r"(?m)^\s*(?:[#*\-・]\s*)*論点\s*(\d+)\s*[:：]\s*(.*)$")
_INNER_FIELD = re.compile(
    r"(?m)^\s*(?:[\-\*・#]\s*)*\**(状況|確認すべき質問)\**\s*[:：]\s*(.*)$"
)


def parse_synthesizer_response(text: str, s1_conc: str, s2_conc: str) -> dict:
    """raw 応答を State 構造体にパース（P6 強化版）。

    P6 改善点:
    - 論点ヘッダのマッチを `^論点 N` だけでなく `**論点 N**` `### 論点 N` も含めて頑健に
    - alignment_comment は「論点ブロック後の本文」の **最初の段落** から、内部用語を含まない自然文を拾う
    - closing_remark は文書の最後の非空行（alignment_comment と重複しない）
    - issues_summary は title が取れなくても `論点N` でフォールバック
    """
    raw = text or ""

    # 論点抽出（既存と同じ正規表現で頑健、ただし出力結果の整理を強化）
    headers = list(_ISSUE_HEADER.finditer(raw))
    issues: list[dict] = []
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        block = raw[start:end]
        # title をマークダウンクリーニング + 引用符除去
        title = _strip_md(m.group(2)).strip("「」『』\"'")
        situation = ""
        question = ""
        for inner in _INNER_FIELD.finditer(block):
            label = inner.group(1)
            value_start = inner.end()
            next_inner = _INNER_FIELD.search(block, value_start)
            value_end = next_inner.start() if next_inner else len(block)
            value = _strip_md(block[value_start:value_end])
            if label == "状況":
                situation = value[:300]
            elif label == "確認すべき質問":
                question = value[:300]
        # title が取れなくても論点番号でフォールバック
        issues.append({
            "title": title or f"論点{m.group(1)}",
            "situation": situation,
            "question": question,
        })

    # alignment_comment（論点後の本文先頭から内部用語含まない自然文を抽出）
    after = raw[headers[-1].end():] if headers else ""
    lines_after = [l.strip() for l in after.splitlines() if l.strip()]
    alignment_comment = ""
    closing_remark = ""
    if lines_after:
        # 「角度違い」「ズレ」「揃って」など整合性を語る語を含む行を優先で拾う
        priority_keywords = ("角度違い", "ズレ", "揃って", "一致してる", "見えてる", "方向は", "立ち止ま")
        for line in lines_after[:6]:
            if any(k in line for k in priority_keywords):
                alignment_comment = _strip_md(line)[:240]
                break

        # 締めは最後の非空行（行頭装飾削除）
        closing_remark = _strip_md(lines_after[-1])[:200]
        if alignment_comment and alignment_comment == closing_remark:
            # 同じ行が両方に入るなら alignment_comment 側を空に
            alignment_comment = ""

    aligned = (s1_conc == s2_conc) if (s1_conc and s2_conc) else None

    return {
        # issues_summary を 5 件まで（仕様は 3 件想定だが、LLM が 4-5 件出すケースも許容）
        "issues_summary": [issue["title"] for issue in issues][:5],
        "questions": [issue["question"] for issue in issues if issue.get("question")][:5],
        "alignment": "aligned" if aligned is True else ("misaligned" if aligned is False else "unknown"),
        "alignment_comment": alignment_comment,
        "closing_remark": closing_remark,
        "raw_response": raw,
        "issues_detail": issues,
    }


class SynthesizerAgent(BaseAgent):
    def model_post_init(self, _ctx) -> None:
        object.__setattr__(self, "_genai_client", None)

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
        s1 = state.get("system1_output") or {}
        s2 = state.get("system2_output") or {}
        user_answers = state.get("user_answers") or []

        prompt = format_prompt(
            user_query=user_query,
            system1_output=s1,
            system2_output=s2,
            user_answers=user_answers,
        )
        raw, status, latency = await self._call_llm(prompt)

        s1_conc = s1.get("intuitive_conclusion", "")
        s2_conc = s2.get("thoughtful_conclusion", "")
        parsed = parse_synthesizer_response(raw, s1_conc, s2_conc)
        parsed["_llm_status"] = status
        parsed["_latency_s"] = latency
        parsed["_s1_conclusion"] = s1_conc
        parsed["_s2_conclusion"] = s2_conc

        actions = EventActions(state_delta={"final_response": parsed})
        summary_text = (
            f"[Synthesizer] s1={s1_conc} / s2={s2_conc} / alignment={parsed['alignment']}"
            f" / 論点 {len(parsed['issues_summary'])}件"
            f" / latency={latency*1000:.0f}ms / status={status}"
        )
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=summary_text)]),
            actions=actions,
        )
