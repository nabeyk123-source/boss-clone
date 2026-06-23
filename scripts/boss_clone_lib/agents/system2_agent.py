"""System2 Agent: Acme KB 4層を網羅検討（Gemini Pro thinking_budget=4000）。

仕様: docs/multi_agent_spec.md §3.2
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

from ..prompts.system2 import PROMPT, format_attached_document, format_kb
from ..retrieval.service import RetrievalService


MODEL = "gemini-2.5-pro"  # flash 試行→論点数1個に劣化のため pro 戻し
MAX_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.2
THINKING_BUDGET = 1000  # 2000→1000、速度優先（論点数2個維持が条件）
TOP_K_PER_LAYER = 3
MAX_RETRIES = 5
INITIAL_BACKOFF_S = 1.0
PRE_CALL_SLEEP_S = 0.5

LAYERS = [
    "L1_principles",
    "L2_regulations",
    "L3_strategy",
    "L4_implicit_knowledge",
]


def _build_client(project: str | None = None, location: str | None = None):
    from google import genai
    proj = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    loc = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return genai.Client(vertexai=True, project=proj, location=loc)


def _normalize_conclusion(s: str) -> str:
    for tag in ("採用", "却下", "条件付き保留"):
        if tag in s:
            return tag
    return "条件付き保留"


def _extract_first_line_after(text: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", text)
    if not m:
        return ""
    return m.group(1).split("\n")[0].strip()


def _strip_md(s: str) -> str:
    """**, ##, ###, ``, _ 等のマークダウン装飾を削る。"""
    s = re.sub(r"\*\*|__", "", s)
    s = re.sub(r"`+", "", s)
    s = s.lstrip(" 　#*-・")
    return s.strip()


# マークダウンを許容する「論点 N」ヘッダ
_ISSUE_HEADER = re.compile(r"(?m)^\s*(?:[#*\-・]\s*)*論点\s*(\d+)\s*[:：]\s*(.*)$")
# 「検討すべきポイント」「トレードオフ」「確認すべき項目」も先頭装飾を許容
_INNER_FIELD = re.compile(
    r"(?m)^\s*(?:[\-\*・#]\s*)*\**(検討すべきポイント|トレードオフ|確認すべき項目)\**\s*[:：]\s*(.*)$"
)


def parse_system2_response(text: str) -> dict:
    """raw 応答を State 構造体にパース。マークダウン装飾を考慮。"""
    raw = text or ""

    # 「論点 N: ...」の出現位置を集めて、各論点ブロックを抽出
    headers = list(_ISSUE_HEADER.finditer(raw))
    issues: list[dict] = []
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        block = raw[start:end]
        title = _strip_md(m.group(2))

        considerations: list[str] = []
        trade_offs: list[str] = []
        # 各インナーフィールドを拾う
        for inner in _INNER_FIELD.finditer(block):
            label = inner.group(1)
            # ラベル後のラベル行 + 続く箇条書きをまとめて取る
            inner_start = inner.end()
            # 次の見出し or 次のインナーフィールドまでが値
            next_inner = _INNER_FIELD.search(block, inner_start)
            value_end = next_inner.start() if next_inner else len(block)
            value = block[inner_start:value_end].strip()
            value_clean = _strip_md(value).strip()
            if label == "検討すべきポイント" and value_clean:
                considerations.append(value_clean[:300])
            elif label == "トレードオフ" and value_clean:
                trade_offs.append(value_clean[:200])

        if title or considerations or trade_offs:
            issues.append({
                "title": title or f"論点{m.group(1)}",
                "considerations": considerations,
                "trade_offs": trade_offs,
            })

    # 熟考的結論（マークダウン許容）
    conc_m = re.search(r"(?m)^\s*(?:[#*\-・]\s*)*\**熟考的結論\**\s*[:：]\s*(.+)$", raw)
    conclusion = _normalize_conclusion(conc_m.group(1) if conc_m else "")

    # 確認すべき項目（P4 強化版）
    # 「確認すべき項目」セクションの後の箇条書き or 改行リストを拾う。
    # ヘッダ行に値が無いケースもサポート（例: "確認すべき項目:\n- A\n- B"）
    verification_items: list[str] = []
    verify_header = re.search(
        r"(?m)^\s*(?:[\-\*・#]\s*)*\**確認すべき項目\**\s*[:：]?\s*$",
        raw,
    )
    if verify_header:
        # ヘッダ単独行のあとから次の空行 or 文書末まで
        after = raw[verify_header.end():]
        block_m = re.match(r"\s*\n(.+?)(?:\n\s*\n|\Z)", after, re.S)
        if block_m:
            for line in block_m.group(1).splitlines():
                stripped = _strip_md(line).strip()
                if stripped:
                    verification_items.append(stripped[:200])
    if not verification_items:
        # 旧パターン: 「確認すべき項目: 内容...」形式
        verify_m = re.search(
            r"(?m)^\s*(?:[\-\*・#]\s*)*\**確認すべき項目\**\s*[:：]\s*(.+?)(?:\n\n|\Z)",
            raw,
            re.S,
        )
        if verify_m:
            for line in verify_m.group(1).splitlines():
                stripped = _strip_md(line).strip()
                if stripped:
                    verification_items.append(stripped[:200])

    # 各論点の trade_offs を全体集計
    all_trade_offs: list[str] = []
    for issue in issues:
        all_trade_offs.extend(issue.get("trade_offs", []))

    return {
        "thoughtful_conclusion": conclusion,
        "issues": issues[:4],
        "trade_offs": all_trade_offs[:5],
        "verification_items": verification_items[:5],
        "raw_response": raw,
    }


