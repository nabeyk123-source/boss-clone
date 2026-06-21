"""masking_dictionary.json の読み込みと変換ロジックの最小動作確認。

本格的なマスキングパイプラインは Day 2 タスク3 で実装する。
ここでは辞書が正しくロードでき、サンプル文字列で意図したとおりに置換されることだけ確認する。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DICT_PATH = ROOT / "docs" / "masking_dictionary.json"

# 置換対象として扱う辞書セクション（一般カテゴリ辞書）
TERM_SECTIONS = [
    "company",
    "executives",
    "user_identity",
    "group_companies",
    "products",
    "departments",
    "external_companies",
]


def load_dictionary(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"masking_dictionary.json が見つかりません: {path}\n"
            f"docs/ に配置してください（.gitignoreで保護されています）"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_term_pairs(d: dict) -> list[tuple[str, str]]:
    """セクション横断で (source, target) のリストを作る。長い source を先に並べる（部分一致衝突を避ける）。"""
    pairs: list[tuple[str, str]] = []
    for sec in TERM_SECTIONS:
        for src, dst in (d.get(sec) or {}).items():
            pairs.append((src, dst))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def apply_terms(text: str, pairs: list[tuple[str, str]], case_insensitive: bool) -> tuple[str, list[tuple[str, str, int]]]:
    """テキストに辞書を適用。マッチ件数も返す。"""
    hits: list[tuple[str, str, int]] = []
    out = text
    flags = re.IGNORECASE if case_insensitive else 0
    for src, dst in pairs:
        pattern = re.compile(re.escape(src), flags)
        new, count = pattern.subn(dst, out)
        if count:
            hits.append((src, dst, count))
            out = new
    return out, hits


def apply_pii_patterns(text: str, patterns: dict) -> tuple[str, list[tuple[str, str, int]]]:
    hits: list[tuple[str, str, int]] = []
    out = text
    for name, spec in patterns.items():
        if name.startswith("_") or not isinstance(spec, dict):
            continue
        regex = spec.get("regex")
        replace_with = spec.get("replace_with", "[MASKED]")
        if not regex:
            continue
        new, count = re.subn(regex, replace_with, out)
        if count:
            hits.append((name, replace_with, count))
            out = new
    return out, hits


SAMPLES = [
    # ユーザー指示の例
    "わたなべがVDで林社長と会議した",
    # 複合パターン
    "ペイクラウドHDの楠木さんから連絡。Lunchke の件で nabeyk123@gmail.com に返信したい",
    # ブラックリスト用語の検出（除外対象だが、変換は走らせない方針なのでマスクされないはず）
    "Sentry-VD の話題は学習データから除外する",
    # 保持対象（preserve_as_is）は変換されないこと
    "クロ と Claude は Vertex AI 経由で動かす",
    # 電話番号 PII パターン
    "連絡先: 090-1234-5678",
]


def main() -> int:
    print(f"[load] {DICT_PATH.relative_to(ROOT)}")
    d = load_dictionary(DICT_PATH)
    meta = d.get("_meta", {})
    rules = meta.get("matching_rules", {})
    case_insensitive = not rules.get("case_sensitive", False)
    pairs = build_term_pairs(d)
    pii_patterns = d.get("pii_patterns") or {}
    blacklist = (d.get("blacklist_topics") or {}).get("patterns") or []
    preserve = (d.get("preserve_as_is") or {}).get("terms") or []

    print(f"[dict] version={meta.get('version')}  term pairs={len(pairs)}  "
          f"pii patterns={len([k for k in pii_patterns if not k.startswith('_')])}  "
          f"blacklist={len(blacklist)}  preserve={len(preserve)}")
    print()

    ok = True
    for i, sample in enumerate(SAMPLES, 1):
        masked, term_hits = apply_terms(sample, pairs, case_insensitive)
        masked, pii_hits = apply_pii_patterns(masked, pii_patterns)
        # ブラックリスト用語の含有検査（マスクは走らせず、検知だけ）
        bl_hits = [b for b in blacklist if b.lower() in sample.lower()]
        print(f"--- sample {i} ---")
        print(f"  in : {sample}")
        print(f"  out: {masked}")
        if term_hits:
            print("  terms: " + ", ".join(f"{s}→{t}×{c}" for s, t, c in term_hits))
        if pii_hits:
            print("  pii  : " + ", ".join(f"{n}={r}×{c}" for n, r, c in pii_hits))
        if bl_hits:
            print(f"  blacklist hits (excluded in pipeline): {bl_hits}")
        # 保持対象が消えていないことの軽い検査
        for term in preserve:
            if term in sample and term not in masked:
                print(f"  !! WARNING: preserve term '{term}' was modified")
                ok = False
        print()

    # ユーザー指定の最重要ケース：「わたなべがVDで林社長と会議した」→「田中部長がAcme Corpで森田社長と会議した」
    expected = "田中部長がAcme Corpで森田社長と会議した"
    masked_expected, _ = apply_terms(SAMPLES[0], pairs, case_insensitive)
    if masked_expected == expected:
        print("[assert] 主要ケース OK: わたなべ/VD/林社長 → 田中部長/Acme Corp/森田社長")
    else:
        print(f"[assert] 主要ケース NG")
        print(f"  expected: {expected}")
        print(f"  got     : {masked_expected}")
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
