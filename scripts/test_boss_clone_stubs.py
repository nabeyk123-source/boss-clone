"""Step 1 スタブの統合動作確認。

ADK Runner で `build_boss_clone()` を実行し、ParallelAgent と SequentialAgent が
正しく配線されて state_delta が伝播することを確認する（LLM 呼び出しなし、コストゼロ）。
"""
from __future__ import annotations

import asyncio
import sys
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

from boss_clone_lib.coordinator import build_boss_clone  # noqa: E402


USER_QUERY = "来月、新機能リリースしたいです"
USER_ID = "watanabe"
SESSION_ID = "stub-test-session"


async def run() -> int:
    agent = build_boss_clone()
    runner = InMemoryRunner(agent=agent)
    session = await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id=USER_ID,
        session_id=SESSION_ID,
        state={"user_query": USER_QUERY},
    )
    print(f"[init] session.state.user_query = {session.state.get('user_query')!r}")

    msg = types.Content(role="user", parts=[types.Part(text=USER_QUERY)])
    seen_authors: list[str] = []
    async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=msg):
        author = getattr(event, "author", "?")
        text_parts = []
        if event.content and event.content.parts:
            text_parts = [p.text or "" for p in event.content.parts if hasattr(p, "text")]
        text = " ".join(text_parts).strip()
        if text:
            print(f"[event] author={author}  text={text[:120]}")
        seen_authors.append(author)

    # 最終 state を取得
    final = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=USER_ID, session_id=SESSION_ID
    )
    state = final.state

    print()
    print("=== verifications ===")
    checks: list[tuple[str, bool, str]] = [
        ("system1 が author に現れた", "system1" in seen_authors, str(seen_authors)),
        ("system2 が author に現れた", "system2" in seen_authors, str(seen_authors)),
        ("synthesizer が author に現れた", "synthesizer" in seen_authors, str(seen_authors)),
        ("state.system1_output が書き込まれた", "system1_output" in state, ""),
        ("state.system2_output が書き込まれた", "system2_output" in state, ""),
        ("state.final_response が書き込まれた", "final_response" in state, ""),
    ]
    failed = 0
    for name, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  ({detail})" if not ok else ""))
        if not ok:
            failed += 1

    if "final_response" in state:
        align = state["final_response"].get("alignment_comment", "")
        print()
        print(f"[stub final] alignment_comment = {align}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
