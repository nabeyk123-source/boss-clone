"""Watanabe Boss Clone Coordinator: ParallelAgent + SequentialAgent 配線。

仕様: docs/multi_agent_spec.md §3.4

Step 1 はスタブ Agent で配線確認、Step 3-5 で本実装に差し替え。
"""
from __future__ import annotations

from google.adk.agents import ParallelAgent, SequentialAgent

from .agents.synthesizer_agent import SynthesizerAgent
from .agents.system1_agent import System1Agent
from .agents.system2_agent import System2Agent


def build_boss_clone(
    *,
    system1: System1Agent | None = None,
    system2: System2Agent | None = None,
    synthesizer: SynthesizerAgent | None = None,
) -> SequentialAgent:
    """3エージェントを ADK で配線して 1 つの SequentialAgent を返す。

    Step 1 スタブを既定値とし、Step 3-5 で実装に差し替える際は引数で渡す。
    """
    s1 = system1 or System1Agent(
        name="system1",
        description="わたなべ部長の直感を担当（過去判断パターンから即答）",
    )
    s2 = system2 or System2Agent(
        name="system2",
        description="わたなべ部長の熟考を担当（Acme KB を網羅的に分析）",
    )
    syn = synthesizer or SynthesizerAgent(
        name="synthesizer",
        description="System1/System2 の結論を統合してわたなべ部長スタイルで応答",
    )

    parallel = ParallelAgent(
        name="parallel_thinking",
        description="System1 と System2 を並列で走らせる",
        sub_agents=[s1, s2],
    )
    return SequentialAgent(
        name="watanabe_boss_clone",
        description="わたなべ部長クローン（直感→熟考→統合）",
        sub_agents=[parallel, syn],
    )
