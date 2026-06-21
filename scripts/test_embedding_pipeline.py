"""embedding_pipeline / embedding_lib の API モック単体テスト。

外部API（Vertex AI / Firestore）は呼ばない。スキーマ・パース・ヘルパーだけ検証。
"""
from __future__ import annotations

import sys
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from embedding_lib import topic_classifier as topic_mod  # noqa: E402
from embedding_lib import decision_classifier as dec_mod  # noqa: E402
from embedding_lib import vector_search_writer as vsw  # noqa: E402


class T:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name, got, expected):
        if got == expected:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name}")
            print(f"       expected: {expected!r}")
            print(f"       got:      {got!r}")

    def assert_true(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name} {detail}")

    def report(self):
        print()
        print(f"=== {self.passed} passed, {self.failed} failed ===")
        return 0 if self.failed == 0 else 1


def main() -> int:
    t = T()

    print("[topic_classifier: rule_based_tags]")
    # ランチケ → lunchke
    tags = topic_mod.rule_based_tags("ランチケのリリース判断について加藤部長に相談")
    t.assert_true("ランチケ → lunchke が含まれる", "lunchke" in tags)
    # Acme人物 → acme_people
    t.assert_true("加藤部長 → acme_people", "acme_people" in tags)
    # 一般技術用語のみ → ヒットなし
    tags2 = topic_mod.rule_based_tags("汎用な技術的議論で特に対象なし")
    t.check("非該当はゼロ", tags2, [])

    print("[topic_classifier: _parse_tags]")
    t.check("3個までに切る", topic_mod._parse_tags("foo, bar, baz, qux, quux"), ["foo", "bar", "baz"])
    t.check("空入力", topic_mod._parse_tags(""), [])
    t.check("日本語混在は英小文字化されない（除外）",
            topic_mod._parse_tags("日本語タグ"), [])
    t.check("記号は _ に置換", topic_mod._parse_tags("ab-cd, ef gh"), ["ab_cd", "ef_gh"])

    print("[decision_classifier: _normalize]")
    t.check("採用", dec_mod._normalize("採用"), "採用")
    t.check("却下を含む文", dec_mod._normalize("これは却下です"), "却下")
    t.check("空入力 → 保留 fallback", dec_mod._normalize(""), "保留")
    t.check("対象外語 → 保留 fallback", dec_mod._normalize("yes"), "保留")

    print("[vector_search_writer: build_pair_datapoint]")
    dp = vsw.build_pair_datapoint(
        doc_id="pid_abc_0001",
        embedding=[0.1] * 768,
        tag="OK",
        topic_tags=["kabe", "design"],
        decision_type="採用",
        created_at_iso="2026-05-21T10:00:00Z",
    )
    t.check("id がそのまま", dp["id"], "pid_abc_0001")
    t.check("次元 768", len(dp["embedding"]), 768)
    namespaces = {r["namespace"] for r in dp["restricts"]}
    t.assert_true("namespace に tag", "tag" in namespaces)
    t.assert_true("namespace に topic_tags", "topic_tags" in namespaces)
    t.assert_true("namespace に decision_type", "decision_type" in namespaces)
    t.assert_true("namespace に created_at_year_month", "created_at_year_month" in namespaces)

    # 空フィールドは restricts に含まれない
    dp2 = vsw.build_pair_datapoint(
        doc_id="x", embedding=[0.0] * 768, tag="", topic_tags=[], decision_type="", created_at_iso=None,
    )
    t.check("空フィールドは restricts に含まれない", dp2["restricts"], [])

    print("[vector_search_writer: build_acme_datapoint]")
    adp = vsw.build_acme_datapoint(
        doc_id="L1_value_customer_first",
        embedding=[0.5] * 768,
        layer="L1_principles",
        category="values",
        applies_to=["all"],
    )
    t.assert_true("layer/category/applies_to の3 namespace",
                  {r["namespace"] for r in adp["restricts"]} == {"layer", "category", "applies_to"})

    print("[embedding_pipeline helpers: imported & callable]")
    # main の import が壊れていないことだけ確認
    import embedding_pipeline  # noqa: F401
    t.assert_true("embedding_pipeline import OK", True)
    t.check("short_uuid", embedding_pipeline.short_uuid("a2e5ab89-93e2-..."), "a2e5ab89")
    t.check("make_doc_id 形式",
            embedding_pipeline.make_doc_id("pid_xy", "a2e5ab89-93e2", 5),
            "pid_xy_a2e5ab89_0005")

    return t.report()


if __name__ == "__main__":
    raise SystemExit(main())
