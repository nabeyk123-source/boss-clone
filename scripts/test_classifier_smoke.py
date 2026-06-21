"""classifier.py の smoke test。

3サンプル（OK/NG/保留を期待）を Vertex AI Gemini 2.5 Flash に投げて、
- 接続が通ること
- 応答が VALID_TAGS のいずれかに正規化されること
- 並列実行ロジックが回ること
を確認する。1呼び出しあたり数十トークン、合計コストは事実上ゼロ。

フル処理（--no--classify を外したフルラン）の前に実行して、Vertex AI 経路の健全性を確かめる目的。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

for s in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from masking_lib.classifier import ClassifyConfig, classify_many  # noqa: E402

SAMPLES: list[tuple[str, str]] = [
    ("OK", "ありがとう、その案で進めて。"),
    ("NG", "いや、それは違う。別の方向で考え直してほしい。"),
    ("保留", "もう少し詳しく教えてくれる？前提が一つ抜けている気がする。"),
]


def main() -> int:
    print(f"[smoke] {len(SAMPLES)} samples, model=gemini-2.5-flash, concurrency=2")
    cfg = ClassifyConfig(max_concurrency=2)

    t0 = time.perf_counter()
    results = asyncio.run(classify_many([s[1] for s in SAMPLES], cfg))
    dt = time.perf_counter() - t0

    ok = 0
    valid_tags = {"OK", "NG", "保留"}
    for (expected, sample), (tag, status, latency) in zip(SAMPLES, results):
        match = "MATCH" if tag == expected else "diff"
        in_set = "valid" if tag in valid_tags else "INVALID"
        print(f"  expected={expected:3s}  got={tag:3s}  status={status:18s}  latency={latency*1000:>5.0f}ms  [{match}/{in_set}]")
        print(f"    input: {sample}")
        if tag in valid_tags:
            ok += 1

    print()
    print(f"[result] {ok}/{len(SAMPLES)} valid tag, {dt:.2f}s elapsed")
    print(f"[verdict] {'PASS - Vertex AI 接続OK、フル処理に進める' if ok == len(SAMPLES) else 'FAIL - 要調査'}")
    return 0 if ok == len(SAMPLES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
