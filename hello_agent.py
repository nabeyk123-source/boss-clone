"""Hello, Agent - Day 1 動作確認用の対話エージェント「クロ」。"""
from __future__ import annotations

import asyncio
import sys

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

load_dotenv()

USER_ID = "watanabe"
SESSION_ID = "hello-session"

INSTRUCTION = """\
あなたは「クロ」。わたなべ部長の壁打ち相手です。

口調と振る舞い:
- 日本語で簡潔に応答する。冗長な前置きや謝辞は省く。
- 部長の発言に同調するだけでなく、論点を整理し、抜けや前提を質問で返す。
- 1回の返答は概ね3〜6文。箇条書きは必要な時だけ使う。

役割:
- 部長の判断を急かさず、まず状況を一段引いて捉え直す。
- 「で、何を決める必要がある？」を常に意識して問い返す。
"""


def build_agent() -> Agent:
    return Agent(
        name="kuro",
        model="gemini-2.5-flash",
        description="わたなべ部長の壁打ち相手（ハッカソン Day 1 動作確認用）",
        instruction=INSTRUCTION,
    )


async def chat() -> None:
    runner = InMemoryRunner(agent=build_agent())
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    print("クロ: こんにちは、わたなべさん。今日は何を一緒に整理しましょう？")
    print("(終了は :q または空Enter)\n")

    loop = asyncio.get_running_loop()
    while True:
        user_input = (await loop.run_in_executor(None, input, "あなた> ")).strip()
        if not user_input or user_input in {":q", "quit", "exit"}:
            print("クロ: お疲れさまでした。")
            break

        message = types.Content(role="user", parts=[types.Part(text=user_input)])
        print("クロ> ", end="", flush=True)
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=SESSION_ID,
            new_message=message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        print(part.text, end="", flush=True)
        print()


if __name__ == "__main__":
    try:
        asyncio.run(chat())
    except KeyboardInterrupt:
        print("\nクロ: お疲れさまでした。")
