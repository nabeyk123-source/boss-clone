"""わたなべ部長クローン マルチターン CLI。

設計（仕様書 §5.1 + Day3 Step 7 マルチターン拡張）:

Turn 1 のフロー:
  1. ユーザーが相談を入力
  2. System1 を即時実行（3-5秒、Flash）→ 「確認質問」を生成
  3. **同じタイミングで System2 をバックグラウンド実行**（asyncio.create_task、17秒）
  4. System1 完了次第、確認質問をユーザーに表示
  5. ユーザーが回答を入力する間に System2 がバックグラウンドで完走

Turn 2 のフロー:
  6. ユーザーが質問への回答を入力
  7. System2 タスクを await（既に完了済みの可能性大）
  8. Synthesizer を実行（16秒、Pro）→ 統合判断を表示
  9. 次の相談へ（exit で終了）

体感待ち時間 = max(Turn 1 の3-5秒, Turn 2 の16秒) ≒ 16秒
（ユーザーが質問に答える 10-30秒の間に System2 17秒が並列で進行）
"""
from __future__ import annotations

import argparse
import asyncio
import re
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
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402

USER_ID = "watanabe"
APP_NAME = "boss_clone"


async def _run_single_agent(agent, user_id: str, session_id: str, state: dict, query: str):
    """1 つのエージェントだけを InMemoryRunner で実行して、最終 state を返す。"""
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id, state=state,
    )
    msg = types.Content(role="user", parts=[types.Part(text=query)])
    async for _ in runner.run_async(user_id=user_id, session_id=session_id, new_message=msg):
        pass
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id,
    )
    return sess.state


def _print_section(title: str, color: str = "") -> None:
    print()
    print(f"────── {title} ──────")


def _format_concerns(items) -> str:
    return " / ".join(items) if items else "(なし)"


