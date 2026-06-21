"""Acme Corp 設計書を 5層×N項目に分解して acme_kb コレクション/インデックス用に整形。

仕様: schema_spec.md §3.3, §5.2
- acme_corp_spec.md を機械パースせず、ここで manual mapping する（自動パースは脆い）
- 各エントリ: {layer, category, key, title, content, embedding_text, applies_to, version}
- 件数目安: L1=8 / L2=8 / L3=10 / L4=12 / L5=8 → 約46件

使い方:
    # ローカル JSONL 出力（埋め込み未生成、Firestore も触らない）
    python scripts/acme_kb_loader.py --dry-run

    # 埋め込み生成 + Firestore 投入 + Vector Search JSONL
    python scripts/acme_kb_loader.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from embedding_lib import embedder as emb_mod  # noqa: E402
from embedding_lib import vector_search_writer as vsw  # noqa: E402

OUT_DIR = ROOT / "data" / "processed" / "embedding"
PATH_DOCS = OUT_DIR / "acme_kb_docs.jsonl"
PATH_VS_JSONL = OUT_DIR / "acme_kb.jsonl"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("acme_kb_loader")

ACME_SPEC_VERSION = "1.1.0"

# ============ 構造化定義 ============

L1_PRINCIPLES: list[dict] = [
    {"category": "mission", "key": "mission",
     "title": "ミッション",
     "content": "「決済で、信頼の輪を広げる」。Acme Pay Solutions の存在意義。"},
    {"category": "vision", "key": "vision",
     "title": "ビジョン",
     "content": "「2030 年、日本のキャッシュレス取引の 20% を支えるインフラに」。長期目標。"},
    {"category": "values", "key": "customer_first",
     "title": "顧客起点（Customer First）",
     "content": "すべての判断は顧客体験を起点にする。提案が通る最重要要件。"},
    {"category": "values", "key": "honest_dialogue",
     "title": "誠実な対話（Honest Dialogue）",
     "content": "耳の痛い真実こそ最初に伝える。提案者は不利な情報も最初に出す前提。"},
    {"category": "values", "key": "kaizen_forever",
     "title": "継続改善（Kaizen Forever）",
     "content": "完璧より、明日の改善。完全主義の長期化は嫌われる。"},
    {"category": "values", "key": "ownership",
     "title": "当事者意識（Ownership）",
     "content": "組織の課題は自分の課題。他部門の問題に「自分ごと」で関わる姿勢を評価する。"},
    {"category": "policy", "key": "fy2026_targets",
     "title": "2026 年度経営方針",
     "content": "ARR 50 億円達成、ハウス型決済の累計導入 1,000 社突破、データ活用ソリューションを新規収益柱に育成、採用強化（特に PdM・エンジニア）。"},
    {"category": "policy", "key": "business_areas",
     "title": "事業領域",
     "content": "ハウス型決済プラットフォーム（小売・飲食チェーン）、ブランド Pay 構築支援（メーカー）、決済データ活用ソリューション（CRM・マーケ）。"},
]

L2_REGULATIONS: list[dict] = [
    {"category": "work_rule", "key": "art15_infosec_violation",
     "title": "就業規則 第15条 情報セキュリティ違反",
     "content": "情報セキュリティ違反は懲戒対象。軽微は戒告、重大は解雇。AI/SaaS 利用時の情報取扱いも対象。"},
    {"category": "work_rule", "key": "art32_side_business",
     "title": "就業規則 第32条 副業",
     "content": "副業は事前申請・承認制。競業避止義務あり。"},
    {"category": "work_rule", "key": "art48_working_hours",
     "title": "就業規則 第48条 労働時間",
     "content": "原則 8 時間/日、36 協定上限 月60時間。"},
    {"category": "infosec", "key": "no_external_secret",
     "title": "情報セキュリティ規程：機密の社外持ち出し禁止",
     "content": "機密情報の社外持ち出し禁止。AI/外部 SaaS への個人情報投入は原則禁止。"},
    {"category": "infosec", "key": "security_review_required",
     "title": "情報セキュリティ規程：セキュリティレビュー必須対象",
     "content": "新規サービスリリース、顧客データ取扱い変更時はセキュリティレビュー必須。"},
    {"category": "approval", "key": "expense_authority",
     "title": "経費・稟議規程：承認権限表",
     "content": "5万円以下 課長、50万円以下 部長、500万円以下 本部長、500万円超 取締役会。"},
    {"category": "compliance", "key": "antisocial_check",
     "title": "コンプライアンス：反社チェック",
     "content": "取引開始前の反社チェック必須。"},
    {"category": "compliance", "key": "privacy_invoice",
     "title": "コンプライアンス：個人情報保護法・特商法・インボイス",
     "content": "個人情報保護法、改正特定商取引法、インボイス制度の遵守。"},
]

L3_STRATEGY: list[dict] = [
    {"category": "kgi", "key": "fy2026_kgi",
     "title": "営業企画部 2026年度 KGI",
     "content": "新規プロダクト ARR 8 億円達成。"},
    {"category": "kpi", "key": "house_payment_120",
     "title": "KPI：ハウス型決済 新規導入 120社"},
    {"category": "kpi", "key": "data_solution_poc_30",
     "title": "KPI：データ活用ソリューション PoC 30社"},
    {"category": "kpi", "key": "existing_expansion_25",
     "title": "KPI：既存顧客の機能利用拡大率 +25%"},
    {"category": "kpi", "key": "pdm_internal_70",
     "title": "KPI：PdM・PMM 内製化率 70%"},
    {"category": "roadmap", "key": "q1_dashboard_v2",
     "title": "Q1：データ可視化ダッシュボード v2"},
    {"category": "roadmap", "key": "q2_brandpay_template",
     "title": "Q2：ブランド Pay 構築の簡易テンプレート化"},
    {"category": "roadmap", "key": "q3_ai_crm",
     "title": "Q3：AI による加盟店 CRM 自動化機能"},
    {"category": "roadmap", "key": "q4_overseas_bridge",
     "title": "Q4：海外決済ブリッジ機能"},
    {"category": "competition", "key": "competitive_landscape",
     "title": "競合認識",
     "content": "大手: Square, Stripe, PayPay。中堅: Showcase Gig, Pocketpay。自社ポジション: 中堅小売・飲食チェーン特化、API 柔軟性で差別化。"},
]

L4_IMPLICIT: list[dict] = [
    {"category": "decision_pattern", "key": "morita_ceo",
     "title": "森田社長の判断パターン",
     "content": "顧客体験のストーリーが弱い提案は通らない。データより「現場で何が起きるか」を重視。競合の悪口を嫌う、「我々が何をするか」を語れと言う。"},
    {"category": "decision_pattern", "key": "takase_vp",
     "title": "高瀬副社長の判断パターン",
     "content": "ROI 試算が甘いと即差し戻し。「3 年後の利益責任を誰が取るのか」と必ず聞く。数字なしで「いい感じ」と言うのを最も嫌う。"},
    {"category": "decision_pattern", "key": "tominaga_advisor",
     "title": "富永顧問の判断パターン",
     "content": "JV / M&A 的アプローチを好む。社内リソース論を持ち出すと「外部を使え」と返してくる。「他社事例」だけの提案には「Acme Corp として何を成すか」を問う。"},
    {"category": "meeting_rule", "key": "agenda_prepost_17",
     "title": "役員会議の暗黙ルール：議題は前日17時までに事前共有"},
    {"category": "meeting_rule", "key": "objection_as_topic",
     "title": "役員会議：反対意見は『論点』として整理（感情的にならない）"},
    {"category": "meeting_rule", "key": "15min_per_topic",
     "title": "役員会議：1議題15分、超える場合は事前予告"},
    {"category": "meeting_rule", "key": "prep_method",
     "title": "役員会議：結論を先に言う（PREP法）"},
    {"category": "department_gap", "key": "sales_vs_planning",
     "title": "営業部 vs 営業企画部のズレ",
     "content": "「数を取りたい」↔「質を上げたい」"},
    {"category": "department_gap", "key": "dev_vs_planning",
     "title": "開発部 vs 営業企画部のズレ",
     "content": "「正しく作りたい」↔「早く出したい」"},
    {"category": "department_gap", "key": "cs_vs_planning",
     "title": "CS部 vs 営業企画部のズレ",
     "content": "「顧客の声を活かせ」↔「全部反映は無理」"},
    {"category": "taboo", "key": "no_vague_numbers",
     "title": "やっちゃダメ：数字なしで『いい感じ』",
     "content": "高瀬副社長激怒の典型パターン。"},
    {"category": "taboo", "key": "no_competitor_bashing",
     "title": "やっちゃダメ：競合の悪口 / 他社事例のみの提案",
     "content": "森田社長は嫌う。「我々が何をするか」「Acme Corp として何を成すか」を語れ。"},
]

L5_PERSONAL: list[dict] = [
    {"category": "style", "key": "structured_thinking",
     "title": "加藤部長の判断スタイル 1：構造化思考",
     "content": "問題を分解してから判断する。"},
    {"category": "style", "key": "tradeoff_explicit",
     "title": "加藤部長の判断スタイル 2：トレードオフ明示",
     "content": "「これを取ると、これを諦める」を必ず言語化する。"},
    {"category": "style", "key": "issue_first",
     "title": "加藤部長の判断スタイル 3：論点先出し",
     "content": "結論より、論点の整理を優先する。"},
    {"category": "style", "key": "question_back",
     "title": "加藤部長の判断スタイル 4：問い返し型",
     "content": "すぐ答えず、相談者の思考を深める質問を返す。"},
    {"category": "style", "key": "must_want_split",
     "title": "加藤部長の判断スタイル 5：MUST/WANT 分離",
     "content": "絶対必要なものと、あったらいいものを分ける。"},
    {"category": "history", "key": "2025_12_poc_expand",
     "title": "過去判断 2025-12：データ活用 PoC を 3社→5社に拡大承認"},
    {"category": "history", "key": "2026_01_dev_jointmtg",
     "title": "過去判断 2026-01：開発部との合同会議体制を提案・実装"},
    {"category": "history", "key": "2026_03_side_business",
     "title": "過去判断 2026-03：副業申請（業務時間外 SaaS 開発）を自ら申請・承認取得"},
]

LAYER_MAP: dict[str, list[dict]] = {
    "L1_principles": L1_PRINCIPLES,
    "L2_regulations": L2_REGULATIONS,
    "L3_strategy": L3_STRATEGY,
    "L4_implicit_knowledge": L4_IMPLICIT,
    "L5_personal_judgment": L5_PERSONAL,
}


def build_entries() -> list[dict]:
    """全エントリを doc_id 付きで返す。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    entries: list[dict] = []
    for layer, items in LAYER_MAP.items():
        for it in items:
            doc_id = f"{layer}_{it['category']}_{it['key']}"
            content = it.get("content") or it["title"]
            embedding_text = f"{it['title']}\n{content}"
            entries.append({
                "doc_id": doc_id,
                "layer": layer,
                "category": it["category"],
                "key": it["key"],
                "title": it["title"],
                "content": content,
                "embedding_text": embedding_text,
                "references": [],
                "applies_to": ["all"],
                "version": ACME_SPEC_VERSION,
                "last_updated": now_iso,
                "active": True,
            })
    return entries


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Corp 設計書を acme_kb として投入")
    p.add_argument("--dry-run", action="store_true", help="Firestore に書かず JSONL のみ出力")
    p.add_argument("--max-concurrency", type=int, default=4)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    entries = build_entries()
    log.info("[build] %d entries (L1=%d L2=%d L3=%d L4=%d L5=%d)",
             len(entries), len(L1_PRINCIPLES), len(L2_REGULATIONS), len(L3_STRATEGY), len(L4_IMPLICIT), len(L5_PERSONAL))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 埋め込み生成 ----
    embed_texts = [e["embedding_text"] for e in entries]
    cfg = emb_mod.EmbedConfig(max_concurrency=args.max_concurrency, batch_size=50)
    log.info("[embed] start (%d items)", len(embed_texts))
    t = time.perf_counter()
    vectors, batch_meta = asyncio.run(emb_mod.embed_many(embed_texts, cfg=cfg))
    log.info("[embed] done %.1fs, batches=%d", time.perf_counter() - t, len(batch_meta))

    n_embedded = sum(1 for v in vectors if v is not None)
    log.info("embedded %d / %d", n_embedded, len(entries))

    # ---- Firestore docs JSONL（ローカル） ----
    with PATH_DOCS.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    log.info("[write] %s", PATH_DOCS.relative_to(ROOT))

    # ---- Vector Search JSONL ----
    vs_datapoints = []
    for e, v in zip(entries, vectors):
        if v is None:
            continue
        vs_datapoints.append(vsw.build_acme_datapoint(
            doc_id=e["doc_id"],
            embedding=v,
            layer=e["layer"],
            category=e["category"],
            applies_to=e["applies_to"],
        ))
    n_vs = vsw.write_index_jsonl(PATH_VS_JSONL, vs_datapoints)
    log.info("[write] %s (%d datapoints)", PATH_VS_JSONL.relative_to(ROOT), n_vs)

    # ---- Firestore 投入 ----
    if args.dry_run:
        log.info("[dry-run] Firestore 投入はスキップ")
    else:
        from embedding_lib import firestore_writer as fw
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        log.info("[firestore] project=%s collection=acme_kb", project)
        client = fw.get_client(project)
        docs_with_id = [(e["doc_id"], {k: v for k, v in e.items() if k != "doc_id"}) for e in entries]
        n = fw.batch_write(client, "acme_kb", docs_with_id)
        log.info("[firestore] %d docs written", n)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
