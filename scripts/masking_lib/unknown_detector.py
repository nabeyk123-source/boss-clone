"""未知固有名詞の候補抽出（v1.1.0 パターン特化方式）。

仕様: masking_pipeline_spec.md §4.7
v1.0.0 のヒューリスティック（カタカナ3〜10文字を一律検出）は技術対話で誤検出多発のため、
v1.1.0 で「人名・社名らしい構造」を検出する6パターンに切替（段階4テスト 2026-06-21）。
"""
from __future__ import annotations

import re

# 各パターン: (compiled_regex, type_label, term_group)
#   term_group: 何番目のキャプチャを「term」として記録するか
#     - 0 = マッチ全体
#     - 1〜 = 該当キャプチャグループ（助詞などを除いて主語部分だけ取りたいケース）
_PATTERNS: tuple[tuple[re.Pattern[str], str, int], ...] = (
    # 1. 日本語敬称
    (re.compile(r"([一-龥]{1,4}|[ァ-ヴー]{2,8})(さん|くん|ちゃん|氏)"), "honorific", 0),

    # 2. 役職付き
    (re.compile(r"([一-龥]{1,4}|[ァ-ヴー]{2,8})(社長|部長|課長|係長|主任|取締役|常務|専務|副社長|会長|CEO|CTO|CFO)"),
     "title", 0),

    # 3. 企業形態（前置）
    (re.compile(r"(株式会社|有限会社|合同会社)[一-龥ァ-ヴー\s]{1,15}"), "company_jp_prefix", 0),

    # 4. 企業形態（後置）
    (re.compile(r"[一-龥ァ-ヴー\s]{1,15}(株式会社|有限会社|合同会社)"), "company_jp_suffix", 0),

    # 5. 英文社名候補（大文字始まり、企業っぽい接尾辞付き）
    (re.compile(r"\b[A-Z][a-zA-Z]{3,15}(?:field|labs|tech|systems|solutions|pay|payments?|Inc\.?|Corp\.?|LLC|Ltd\.?)\b"),
     "company_en", 0),

    # 6. 主語っぽい人名候補（カタカナ + 助詞）— 助詞は除いて記録
    # 注: 仕様書の初版では末尾に \b があったが、ASCII 単語境界は日本語テキスト中で常に偽になり
    # 「ヤマダが連絡」のようなケースを取りこぼすため削除した（v1.1.1 微調整）。
    (re.compile(r"([ァ-ヴー]{3,8})(が|は|を|に|から|と)"), "name_with_particle", 1),
)


def detect_unknown_entities(text: str, known_terms: set[str]) -> list[dict]:
    """未知の固有名詞候補をパターン別に抽出。

    返り値の各要素: {term, type, context}
    `known_terms` に含まれる用語（preserve_as_is や辞書登録済み）は除外。
    同一テキスト内で同じ (term, type) が複数回検出されても1度しか返さない。
    """
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    for pat, kind, group in _PATTERNS:
        for m in pat.finditer(text):
            term = (m.group(group) or "").strip()
            if not term:
                continue
            if term in known_terms:
                continue
            key = (term, kind)
            if key in seen:
                continue
            seen.add(key)
            ctx_start = max(0, m.start() - 20)
            ctx_end = min(len(text), m.end() + 20)
            out.append({
                "term": term,
                "type": kind,
                "context": text[ctx_start:ctx_end],
            })
    return out