class System2Agent(BaseAgent):
    """RetrievalService + Vertex AI Gemini Pro thinking を使った本実装。"""

    def model_post_init(self, _ctx) -> None:
        object.__setattr__(self, "_retrieval", None)
        object.__setattr__(self, "_genai_client", None)

    def set_retrieval(self, retrieval: RetrievalService) -> None:
        object.__setattr__(self, "_retrieval", retrieval)

    def _ensure_client(self):
        if getattr(self, "_genai_client", None) is None:
            object.__setattr__(self, "_genai_client", _build_client())
        return self._genai_client

    async def _retrieve_all_layers(self, retrieval: RetrievalService, user_query: str) -> dict[str, list]:
        """4 layer の KB を順次取得（embed cache 効くので 4 回呼んでも embed は1回）。"""
        out: dict[str, list] = {}
        for layer in LAYERS:
            try:
                items = await retrieval.get_relevant_kb(user_query, layer_filter=layer, top_k=TOP_K_PER_LAYER)
            except Exception:  # noqa: BLE001
                items = []
            out[layer] = items
        return out

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

        # 1) 4 layer の KB を取得
        if retrieval is not None and user_query:
            kb = await self._retrieve_all_layers(retrieval, user_query)
            retrieval_error = None
        else:
            kb = {layer: [] for layer in LAYERS}
            retrieval_error = "RetrievalService 未設定 または user_query 空"

        # 2) Build prompt
        prompt = PROMPT.format(
            user_query=user_query or "(empty)",
            attached_document=format_attached_document(attached_document),
            retrieved_l1=format_kb(kb.get("L1_principles", [])),
            retrieved_l2=format_kb(kb.get("L2_regulations", [])),
            retrieved_l3=format_kb(kb.get("L3_strategy", [])),
            retrieved_l4=format_kb(kb.get("L4_implicit_knowledge", [])),
        )

        # 3) LLM call
        raw, status, latency = await self._call_llm(prompt)

        # 4) Parse + state_delta
        parsed = parse_system2_response(raw)
        parsed["_llm_status"] = status
        parsed["_latency_s"] = latency
        parsed["_kb_counts"] = {layer: len(items) for layer, items in kb.items()}
        if retrieval_error:
            parsed["_retrieval_error"] = retrieval_error

        actions = EventActions(state_delta={"system2_output": parsed})
        summary_text = (
            f"[System2] {parsed['thoughtful_conclusion']}"
            f" / 論点 {len(parsed['issues'])}件"
            f" / kb={parsed['_kb_counts']}"
            f" / latency={latency*1000:.0f}ms / status={status}"
        )
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=summary_text)]),
            actions=actions,
        )
