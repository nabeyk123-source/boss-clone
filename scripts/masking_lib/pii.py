"""PII（メール・電話・カード番号）の正規表現マスキング。

仕様: masking_pipeline_spec.md §4.5
辞書の `pii_patterns` を読み、`_` で始まる説明キーは無視する。
"""
from __future__ import annotations

import re
from typing import Pattern


def compile_pii_patterns(pii_patterns: dict) -> list[tuple[str, Pattern[str], str]]:
    """[(name, compiled_regex, replace_with), ...] にコンパイル。"""
    compiled: list[tuple[str, Pattern[str], str]] = []
    for name, spec in (pii_patterns or {}).items():
        if name.startswith("_") or not isinstance(spec, dict):
            continue
        regex = spec.get("regex")
        replace_with = spec.get("replace_with", "[MASKED]")
        if not regex:
            continue
        compiled.append((name, re.compile(regex), replace_with))
    return compiled


def mask_pii(text: str, compiled: list[tuple[str, Pattern[str], str]]) -> tuple[str, list[dict]]:
    masks_applied: list[dict] = []
    out = text
    for name, pat, replace_with in compiled:
        new, count = pat.subn(replace_with, out)
        if count:
            masks_applied.append({"type": name, "count": count, "replace_with": replace_with})
            out = new
    return out, masks_applied
