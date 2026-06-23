"""System1 の JSON 選択肢出力（TODO-P8 Step 1-1）動作確認。

3 シナリオで:
- LLM が JSON 配列で {question, options} を返すか
- options が 2〜4 個に収まるか
- 質問が具体的か
- パース失敗時に fallback で options=[] が入るか
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

from boss_clone_lib.agents.system1_agent import System1Agent, _extract_questions  # noqa: E402
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402


SCENARIOS = [
    "来月、kabe-anon の新機能をリリースしたいです",
    "顧客の追加要望を一旦保留にしたいけど大丈夫？",
    "新サービスにセキュリティレビューが必要かどうか判断したい",
]


# --- 単体: parser ---

def parser_unit_tests() -> int:
    failures = 0
    # 1) 正しい JSON ブロック
    raw = """直感的結論: 採用
過去パターンとの一致度: 高
根拠となる過去ケース: #1 ...
直感的に気になる点: 時期

確認質問:
```json
[
  {"question": "リリース予定日は？", "options": ["来月25日", "再来月", "未定"]},
  {"question": "重視したい観点は？", "options": ["スピード", "品質", "コスト"]}
]
```
"""
    qs = _extract_questions(raw)
    if not (len(qs) == 2 and qs[0]["question"] == "リリース予定日は？" and len(qs[0]["options"]) == 3):
        print(f"  FAIL parser: 正常 JSON / got={qs}")
        failures += 1
    else:
        print(f"  ok parser: 正常 JSON ({len(qs)} questions, options OK)")

    # 2) JSON が壊れて legacy fallback
    raw2 = """確認質問:
1. リリース日は？
2. 予算は？
"""
    qs2 = _extract_questions(raw2)
    if not (len(qs2) == 2 and qs2[0]["options"] == []):
        print(f"  FAIL parser: legacy fallback / got={qs2}")
        failures += 1
    else:
        print(f"  ok parser: legacy fallback ({len(qs2)} questions, options=[])")

    # 3) ```json なしの素 JSON
    raw3 = """確認質問:
[
  {"question": "Q1?", "options": ["A", "B"]}
]
"""
    qs3 = _extract_questions(raw3)
    if not (len(qs3) == 1 and qs3[0]["options"] == ["A", "B"]):
        print(f"  FAIL parser: 素 JSON / got={qs3}")
        failures += 1
    else:
        print(f"  ok parser: 素 JSON")

    # 4) options が 5 個 → 上位 4 個で切る
    raw4 = """確認質問:
```json
[{"question": "Q?", "options": ["a","b","c","d","e","f"]}]
```
"""
    qs4 = _extract_questions(raw4)
    if not (len(qs4) == 1 and qs4[0]["options"] == ["a", "b", "c", "d"]):
        print(f"  FAIL parser: options 切り捨て / got={qs4}")
        failures += 1
    else:
        print(f"  ok parser: options 上位 4 個に制限")
    return failures


# --- 統合: LLM 経由 ---

async def integration_one(query: str, idx: int, retrieval: RetrievalService) -> dict:
    agent = System1Agent(name="system1", description="直感")
    agent.set_retrieval(retrieval)
    runner = InMemoryRunner(agent=agent, app_name="boss_clone")
    sid = f"s1-choices-{idx}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="watanabe", session_id=sid,
        state={"user_query": query},
    )
    msg = types.Content(role="user", parts=[types.Part(text=query)])
    t0 = time.perf_counter()
    async for _ in runner.run_async(user_id="watanabe", session_id=sid, new_message=msg):
        pass
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="watanabe", session_id=sid,
    )
    out = sess.state.get("system1_output", {})
    out["_total_s"] = time.perf_counter() - t0
    return out


async def main() -> int:
    print("=== parser 単体テスト ===")
    p_fail = parser_unit_tests()

    print()
    print("=== LLM 統合（3 シナリオ）===")
    retrieval = RetrievalService()
    print("[warmup] embed...", end="", flush=True)
    await retrieval.embed_query("warmup")
    print(" OK")
    print()

    integration_fail = 0
    for i, q in enumerate(SCENARIOS, 1):
        print(f"--- Q{i}: {q} ---")
        out = await integration_one(q, i, retrieval)
        latency = out.get("_latency_s", 0)
        total = out.get("_total_s", 0)
        questions = out.get("questions") or []
        print(f"  latency LLM={latency*1000:.0f}ms / total={total*1000:.0f}ms")
        print(f"  conclusion={out.get('intuitive_conclusion')} / pairs={out.get('_retrieved_pairs_n')}")
        if not questions:
            print(f"  FAIL: 質問が 0 件")
            integration_fail += 1
            continue
        for j, q_obj in enumerate(questions, 1):
            qtext = q_obj.get("question", "")
            opts = q_obj.get("options") or []
            opt_str = " / ".join(opts) if opts else "(自由入力)"
            mark = ""
            if not opts:
                mark = " ⚠️ options 空（fallback or LLM が JSON 出さなかった）"
            elif len(opts) < 2 or len(opts) > 4:
                mark = f" ⚠️ options 数 {len(opts)} (期待 2〜4)"
            print(f"    Q{j}: {qtext}")
            print(f"         選択肢: {opt_str}{mark}")
            if not opts:
                integration_fail += 1
        print()

    print()
    print("=== 集計 ===")
    print(f"  parser fail: {p_fail} / 4")
    print(f"  integration fail (options 空 / 質問 0): {integration_fail}")
    return 0 if (p_fail == 0 and integration_fail == 0) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
