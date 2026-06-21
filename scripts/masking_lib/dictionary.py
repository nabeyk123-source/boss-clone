"""変換辞書の読込・適用・ハッシュ計算。

仕様: masking_pipeline_spec.md §4.6
preserve_as_is を最初にプレースホルダー化 → 長語句優先で辞書適用 → preserve を復元、の3段構え。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CATEGORIES_TO_APPLY: tuple[str, ...] = (
    "company",
    "executives",
    "user_identity",
    "group_companies",
    "products",
    "departments",
    "external_companies",
)


def load_dictionary(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"masking_dictionary.json が見つかりません: {path}\n"
            f"docs/ に配置してください（.gitignore で保護されています）"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dict_hash(dictionary: dict) -> str:
    """辞書のべき等性ハッシュ。"""
    payload = json.dumps(dictionary, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def collect_known_terms(dictionary: dict) -> set[str]:
    """辞書に登録済みの全 source 用語 + preserve 用語のセット。

    `unknown_detector.detect_unknown_entities` で「未知扱いしない」フィルタに使う。
    """
    known: set[str] = set()
    for cat in CATEGORIES_TO_APPLY:
        known.update((dictionary.get(cat) or {}).keys())
        # target 側も「既知」扱い（マスク後テキストで誤検出しないため）
        known.update((dictionary.get(cat) or {}).values())
    preserve = (dictionary.get("preserve_as_is") or {}).get("terms") or []
    known.update(preserve)
    return known


def apply_dictionary(text: str, dictionary: dict) -> tuple[str, list[dict]]:
    """preserve_as_is を保護したうえでカテゴリ別に長語句優先で置換。

    返り値: (置換後テキスト, 置換明細リスト)
    各明細: {category, source, target, count}
    """
    replacements: list[dict] = []
    preserve_terms = list((dictionary.get("preserve_as_is") or {}).get("terms") or [])
    # 長い順に preserve（短い語句が長い語句の一部を喰わないよう）
    preserve_terms.sort(key=len, reverse=True)

    preserved: dict[str, str] = {}
    out = text
    for i, term in enumerate(preserve_terms):
        if term and term in out:
            placeholder = f"\x00PRESERVE_{i}\x01"
            preserved[placeholder] = term
            out = out.replace(term, placeholder)

    for category in CATEGORIES_TO_APPLY:
        terms = dictionary.get(category) or {}
        sorted_pairs = sorted(terms.items(), key=lambda kv: len(kv[0]), reverse=True)
        for source, target in sorted_pairs:
            if source and source in out:
                count = out.count(source)
                out = out.replace(source, target)
                replacements.append({
                    "category": category,
                    "source": source,
                    "target": target,
                    "count": count,
                })

    for placeholder, original in preserved.items():
        out = out.replace(placeholder, original)

    return out, replacements


def all_source_terms(dictionary: dict) -> list[str]:
    """エージェント出力の VD 用語混入チェック用に、全 source を返す。"""
    terms: list[str] = []
    for cat in CATEGORIES_TO_APPLY:
        terms.extend((dictionary.get(cat) or {}).keys())
    return [t for t in terms if t]
