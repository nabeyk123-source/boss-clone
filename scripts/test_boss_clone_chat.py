"""boss_clone_chat.py のマルチターン動作 e2e 確認（非対話モード）。

3パターン:
- A: 一致シナリオ（kabe 新機能リリース）
- B: 不一致シナリオ（顧客要望保留）
- C: 複雑シナリオ（セキュリティレビュー）

各パターンで:
- System1 + System2 が並列で動く
- System2 はバックグラウンドで継続
- Synthesizer がユーザー回答を反映
- 体感待ち時間（Turn1 表示まで + Turn2 統合まで）を計測
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

import boss_clone_chat  # noqa: E402
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402


SCENARIOS = [
    {
        "name": "A_kabe_release",
        "query": "来月、kabe-anon の新機能をリリースしたいです",
        # 回答は質問数に応じて先頭から使う
        "answers": [
            "kabe v3 の新規ダッシュボード機能、ユーザー数を 15% 拡大したい",
            "リリース日は来月25日、社内レビューは来週完了予定",
            "想定 KPI は MAU +10% / 滞在時間 +20%",
        ],
    },
    {
        "name": "B_customer_hold",
        "query": "顧客の追加要望を一旦保留にしたいけど大丈夫？",
        "answers": [
            "戦略顧客の A社、契約金額は年 3000万",
            "要望は分析ダッシュボードの追加、工数 40人日見込み",
            "代替提案として既存 Lite 機能の活用を提示済み",
        ],
    },
    {
        "name": "C_security_review",
        "query": "新サービスにセキュリティレビューが必要かどうか判断したい",
        "answers": [
            "顧客の決済データを扱う、本人確認情報も含む",
            "外部 SaaS（決済代行）と API 連携あり",
            "リリース予定は3ヶ月後",
        ],
    },
]


async def run_scenario(retrieval: RetrievalService, scenario: dict) -> dict:
    print()
    print("=" * 72)
    print(f"シナリオ: {scenario['name']}")
    print(f"クエリ:   {scenario['query']}")
    print("=" * 72)

    # System1 + System2 並列起動
    t0 = time.perf_counter()

    s1_agent = boss_clone_chat.System1Agent(name="system1", description="直感")
    s1_agent.set_retrieval(retrieval)
    s2_agent = boss_clone_chat.System2Agent(name="system2", description="熟考")
    s2_agent.set_retrieval(retrieval)

    s2_task = asyncio.create_task(
        boss_clone_chat._run_single_agent(
            s2_agent, "watanabe", f"test-{scenario['name']}-s2",
            {"user_query": scenario["query"]}, scenario["query"],
        )
    )

    t_s1 = time.perf_counter()
    s1_state = await boss_clone_chat._run_single_agent(
        s1_agent, "watanabe", f"test-{scenario['name']}-s1",
        {"user_query": scenario["query"]}, scenario["query"],
    )
    s1_elapsed = time.perf_counter() - t_s1
    s1_out = s1_state.get("system1_output", {})

    questions = s1_out.get("questions") or []
    print(f"[Turn 1 表示まで] {s1_elapsed:.1f}s ← 体感「即答」")
    print(f"  S1 結論: {s1_out.get('intuitive_conclusion')} (一致度: {s1_out.get('match_confidence')})")
    print(f"  S1 質問数: {len(questions)}")
    for i, q in enumerate(questions, 1):
        if isinstance(q, dict):
            qtext = q.get("question", "")
            opts = q.get("options") or []
            opt_disp = " / ".join(opts) if opts else "(自由入力)"
            print(f"    Q{i}: {qtext[:80]}")
            print(f"         選択肢: {opt_disp}")
        else:
            print(f"    Q{i}: {str(q)[:100]}")

    # ユーザー回答（自動）— v2 構造に変換
    # SCENARIOS の answers は free_text 想定。selected_options は空で渡す
    answers_raw = scenario["answers"][:len(questions)]
    answers = []
    for q_obj, ans_str in zip(questions, answers_raw):
        if isinstance(q_obj, dict):
            qtext = q_obj.get("question", "")
        else:
            qtext = str(q_obj)
        answers.append({
            "question": qtext,
            "selected_options": [],
            "free_text": ans_str,
        })
    print()
    print(f"  (ユーザー回答シミュレーション: {len(answers)} 件、すべて free_text)")

    # System2 await + Synthesizer
    t_wait = time.perf_counter()
    s2_state = await s2_task
    s2_wait = time.perf_counter() - t_wait
    s2_out = s2_state.get("system2_output", {})

    syn_agent = boss_clone_chat.SynthesizerAgent(name="synthesizer", description="統合")
    t_syn = time.perf_counter()
    syn_state = await boss_clone_chat._run_single_agent(
        syn_agent, "watanabe", f"test-{scenario['name']}-syn",
        {
            "user_query": scenario["query"],
            "system1_output": s1_out,
            "system2_output": s2_out,
            "user_answers": answers,
        },
        scenario["query"],
    )
    syn_elapsed = time.perf_counter() - t_syn
    final = syn_state.get("final_response", {})

    total_wall = time.perf_counter() - t0
    # 体感待ち時間 = Turn1 表示まで + Turn2 統合まで（ユーザー応答時間は除外）
    perceived_latency = s1_elapsed + max(s2_wait, 0) + syn_elapsed
    perceived_no_userwait = s1_elapsed + syn_elapsed  # ユーザーが回答してる間に S2 が完走している仮定

    print()
    print(f"[Turn 2 表示まで] System2 wait={s2_wait*1000:.0f}ms + Synthesizer={syn_elapsed:.1f}s")
    print(f"  S2 結論: {s2_out.get('thoughtful_conclusion')} / 論点{len(s2_out.get('issues') or [])}件")
    print(f"  Synthesizer alignment: {final.get('alignment')}")
    print()
    print("--- Synthesizer raw（先頭 25 行）---")
    for line in (final.get("raw_response") or "").splitlines()[:25]:
        print(f"  | {line}")
    print()
    print(f"⏱️  実時間 wall total: {total_wall:.1f}s")
    print(f"⏱️  体感待ち時間（理想：S2が完走済み）: {perceived_no_userwait:.1f}s "
          f"({s1_elapsed:.1f}s + {syn_elapsed:.1f}s)")
    return {
        "total_wall": total_wall,
        "s1_elapsed": s1_elapsed,
        "s2_wait": s2_wait,
        "syn_elapsed": syn_elapsed,
        "perceived": perceived_no_userwait,
        "alignment": final.get("alignment"),
        "s1_conclusion": s1_out.get("intuitive_conclusion"),
        "s2_conclusion": s2_out.get("thoughtful_conclusion"),
        "n_questions": len(questions),
        "raw_final": final.get("raw_response") or "",
    }


async def main() -> int:
    retrieval = RetrievalService()
    print("[init] embed warmup...", end="", flush=True)
    try:
        await retrieval.embed_query("warmup")
        print(" OK")
    except Exception as e:  # noqa: BLE001
        print(f" failed: {e}")

    results = []
    for sc in SCENARIOS:
        r = await run_scenario(retrieval, sc)
        results.append((sc["name"], r))

    print()
    print("=" * 72)
    print("📊 全シナリオ サマリ")
    print("=" * 72)
    print(f"{'シナリオ':<22}{'s1_concl':<12}{'s2_concl':<14}{'align':<11}{'体感':>6}{'実時間':>8}{'Q数':>4}")
    for name, r in results:
        print(
            f"{name:<22}{(r['s1_conclusion'] or '?'):<12}{(r['s2_conclusion'] or '?'):<14}"
            f"{(r['alignment'] or '?'):<11}{r['perceived']:>5.1f}s{r['total_wall']:>7.1f}s{r['n_questions']:>4}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
