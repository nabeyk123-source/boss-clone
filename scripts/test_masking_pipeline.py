"""masking_lib の単体テスト。

仕様: masking_pipeline_spec.md §9
- 辞書適用の長語句優先
- preserve_as_is の保護
- ブラックリスト検出（対話単位、文字列部分一致）
- PII検出
- べき等性（ハッシュ）
- 主要ブランチ抽出
- ペア抽出
- 未知固有名詞検出
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from masking_lib import dictionary as dict_mod  # noqa: E402
from masking_lib import pii as pii_mod  # noqa: E402
from masking_lib import pair_extractor as pe  # noqa: E402
from masking_lib import unknown_detector as ud  # noqa: E402

DICT_PATH = ROOT / "docs" / "masking_dictionary.json"


class T:
    """超簡易テストランナー（pytest 入れる手間を省略）。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, got, expected) -> None:
        if got == expected:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name}")
            print(f"       expected: {expected!r}")
            print(f"       got:      {got!r}")

    def assert_true(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name} {detail}")

    def report(self) -> int:
        print()
        print(f"=== {self.passed} passed, {self.failed} failed ===")
        return 0 if self.failed == 0 else 1


def main() -> int:
    t = T()
    d = dict_mod.load_dictionary(DICT_PATH)

    # --- dictionary.apply_dictionary ---
    print("[dictionary]")
    out, repls = dict_mod.apply_dictionary("わたなべがVDで林社長と会議した", d)
    t.check("主要ケース（v1.1.0: 加藤部長）", out, "加藤部長がAcme Corpで森田社長と会議した")
    t.assert_true("置換明細あり", len(repls) >= 3)

    # preserve_as_is の保護: 「クロ」「Claude」が辞書由来で誤変換されない
    out2, _ = dict_mod.apply_dictionary("クロ と Claude は Vertex AI で動く", d)
    t.check("preserve 保護", out2, "クロ と Claude は Vertex AI で動く")

    # 長語句優先: 「nabeyk123-source」が「nabeyk123」より先に置換される（v1.1.0: kato-t）
    out3, _ = dict_mod.apply_dictionary("リポは github.com/nabeyk123-source/boss-clone", d)
    t.assert_true(
        "長語句優先（nabeyk123-source → kato-t）",
        "kato-t" in out3 and "kato_t-source" not in out3,
        f"got: {out3}",
    )

    # 4015 → 9999（証券コード）
    out4, _ = dict_mod.apply_dictionary("証券コード 4015", d)
    t.check("証券コード", out4, "証券コード 9999")

    # 連続適用でべき等（masked → masked' でこれ以上変化しない）
    once, _ = dict_mod.apply_dictionary("わたなべがVDで会議", d)
    twice, _ = dict_mod.apply_dictionary(once, d)
    t.check("辞書適用のべき等性（出力は再適用で変わらない）", twice, once)

    # v1.1.0 主人公衝突回避: 「わたなべ」「田中さん」混在時の処理順序
    case = "わたなべは田中さんと会議。田中もOKと言った"
    got, _ = dict_mod.apply_dictionary(case, d)
    t.check(
        "v1.1.0 わたなべ/田中さん/田中 の混在処理",
        got,
        "加藤部長は斎藤さんと会議。斎藤もOKと言った",
    )

    # 実在人物7名の追加置換（v1.1.1 で鵜月→野村に変更）
    got, _ = dict_mod.apply_dictionary("渡邉さんと吉田さん、鵜月さんに連絡", d)
    t.check(
        "v1.1.1 実在7名のうち3名（渡邉/吉田/鵜月）",
        got,
        "佐々木さんと鈴木さん、野村さんに連絡",
    )

    got, _ = dict_mod.apply_dictionary("小池さん・山崎さん・久保さん・本田さんも", d)
    t.check(
        "v1.1.0 実在7名のうち4名（小池/山崎/久保/本田）",
        got,
        "石井さん・後藤さん・岡田さん・森さんも",
    )

    # v1.1.1 鵜月→野村（実在「中島さん」との衝突回避）
    got, _ = dict_mod.apply_dictionary("鵜月さんと打ち合わせ。鵜月から後日連絡。", d)
    t.check(
        "v1.1.1 鵜月→野村（中島さんと衝突しない）",
        got,
        "野村さんと打ち合わせ。野村から後日連絡。",
    )
    # 実在「中島さん」も同文に出るケースで、両方が独立して動くこと
    got, _ = dict_mod.apply_dictionary("鵜月さんと中島さんは別人", d)
    t.assert_true(
        "v1.1.1 鵜月さん/中島さん 共存（衝突なし）",
        "野村さん" in got and "中島さん" in got and "鵜月" not in got,
        f"got: {got}",
    )

    # 二重変換が起きないことの直接確認: 新規 target が source として再マッチしない
    new_targets = {"加藤", "加藤部長", "斎藤", "斎藤さん", "佐々木", "佐々木さん",
                   "鈴木", "鈴木さん", "中島", "中島さん", "石井", "石井さん",
                   "後藤", "後藤さん", "岡田", "岡田さん", "森", "森さん"}
    all_sources = set()
    for cat in dict_mod.CATEGORIES_TO_APPLY:
        all_sources.update((d.get(cat) or {}).keys())
    overlap = new_targets & all_sources
    t.assert_true(
        f"v1.1.0 新規target が source と衝突しない（overlap={overlap or '∅'}）",
        not overlap,
    )

    # --- pii ---
    print("[pii]")
    compiled = pii_mod.compile_pii_patterns(d.get("pii_patterns") or {})
    t.assert_true("PIIパターンが3つコンパイルされる", len(compiled) == 3)
    masked, hits = pii_mod.mask_pii("foo@example.com / 090-1234-5678 / 4242 4242 4242 4242", compiled)
    t.assert_true("email/phone/card 全て [...]", "[EMAIL]" in masked and "[PHONE]" in masked and "[CARD]" in masked)
    t.check("PII ヒット3種類", sorted(h["type"] for h in hits), ["credit_card", "email", "phone_jp"])

    # --- pair_extractor.is_blacklisted ---
    print("[blacklist]")
    bl_patterns = (d.get("blacklist_topics") or {}).get("patterns") or []
    hit_conv = {
        "chat_messages": [
            {"sender": "human", "text": "Sentry-VD の運用について"},
            {"sender": "assistant", "text": "了解です"},
        ]
    }
    safe_conv = {
        "chat_messages": [
            {"sender": "human", "text": "ランチケのリリース判断"},
            {"sender": "assistant", "text": "了解です"},
        ]
    }
    hit, term = pe.is_blacklisted(hit_conv, bl_patterns)
    t.assert_true("ブラックリスト検出", hit and term.lower() == "sentry-vd")
    hit2, _ = pe.is_blacklisted(safe_conv, bl_patterns)
    t.assert_true("ブラックリスト誤検知なし", not hit2)

    # --- pair_extractor.extract_main_branch / extract_pairs ---
    print("[pair extractor]")
    msgs = [
        {"uuid": "a", "sender": "human", "text": "問", "parent_message_uuid": None, "created_at": "2026-04-10T00:00:00Z"},
        {"uuid": "b", "sender": "assistant", "text": "答1", "parent_message_uuid": "a", "created_at": "2026-04-10T00:00:01Z"},
        {"uuid": "c", "sender": "assistant", "text": "答2（別案）", "parent_message_uuid": "a", "created_at": "2026-04-10T00:00:02Z"},
        {"uuid": "d", "sender": "human", "text": "ありがとう", "parent_message_uuid": "c", "created_at": "2026-04-10T00:00:03Z"},
    ]
    branch = pe.extract_main_branch(msgs)
    branch_uuids = [m["uuid"] for m in branch]
    # 最新リーフは b (00:01) と d (00:03)、最新は d、辿ると d→c→a
    t.check("メインブランチ抽出（d→c→a）", branch_uuids, ["a", "c", "d"])

    pairs = pe.extract_pairs(branch)
    t.assert_true("ペア抽出1組", len(pairs) == 1 and pairs[0]["human_uuid"] == "a")

    # --- unknown_detector (v1.1.0 パターン特化方式) ---
    print("[unknown detector]")
    known = dict_mod.collect_known_terms(d)

    # 1. 敬称（辞書 v1.1.0 で「鈴木さん」「斎藤さん」は known なので、辞書に無い「橋本さん」で確認）
    cands = ud.detect_unknown_entities("橋本さんと前田くんに連絡。山口氏も同席", known)
    t.assert_true("honorific 検出（橋本さん）",
                  any(c["type"] == "honorific" and "橋本さん" in c["term"] for c in cands))

    # 2. 役職付き
    cands = ud.detect_unknown_entities("山田部長と佐藤取締役の判断", known)
    titles = [c["term"] for c in cands if c["type"] == "title"]
    t.assert_true("title 検出（山田部長/佐藤取締役）",
                  any("部長" in t for t in titles) and any("取締役" in t for t in titles))

    # 3. 企業形態（前置）
    cands = ud.detect_unknown_entities("株式会社サンプル商事と契約", known)
    t.assert_true("company_jp_prefix 検出",
                  any(c["type"] == "company_jp_prefix" for c in cands))

    # 4. 企業形態（後置）
    cands = ud.detect_unknown_entities("ヤマトホールディングス株式会社と提携", known)
    t.assert_true("company_jp_suffix 検出",
                  any(c["type"] == "company_jp_suffix" for c in cands))

    # 5. 英文社名（接尾辞付き）
    cands = ud.detect_unknown_entities("Datalabs と Smartpay の比較", known)
    en_terms = [c["term"] for c in cands if c["type"] == "company_en"]
    t.assert_true("company_en 検出（Datalabs/Smartpay）",
                  "Datalabs" in en_terms and "Smartpay" in en_terms)

    # 6. 主語+助詞（カタカナ）
    cands = ud.detect_unknown_entities("ヤマダが連絡してきた、ナベヤマからも", known)
    np_terms = [c["term"] for c in cands if c["type"] == "name_with_particle"]
    t.assert_true("name_with_particle 検出（ヤマダ/ナベヤマ）",
                  "ヤマダ" in np_terms and "ナベヤマ" in np_terms)

    # 既知 preserve は除外
    cands_pres = ud.detect_unknown_entities("Anthropic と Claude を使う", known)
    t.assert_true("preserve は未知扱いしない",
                  not any(c["term"] in {"Anthropic", "Claude"} for c in cands_pres))

    # v1.0.0 で問題になった「カタカナ単独検出」が消えたことの確認
    # （type='katakana' は v1.1.0 で削除。'name_with_particle' で助詞付きで拾われるのは設計上許容）
    common_text = "ブラウザを起動した"
    cands_common = ud.detect_unknown_entities(common_text, known)
    t.assert_true(
        "v1.0.0 の type='katakana' は廃止（単独カタカナはもう拾わない）",
        not any(c["type"] == "katakana" for c in cands_common),
    )

    # --- dictionary.dict_hash / file_hash（べき等性） ---
    print("[idempotency hashes]")
    h1 = dict_mod.dict_hash(d)
    h2 = dict_mod.dict_hash(d)
    t.assert_true("辞書ハッシュが安定", h1 == h2 and len(h1) == 16)
    fh1 = dict_mod.file_hash(DICT_PATH)
    fh2 = dict_mod.file_hash(DICT_PATH)
    t.assert_true("ファイルハッシュが安定", fh1 == fh2 and len(fh1) == 16)

    return t.report()


if __name__ == "__main__":
    raise SystemExit(main())