async def _input_async(prompt: str) -> str:
    """ブロッキング input をスレッド経由で async に。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


def _parse_choice_input(raw: str, options: list[str]) -> tuple[list[str], str | None, str | None]:
    """ユーザー入力をパース。

    返り値: (selected_options, free_text, error)
    - selected_options: 選択された options 文字列のリスト
    - free_text: 自由入力（または None）
    - error: 範囲外などの再入力すべきエラー（または None）

    ルール:
    - 空入力 → ([], None, None) でスキップ扱い
    - 「1,3」のような番号カンマ区切り → 該当 options を selected_options に
    - N+1 番号（=「その他」スロット）→ free_text 入力フラグ "__OTHER__" を返す
    - 数字以外で始まる入力 → そのまま free_text 扱い
    - 範囲外番号 → error を返して再入力
    """
    s = (raw or "").strip()
    if not s:
        return [], None, None

    n_real = len(options)
    other_slot = n_real + 1  # 「その他」の番号

    # 「1,3」「1 3」「1、3」のようなカンマ/空白/全角カンマ区切りを許容
    tokens = [t.strip() for t in re.split(r"[,、\s]+", s) if t.strip()]
    all_numeric = bool(tokens) and all(t.isdigit() for t in tokens)

    if not all_numeric:
        # 数字以外で始まる → 全文を自由入力として扱う
        return [], s, None

    nums: list[int] = []
    for t in tokens:
        try:
            nums.append(int(t))
        except ValueError:
            return [], None, f"番号として解釈できません: {t}"

    selected: list[str] = []
    want_other = False
    for n in nums:
        if 1 <= n <= n_real:
            opt = options[n - 1]
            if opt not in selected:
                selected.append(opt)
        elif n == other_slot:
            want_other = True
        else:
            return [], None, f"範囲外の番号: {n}（有効: 1〜{other_slot}）"

    return selected, ("__OTHER__" if want_other else None), None


async def _collect_one_answer(idx: int, question_obj: dict) -> dict:
    """1 質問分の回答をユーザーから取る。{question, selected_options, free_text} を返す。"""
    question = question_obj.get("question", "")
    options = list(question_obj.get("options") or [])

    print(f"  Q{idx}: {question}")
    if options:
        for i, opt in enumerate(options, 1):
            print(f"    [{i}] {opt}")
        print(f"    [{len(options) + 1}] その他（自由入力）")
        hint = f"番号をカンマ区切り（例: 1,3）、または自由入力、空 Enter でスキップ"
    else:
        hint = "自由入力、空 Enter でスキップ"
    while True:
        raw = await _input_async(f"  → {hint}: ")
        selected, free_marker, err = _parse_choice_input(raw, options)
        if err:
            print(f"    ⚠️  {err}、もう一度入力してください")
            continue
        free_text: str | None = None
        if free_marker == "__OTHER__":
            other_raw = await _input_async(f"    → 「その他」の内容を入力: ")
            free_text = other_raw.strip() or None
        elif free_marker is not None:
            free_text = free_marker
        return {
            "question": question,
            "selected_options": selected,
            "free_text": free_text,
        }


def _is_answered(ans: dict) -> bool:
    return bool(ans.get("selected_options")) or bool(ans.get("free_text"))


async def conversation_turn(retrieval: RetrievalService, turn_idx: int, user_query: str) -> dict:
    """1 ラウンドの相談（Turn 1 → 質問 → ユーザー回答 → Turn 2）を回す。"""
    # ===== Turn 1: System1 即時実行 + System2 バックグラウンド =====
    print()
    print("🤖 わたなべ部長クローンが考え中…")
    t_total = time.perf_counter()

    s1_agent = System1Agent(name="system1", description="直感（過去判断パターン）")
    s1_agent.set_retrieval(retrieval)
    s2_agent = System2Agent(name="system2", description="熟考（Acme KB 網羅）")
    s2_agent.set_retrieval(retrieval)

    s1_session_id = f"chat-{turn_idx}-s1"
    s2_session_id = f"chat-{turn_idx}-s2"

    # ★System2 をバックグラウンドタスクで先に起動（待たない）
    s2_task = asyncio.create_task(
        _run_single_agent(
            s2_agent, USER_ID, s2_session_id,
            {"user_query": user_query}, user_query,
        )
    )

    # System1 を実行（こちらは即返答が欲しいので await）
    t_s1 = time.perf_counter()
    s1_state = await _run_single_agent(
        s1_agent, USER_ID, s1_session_id,
        {"user_query": user_query}, user_query,
    )
    s1_out = s1_state.get("system1_output", {})
    t_s1_elapsed = time.perf_counter() - t_s1

    # 結果表示
    _print_section("🧠 System1 の直感")
    print(f"直感的結論:   {s1_out.get('intuitive_conclusion')}（一致度: {s1_out.get('match_confidence')}）")
    refs = s1_out.get("reference_cases") or []
    if refs:
        print(f"根拠ケース:   {refs[0][:140]}")
    print(f"気になる点:   {_format_concerns(s1_out.get('concerns') or [])}")
    print(f"  (System1 実時間: {t_s1_elapsed:.1f}s、System2 はバックグラウンドで継続中)")

    # 確認質問
    questions = s1_out.get("questions") or []
    user_answers: list[dict] = []
    if not questions:
        print()
        print("⚠️  System1 が確認質問を生成しませんでした。Synthesizer に直接進みます。")
    else:
        _print_section("🙋 確認させてください")
        print()
        for i, q_obj in enumerate(questions, 1):
            # questions は v2 で [{question, options}] 形式（v1 互換: str も受ける）
            if isinstance(q_obj, str):
                q_obj = {"question": q_obj, "options": []}
            ans = await _collect_one_answer(i, q_obj)
            user_answers.append(ans)
            print()

    # ===== Turn 2: System2 を await + Synthesizer =====
    print()
    print("🤖 直感・熟考・あなたの回答を統合中…")
    t_wait = time.perf_counter()
    s2_state = await s2_task
    s2_wait = time.perf_counter() - t_wait
    s2_out = s2_state.get("system2_output", {})
    s2_latency = s2_out.get("_latency_s", 0)
    print(f"  (System2 待ち: {s2_wait*1000:.0f}ms、System2 内 LLM: {s2_latency*1000:.0f}ms)")

    # Synthesizer 実行
    syn_agent = SynthesizerAgent(name="synthesizer", description="統合")
    syn_session_id = f"chat-{turn_idx}-syn"
    t_syn = time.perf_counter()
    syn_state = await _run_single_agent(
        syn_agent, USER_ID, syn_session_id,
        {
            "user_query": user_query,
            "system1_output": s1_out,
            "system2_output": s2_out,
            "user_answers": user_answers,
        },
        user_query,
    )
    syn_elapsed = time.perf_counter() - t_syn
    final = syn_state.get("final_response", {})

    _print_section("🎯 わたなべ部長として")
    print(final.get("raw_response", "").strip())
    print()
    print(f"  (Synthesizer: {syn_elapsed:.1f}s、システム待ち合計: {time.perf_counter() - t_total:.1f}s)")

    return {
        "system1": s1_out,
        "system2": s2_out,
        "final": final,
        "latency": {
            "system1": t_s1_elapsed,
            "system2_wait": s2_wait,
            "synthesizer": syn_elapsed,
            "total_wall": time.perf_counter() - t_total,
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="わたなべ部長クローン マルチターン CLI")
    parser.add_argument("--query", type=str, help="1ターンだけ実行（自動テスト用）")
    parser.add_argument("--answers", nargs="*", help="--query と組み合わせて非対話で実行（質問への回答）")
    args = parser.parse_args()

    print("🤖 わたなべ部長クローン（マルチターン CLI）")
    print("    終了は 'exit' / 'quit' / 空 Enter")
    print()

    retrieval = RetrievalService()
    # Embedding のコールドスタートを起動時に潰す
    print("[init] embedding warmup...", end="", flush=True)
    try:
        await retrieval.embed_query("warmup")
        print(" OK")
    except Exception as e:  # noqa: BLE001
        print(f" failed: {e}")

    turn = 0
    if args.query:
        # 非対話モード（テスト用）：質問への回答は --answers から
        # Turn 1 の質問数が不明なので、与えられた回答を順に使う
        await _run_noninteractive(retrieval, args.query, args.answers or [])
        return 0

    while True:
        try:
            user_query = (await _input_async("あなた> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_query or user_query.lower() in {"exit", "quit", ":q"}:
            print("お疲れさまでした。")
            break
        turn += 1
        try:
            await conversation_turn(retrieval, turn, user_query)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {type(e).__name__}: {e}")
        print()

    return 0


async def _run_noninteractive(retrieval: RetrievalService, query: str, answers: list[str]) -> None:
    """テスト用：自動で質問へ回答する non-interactive モード。"""
    print(f"あなた> {query}")
    print()
    print("🤖 わたなべ部長クローンが考え中…")
    t_total = time.perf_counter()

    s1_agent = System1Agent(name="system1", description="直感")
    s1_agent.set_retrieval(retrieval)
    s2_agent = System2Agent(name="system2", description="熟考")
    s2_agent.set_retrieval(retrieval)

    s2_task = asyncio.create_task(
        _run_single_agent(s2_agent, USER_ID, "ni-s2", {"user_query": query}, query)
    )

    t_s1 = time.perf_counter()
    s1_state = await _run_single_agent(s1_agent, USER_ID, "ni-s1", {"user_query": query}, query)
    s1_out = s1_state.get("system1_output", {})
    print(f"[S1 完了] {time.perf_counter() - t_s1:.1f}s, 結論={s1_out.get('intuitive_conclusion')}")
    print(f"  質問:")
    for i, q in enumerate(s1_out.get("questions") or [], 1):
        a = answers[i - 1] if i - 1 < len(answers) else ""
        print(f"    Q{i}: {q}")
        print(f"    A{i}: {a or '(空)'}")

    t_wait = time.perf_counter()
    s2_state = await s2_task
    s2_out = s2_state.get("system2_output", {})
    print(f"[S2 await] 待ち {(time.perf_counter() - t_wait)*1000:.0f}ms, "
          f"LLM内部 {s2_out.get('_latency_s', 0):.1f}s, 結論={s2_out.get('thoughtful_conclusion')}")

    syn_agent = SynthesizerAgent(name="synthesizer", description="統合")
    t_syn = time.perf_counter()
    syn_state = await _run_single_agent(
        syn_agent, USER_ID, "ni-syn",
        {
            "user_query": query,
            "system1_output": s1_out,
            "system2_output": s2_out,
            "user_answers": answers,
        },
        query,
    )
    syn_elapsed = time.perf_counter() - t_syn
    final = syn_state.get("final_response", {})
    print(f"[Synthesizer] {syn_elapsed:.1f}s, alignment={final.get('alignment')}")
    print()
    print("=== 統合判断 ===")
    print(final.get("raw_response", "").strip())
    print()
    print(f"[計] total wall {time.perf_counter() - t_total:.1f}s")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
