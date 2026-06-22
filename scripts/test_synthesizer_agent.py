"""Synthesizer Agent 単体テスト（一致 / 不一致パターン）+ end-to-end 統合動作確認。

Step 5: モック s1/s2 の state を与えて Synthesizer 単体動作 + 一致／不一致の差分
Step 6: 実エージェント3体で end-to-end、レイテンシ計測
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

from boss_clone_lib.agents.synthesizer_agent import SynthesizerAgent  # noqa: E402
from boss_clone_lib.agents.system1_agent import System1Agent  # noqa: E402
from boss_clone_lib.agents.system2_agent import System2Agent  # noqa: E402
from boss_clone_lib.coordinator import build_boss_clone  # noqa: E402
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402


MOCK_ALIGNED = {
    "system1_output": {
        "intuitive_conclusion": "条件付き保留",
        "match_confidence": "高",
        "reference_cases": ["過去ケース: kabe v3 リリース時の議論と類似"],
        "concerns": ["セキュリティレビューが事前にされてるか不明"],
    },
    "system2_output": {
        "thoughtful_conclusion": "条件付き保留",
        "issues": [
            {"title": "KPI整合性", "considerations": ["データ活用 PoC 30社 達成への寄与不明"], "trade_offs": []},
            {"title": "ガバナンス", "considerations": ["セキュリティ規程上、新機能リリース前にレビュー必須"], "trade_offs": []},
            {"title": "ROI", "considerations": ["開発工数 vs 想定収益の試算が必要"], "trade_offs": []},
        ],
        "verification_items": ["KPI寄与の数値見積もり", "セキュリティレビュー実施計画", "3年ROI試算"],
    },
}

MOCK_MISALIGNED = {
    "system1_output": {
        "intuitive_conclusion": "採用",
        "match_confidence": "高",
        "reference_cases": ["過去ケース: ランチケ追加機能と同パターン、過去はOK"],
        "concerns": [],
    },
    "system2_output": {
        "thoughtful_conclusion": "却下",
        "issues": [
            {"title": "戦略整合性", "considerations": ["2026年度KPIに直接寄与しない"], "trade_offs": []},
            {"title": "リスク", "considerations": ["顧客データ取扱い変更でセキュリティリスク増"], "trade_offs": []},
        ],
        "verification_items": ["KPI寄与の再評価", "セキュリティ影響評価"],
    },
}


async def run_synth_mock(name: str, mock: dict, user_query: str) -> dict:
    agent = SynthesizerAgent(name="synthesizer", description="統合エージェント")
    runner = InMemoryRunner(agent=agent)
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="watanabe",
        session_id=f"synth-{name}",
        state={"user_query": user_query, **mock},
    )
    msg = types.Content(role="user", parts=[types.Part(text=user_query)])
    t0 = time.perf_counter()
    async for _ in runner.run_async(user_id="watanabe", session_id=f"synth-{name}", new_message=msg):
        pass
    total = time.perf_counter() - t0
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="watanabe", session_id=f"synth-{name}"
    )
    out = sess.state.get("final_response", {})
    out["_total_s"] = total
    return out


async def run_e2e(user_query: str) -> dict:
    retrieval = RetrievalService()
    s1 = System1Agent(name="system1", description="直感")
    s1.set_retrieval(retrieval)
    s2 = System2Agent(name="system2", description="熟考")
    s2.set_retrieval(retrieval)
    syn = SynthesizerAgent(name="synthesizer", description="統合")
    boss = build_boss_clone(system1=s1, system2=s2, synthesizer=syn)
    runner = InMemoryRunner(agent=boss)
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="watanabe",
        session_id="e2e-test",
        state={"user_query": user_query},
    )
    msg = types.Content(role="user", parts=[types.Part(text=user_query)])
    t0 = time.perf_counter()
    async for _ in runner.run_async(user_id="watanabe", session_id="e2e-test", new_message=msg):
        pass
    total = time.perf_counter() - t0
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="watanabe", session_id="e2e-test"
    )
    return {
        "system1_output": sess.state.get("system1_output", {}),
        "system2_output": sess.state.get("system2_output", {}),
        "final_response": sess.state.get("final_response", {}),
        "_total_s": total,
    }


def _print_final(label: str, final: dict) -> None:
    print(f"  [{label}] alignment={final.get('alignment')} latency={final.get('_latency_s', 0)*1000:.0f}ms status={final.get('_llm_status')}")
    print(f"  issues_summary ({len(final.get('issues_summary') or [])}):")
    for t_ in final.get("issues_summary") or []:
        print(f"    - {t_}")
    print(f"  alignment_comment: {final.get('alignment_comment')}")
    print(f"  closing_remark:    {final.get('closing_remark')}")
    raw = final.get("raw_response", "")
    print("  --- raw (head 15 lines) ---")
    for line in raw.splitlines()[:15]:
        print(f"  | {line}")


async def main() -> int:
    USER_QUERY = "来月、新機能リリースしたいです"

    # ===== Step 5-A: 一致パターン =====
    print("===== Step 5-A: 一致パターン（s1/s2 とも 条件付き保留）=====")
    aligned = await run_synth_mock("aligned", MOCK_ALIGNED, USER_QUERY)
    _print_final("ALIGNED", aligned)
    print(f"  wall total: {aligned.get('_total_s', 0)*1000:.0f}ms")

    # ===== Step 5-B: 不一致パターン =====
    print()
    print("===== Step 5-B: 不一致パターン（s1=採用、s2=却下）=====")
    mis = await run_synth_mock("misaligned", MOCK_MISALIGNED, USER_QUERY)
    _print_final("MISALIGNED", mis)
    print(f"  wall total: {mis.get('_total_s', 0)*1000:.0f}ms")

    # ===== Step 6: end-to-end =====
    print()
    print("===== Step 6: end-to-end 統合動作確認 =====")
    e2e_query = "顧客の追加要望を一旦保留にしたいけど大丈夫？"
    print(f"  query: {e2e_query}")
    e2e = await run_e2e(e2e_query)
    s1 = e2e["system1_output"]
    s2 = e2e["system2_output"]
    final = e2e["final_response"]
    print(f"  total wall: {e2e['_total_s']*1000:.0f}ms")
    print(f"  S1: {s1.get('intuitive_conclusion')}（一致度: {s1.get('match_confidence')}) / "
          f"latency={s1.get('_latency_s', 0)*1000:.0f}ms")
    print(f"  S2: {s2.get('thoughtful_conclusion')} / 論点 {len(s2.get('issues') or [])}件 / "
          f"latency={s2.get('_latency_s', 0)*1000:.0f}ms")
    _print_final("E2E", final)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
