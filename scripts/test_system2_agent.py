"""System2 Agent 本実装の単体テスト。3クエリで品質確認。"""
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

from boss_clone_lib.agents.system2_agent import System2Agent  # noqa: E402
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402


QUERIES = [
    "来月、kabe-anon の新機能をリリースしたいです",
    "セキュリティレビューが必要かどうか判断したい",
    "顧客の追加要望を一旦保留にしたいけど大丈夫？",
]


async def run_one(query: str, idx: int) -> dict:
    retrieval = RetrievalService()
    agent = System2Agent(name="system2", description="わたなべ部長の熟考を担当")
    agent.set_retrieval(retrieval)

    runner = InMemoryRunner(agent=agent)
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="watanabe",
        session_id=f"s2-test-{idx}",
        state={"user_query": query},
    )
    msg = types.Content(role="user", parts=[types.Part(text=query)])
    t0 = time.perf_counter()
    async for _event in runner.run_async(user_id="watanabe", session_id=f"s2-test-{idx}", new_message=msg):
        pass
    total = time.perf_counter() - t0
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="watanabe", session_id=f"s2-test-{idx}"
    )
    out = sess.state.get("system2_output", {})
    out["_total_s"] = total
    return out


async def main() -> int:
    for i, q in enumerate(QUERIES, 1):
        print(f"=== Q{i}: {q} ===")
        out = await run_one(q, i)
        print(f"  total wall: {out.get('_total_s', 0)*1000:.0f}ms (LLM: {out.get('_latency_s', 0)*1000:.0f}ms)")
        print(f"  kb_counts: {out.get('_kb_counts')}, llm_status={out.get('_llm_status')}")
        print(f"  thoughtful_conclusion: {out.get('thoughtful_conclusion')}")
        print(f"  issues (count={len(out.get('issues') or [])}):")
        for j, issue in enumerate(out.get("issues") or [], 1):
            print(f"    {j}. {issue.get('title')}")
        print(f"  verification_items ({len(out.get('verification_items') or [])}):")
        for v in (out.get("verification_items") or [])[:5]:
            print(f"    - {v}")
        raw = out.get('raw_response', '')
        print(f"  --- raw (head 18 lines) ---")
        for line in raw.splitlines()[:18]:
            print(f"  | {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
