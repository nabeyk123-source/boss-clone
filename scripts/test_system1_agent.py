"""System1 Agent 本実装の単体テスト。

3サンプルクエリで:
- RetrievalService から similar_pairs を引いて
- Gemini Flash で直感応答
- 出力をパースして state に書く
ことを確認。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from boss_clone_lib.agents.system1_agent import System1Agent  # noqa: E402
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402


QUERIES = [
    "来月、kabe-anon の新機能をリリースしたいです",
    "セキュリティレビューが必要かどうか判断したい",
    "顧客の追加要望を一旦保留にしたいけど大丈夫？",
]


async def run_one(query: str, idx: int) -> dict:
    retrieval = RetrievalService()
    agent = System1Agent(
        name="system1",
        description="わたなべ部長の直感を担当",
    )
    agent.set_retrieval(retrieval)

    runner = InMemoryRunner(agent=agent, app_name="boss_clone")
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="watanabe",
        session_id=f"s1-test-{idx}",
        state={"user_query": query},
    )

    msg = types.Content(role="user", parts=[types.Part(text=query)])
    t0 = time.perf_counter()
    summaries: list[str] = []
    async for event in runner.run_async(user_id="watanabe", session_id=f"s1-test-{idx}", new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    summaries.append(part.text)
    total = time.perf_counter() - t0

    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="watanabe", session_id=f"s1-test-{idx}"
    )
    out = sess.state.get("system1_output", {})
    out["_total_s"] = total
    out["_event_text"] = " | ".join(summaries)
    return out


async def main() -> int:
    for i, q in enumerate(QUERIES, 1):
        print(f"=== Q{i}: {q} ===")
        out = await run_one(q, i)
        print(f"  total wall: {out.get('_total_s', 0)*1000:.0f}ms (LLM: {out.get('_latency_s', 0)*1000:.0f}ms)")
        print(f"  retrieved_pairs: {out.get('_retrieved_pairs_n', 0)}, llm_status={out.get('_llm_status')}")
        print(f"  intuitive_conclusion: {out.get('intuitive_conclusion')}")
        print(f"  match_confidence:     {out.get('match_confidence')}")
        print(f"  reference_cases:      {out.get('reference_cases')}")
        print(f"  concerns:             {out.get('concerns')}")
        raw = out.get('raw_response', '')
        print(f"  --- raw response ---")
        for line in raw.splitlines()[:12]:
            print(f"  | {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
