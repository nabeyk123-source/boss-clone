"""ファイル添付機能の e2e スモーク。

LLM までは叩かず、各フォーマット → load_document → 各 Agent プロンプト整形まで通す。
（LLM 課金回避。本物の判定は Cloud Run デプロイ後に手動 e2e で確認する）
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

for s in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from boss_clone_lib.document_loader import load_document  # noqa: E402
from boss_clone_lib.prompts.system1 import (  # noqa: E402
    PROMPT as P_S1,
    format_attached_document as fad_s1,
    format_pairs,
)
from boss_clone_lib.prompts.system2 import (  # noqa: E402
    PROMPT as P_S2,
    format_attached_document as fad_s2,
    format_kb,
)
from boss_clone_lib.prompts.synthesizer import format_prompt as fp_syn  # noqa: E402


SAMPLE_TEXT = """# 新機能リリース計画

## 概要
kabe-anon v3 ダッシュボード機能のリリース。

## KPI
- DAU 15% 増
- 継続率 +5pt

## スケジュール
- 来月25日リリース予定

## リスク
- QA 期間が短い
"""


def _make_docx_bytes(text: str) -> bytes:
    from docx import Document
    doc = Document()
    for line in text.splitlines():
        if line.strip():
            doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _try_make_pdf_bytes(text: str) -> bytes | None:
    try:
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        try:
            pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
            c.setFont("HeiseiKakuGo-W5", 12)
        except Exception:
            c.setFont("Helvetica", 12)
        for i, line in enumerate(text.splitlines()):
            c.drawString(50, 800 - i * 18, line)
        c.showPage()
        c.save()
        return buf.getvalue()
    except Exception:
        return None


def main() -> int:
    passed = failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        mark = "ok  " if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {mark} {name}  {detail}")

    user_query = "この計画レビューしてほしい"

    formats: list[tuple[str, str, bytes | None]] = [
        ("md",   "plan.md",   SAMPLE_TEXT.encode("utf-8")),
        ("docx", "plan.docx", _make_docx_bytes(SAMPLE_TEXT)),
        ("pdf",  "plan.pdf",  _try_make_pdf_bytes(SAMPLE_TEXT)),
    ]

    for fmt, fname, data in formats:
        print(f"[{fmt}]")
        if data is None:
            print(f"  (skip {fmt}: 生成ツール無し)")
            continue
        doc = load_document(fname, data)
        check(f"{fmt} load status=ok", doc["status"] == "ok", str(doc.get("error")))
        check(f"{fmt} content non-empty", bool(doc["content"]))
        # 日本語 PDF は pypdf 抽出が崩れがちなので、軽い検査に留める
        check(
            f"{fmt} 主要トークンが入っている",
            ("KPI" in doc["content"] or "kabe" in doc["content"]),
            f'content head={doc["content"][:120]!r}',
        )

        # 各 Agent のプロンプトに乗ること
        p1 = P_S1.format(
            user_query=user_query,
            attached_document=fad_s1(doc),
            retrieved_pairs=format_pairs([]),
        )
        check(f"{fmt} → system1 prompt にファイル名", fname in p1)
        check(f"{fmt} → system1 prompt に内容", ("KPI" in p1 or "kabe" in p1))
        check(f"{fmt} → system1 添付資料ありの指示", "添付資料がある場合の進め方" in p1)

        p2 = P_S2.format(
            user_query=user_query,
            attached_document=fad_s2(doc),
            retrieved_l1=format_kb([]),
            retrieved_l2=format_kb([]),
            retrieved_l3=format_kb([]),
            retrieved_l4=format_kb([]),
        )
        check(f"{fmt} → system2 prompt にファイル名", fname in p2)
        check(f"{fmt} → system2 添付資料ありの指示", "添付資料がある場合の進め方" in p2)

        p_syn = fp_syn(
            user_query=user_query,
            system1_output={"questions": []},
            system2_output={},
            user_answers=[],
            attached_document=doc,
        )
        check(f"{fmt} → synthesizer prompt にファイル名", fname in p_syn)
        check(f"{fmt} → synthesizer 添付資料モードの指示", "資料に対するレビュー" in p_syn)

    # 添付無し時に「(なし)」がきちんと出ること（既存パス回帰）
    print("[no-attachment regression]")
    p_syn = fp_syn(
        user_query="普通の相談",
        system1_output={"questions": []},
        system2_output={},
        user_answers=[],
        attached_document=None,
    )
    check("attached_document=None でも format 通る", True)
    check("synthesizer に '(なし)' 行", "(なし)" in p_syn)

    print()
    print(f"=== {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
