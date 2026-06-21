"""トピックタグ付与。

仕様: schema_spec.md §5.1 Step 3
- ルールベース優先（プロジェクト名キーワード）でヒットしたら Gemini を呼ばない
- ルール非該当のみ Gemini で 1〜3 タグを生成
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

# ルールベース：キーワード（小文字化済み） → タグ
# 辞書 v1.1.1 のマスク後文字列で当てる前提。masked データは Acme Corp 語彙に正規化されている
RULES: list[tuple[tuple[str, ...], str]] = [
    (("kabe-anon", "kabe"), "kabe"),
    (("mealassist", "ランチケ"), "lunchke"),
    (("ロイヤルティpayプラス", "valuegift"), "valuegift"),
    (("buspay-anon", "tourpay"), "tourpay"),
    (("shareholder-anon", "kabuyu"), "kabuyu"),
    (("nabechat-anon",), "nabechat"),
    (("regulation-updater-anon",), "regulation"),
    (("vector search", "vertex ai", "embedding"), "vector_search"),
    (("firestore",), "firestore"),
    (("cloud run",), "cloud_run"),
    (("adk", "agent"), "agent_dev"),
    (("hackathon", "ハッカソン"), "hackathon"),
    (("acme corp", "northstar"), "acme_corp"),
    (("加藤部長", "森田社長", "高瀬副社長", "富永顧問"), "acme_people"),
    (("デザイン", "design", "ui", "画面"), "design"),
    (("セキュリティ", "pii", "個人情報", "マスキング"), "security"),
    (("コスト", "料金", "credit"), "cost"),
    (("テスト", "test", "単体テスト"), "testing"),
    (("デプロイ", "deploy"), "deploy"),
]


@dataclass
class TopicConfig:
    model: str = "gemini-2.5-flash"
    max_concurrency: int = 4
    max_retries: int = 3
    initial_backoff_s: float = 1.0
    char_cap: int = 500
    thinking_budget: int = 0
    max_output_tokens: int = 60
    fallback_tag: str = "general"


PROMPT = """以下は対話ペアの要約です。この内容を表すトピックタグを 1〜3 個、英小文字スネークケースで挙げてください。

要約: {summary}

出力形式（カンマ区切り、1〜3個、それ以外は出力しない）:"""


def rule_based_tags(summary: str) -> list[str]:
    s = (summary or "").lower()
    hits: list[str] = []
    for keywords, tag in RULES:
        if any(kw in s for kw in keywords):
            if tag not in hits:
                hits.append(tag)
        if len(hits) >= 3:
            break
    return hits


def _build_client():
    from google import genai
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT 未設定")
    return genai.Client(vertexai=True, project=project, location=location)


def _parse_tags(text: str) -> list[str]:
    raw = (text or "").strip().splitlines()[0] if text else ""
    parts = [p.strip().strip(".`、,") for p in raw.replace("、", ",").split(",") if p.strip()]
    # ASCII 英小文字スネークのみ採用（日本語等は除外）
    out: list[str] = []
    for p in parts:
        if not p.isascii():
            continue
        norm = "".join(c if (c.isascii() and (c.isalnum() or c == "_")) else "_" for c in p.lower())
        norm = norm.strip("_")
        if 2 <= len(norm) <= 30 and norm not in out:
            out.append(norm)
        if len(out) >= 3:
            break
    return out


async def _llm_tag_one(
    client,
    sem: asyncio.Semaphore,
    cfg: TopicConfig,
    summary: str,
) -> tuple[list[str], str, float]:
    prompt = PROMPT.format(summary=(summary or "")[: cfg.char_cap])
    async with sem:
        backoff = cfg.initial_backoff_s
        t_start = time.perf_counter()
        for attempt in range(cfg.max_retries):
            try:
                resp = await client.aio.models.generate_content(
                    model=cfg.model,
                    contents=prompt,
                    config={
                        "max_output_tokens": cfg.max_output_tokens,
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": cfg.thinking_budget},
                    },
                )
                latency = time.perf_counter() - t_start
                tags = _parse_tags(getattr(resp, "text", "") or "")
                if not tags:
                    return [cfg.fallback_tag], "empty", latency
                return tags, "ok", latency
            except Exception as e:  # noqa: BLE001
                if attempt == cfg.max_retries - 1:
                    return [cfg.fallback_tag], f"retry_exhausted:{type(e).__name__}", time.perf_counter() - t_start
                await asyncio.sleep(backoff)
                backoff *= 2
    return [cfg.fallback_tag], "loop_exit", time.perf_counter() - t_start


async def classify_topics_many(
    summaries: list[str],
    cfg: TopicConfig | None = None,
    progress_cb=None,
) -> list[tuple[list[str], str, float]]:
    """ルールベース優先。ヒットしたものは LLM を呼ばない（コスト削減）。"""
    cfg = cfg or TopicConfig()
    results: list[tuple[list[str], str, float] | None] = [None] * len(summaries)
    llm_targets: list[tuple[int, str]] = []

    for i, s in enumerate(summaries):
        rb = rule_based_tags(s)
        if rb:
            results[i] = (rb, "rule_based", 0.0)
        else:
            llm_targets.append((i, s))

    if llm_targets:
        client = _build_client()
        sem = asyncio.Semaphore(cfg.max_concurrency)
        done_llm = 0
        total_llm = len(llm_targets)

        async def wrapper(idx_summary: tuple[int, str]) -> tuple[int, tuple[list[str], str, float]]:
            nonlocal done_llm
            idx, summary = idx_summary
            r = await _llm_tag_one(client, sem, cfg, summary)
            done_llm += 1
            if progress_cb and (done_llm % 25 == 0 or done_llm == total_llm):
                progress_cb(done_llm, total_llm)
            return idx, r

        gathered = await asyncio.gather(*(wrapper(t) for t in llm_targets))
        for idx, r in gathered:
            results[idx] = r

    # 残り（rule_based でも LLM でも埋まらなかったケース）は fallback
    final: list[tuple[list[str], str, float]] = []
    for r in results:
        final.append(r if r is not None else ([cfg.fallback_tag], "skipped", 0.0))
    return final
