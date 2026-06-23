"""document_loader の動作確認。txt / md / pdf / docx を生成して全形式テスト。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from boss_clone_lib.document_loader import load_document, format_for_prompt  # noqa: E402


def _make_pdf(text: str) -> bytes | None:
    """テスト用に reportlab で簡易 PDF を作る。reportlab 不在なら None。"""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        try:
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
            c.setFont('HeiseiKakuGo-W5', 12)
        except Exception:
            c.setFont('Helvetica', 12)
        for i, line in enumerate(text.splitlines()):
            c.drawString(50, 800 - i * 20, line)
        c.showPage()
        c.save()
        return buf.getvalue()
    except Exception:
        return None


def _make_docx(text: str) -> bytes:
    from docx import Document
    doc = Document()
    for line in text.splitlines():
        if line.strip():
            doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def main() -> int:
    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  ok   {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    sample_text = "これはテスト用の資料です。\n\n# 見出し\n\n本文 ABC 123。"

    print("[txt]")
    r = load_document("sample.txt", sample_text.encode("utf-8"))
    check("status ok", r["status"] == "ok", str(r))
    check("format=txt", r["format"] == "txt")
    check("content 一致", r["content"] == sample_text)
    check("char_count > 0", r["char_count"] > 0)

    print("[md]")
    r = load_document("sample.md", sample_text.encode("utf-8"))
    check("format=md", r["format"] == "md")
    check("content 一致", r["content"] == sample_text)

    print("[cp932 fallback]")
    r = load_document("sjis.txt", sample_text.encode("cp932"))
    check("status ok (cp932 decode)", r["status"] == "ok")
    check("content 一致", r["content"] == sample_text)

    print("[docx]")
    docx_data = _make_docx(sample_text)
    r = load_document("sample.docx", docx_data)
    check("status ok", r["status"] == "ok")
    check("format=docx", r["format"] == "docx")
    check("テキスト含む", "本文 ABC 123" in r["content"])

    print("[pdf]")
    pdf_data = _make_pdf(sample_text)
    if pdf_data:
        r = load_document("sample.pdf", pdf_data)
        check("status ok", r["status"] == "ok")
        check("format=pdf", r["format"] == "pdf")
        check("テキスト抽出 (英数字)", "ABC" in r["content"] or "123" in r["content"],
              f'got: {r["content"][:200]!r}')
    else:
        print("  (skip pdf, reportlab が無いため自動生成不可)")

    print("[未対応形式]")
    r = load_document("sample.xlsx", b"dummy")
    check("status error", r["status"] == "error")
    check("error 文言に 'xlsx'", "xlsx" in (r["error"] or ""))

    print("[切り詰め]")
    long_text = "あ" * 60_000
    r = load_document("long.txt", long_text.encode("utf-8"))
    check("truncated=True", r["truncated"] is True)
    check("char_count == 50000", r["char_count"] == 50000)
    check("original_char_count == 60000", r["original_char_count"] == 60000)
    check("warning メッセージ", any("切り詰め" in w for w in r["warnings"]))

    print("[format_for_prompt]")
    sample_doc = load_document("x.txt", sample_text.encode("utf-8"))
    formatted = format_for_prompt(sample_doc)
    check("ファイル名を含む", "x.txt" in formatted)
    check("本文を含む", "本文 ABC 123" in formatted)

    print()
    print(f"=== {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
