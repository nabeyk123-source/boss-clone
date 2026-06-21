"""Claudeエクスポートデータの構造分析。

`data/raw/claude_export/` 配下の以下を解析してマークダウンレポートを出す:
- conversations.json   通常チャット本体（大）
- design_chats/*.json  Design Chat（β）
- projects/*.json      Project機能のメタ + docs
- memories.json        プロジェクト記憶
- users.json           アカウント情報

ストリーミング読み込み（ijson）は採用していない。理由は冒頭 docstring 参照。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / "data" / "raw" / "claude_export"
OUT = ROOT / "data" / "interim" / "analysis_report.md"

# 1文字あたりトークン数の概算係数（日本語混在テキストの素朴な目安）
CHARS_PER_TOKEN = 1.5


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fmt_int(n: int) -> str:
    return f"{n:,}"


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # 末尾 Z を +00:00 に正規化
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def collect_text(msg: dict) -> str:
    """1メッセージの本文を結合して返す。text フィールド優先、無ければ content[].text を連結。"""
    if msg.get("text"):
        return msg["text"]
    parts: list[str] = []
    for part in msg.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            parts.append(part["text"])
    return "\n".join(parts)


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs_sorted = sorted(xs)
    k = (len(xs_sorted) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs_sorted) - 1)
    return xs_sorted[f] + (xs_sorted[c] - xs_sorted[f]) * (k - f)


def analyze_conversations(convs: list[dict]) -> dict:
    """conversations.json の集計。"""
    total_msgs = 0
    sender_counts: Counter[str] = Counter()
    sender_chars: Counter[str] = Counter()
    created_ats: list[datetime] = []
    msg_lengths_per_conv: list[int] = []
    empty_text_msgs = 0
    null_sender = 0
    branching_convs = 0     # 同一 parent から複数返信がぶら下がる対話の数
    has_attachments = 0
    has_files = 0
    unique_uuids: set[str] = set()
    duplicate_uuids = 0
    name_blank = 0

    for conv in convs:
        uuid = conv.get("uuid")
        if uuid in unique_uuids:
            duplicate_uuids += 1
        else:
            unique_uuids.add(uuid)
        if not (conv.get("name") or "").strip():
            name_blank += 1

        if dt := parse_dt(conv.get("created_at")):
            created_ats.append(dt)

        msgs = conv.get("chat_messages") or []
        msg_lengths_per_conv.append(len(msgs))
        total_msgs += len(msgs)

        parent_counts: Counter[str] = Counter()
        for m in msgs:
            sender = m.get("sender")
            if sender is None:
                null_sender += 1
                sender = "unknown"
            sender_counts[sender] += 1
            text = collect_text(m)
            if not text.strip():
                empty_text_msgs += 1
            sender_chars[sender] += len(text)
            if m.get("attachments"):
                has_attachments += 1
            if m.get("files"):
                has_files += 1
            if pid := m.get("parent_message_uuid"):
                parent_counts[pid] += 1
        if any(c >= 2 for c in parent_counts.values()):
            branching_convs += 1

    titles = [c.get("name") or "(no title)" for c in convs]

    return {
        "total_convs": len(convs),
        "total_msgs": total_msgs,
        "sender_counts": dict(sender_counts),
        "sender_chars": dict(sender_chars),
        "sender_tokens_est": {k: int(v / CHARS_PER_TOKEN) for k, v in sender_chars.items()},
        "earliest": min(created_ats) if created_ats else None,
        "latest": max(created_ats) if created_ats else None,
        "msgs_per_conv_avg": statistics.mean(msg_lengths_per_conv) if msg_lengths_per_conv else 0,
        "msgs_per_conv_median": statistics.median(msg_lengths_per_conv) if msg_lengths_per_conv else 0,
        "msgs_per_conv_p95": percentile([float(x) for x in msg_lengths_per_conv], 0.95),
        "msgs_per_conv_max": max(msg_lengths_per_conv) if msg_lengths_per_conv else 0,
        "empty_text_msgs": empty_text_msgs,
        "null_sender": null_sender,
        "branching_convs": branching_convs,
        "has_attachments_msgs": has_attachments,
        "has_files_msgs": has_files,
        "duplicate_uuids": duplicate_uuids,
        "name_blank": name_blank,
        "titles_sample": titles[:20],
    }


def analyze_design_chats(dir_: Path) -> dict:
    files = sorted(dir_.glob("*.json"))
    total_msgs = 0
    project_links: Counter[str] = Counter()
    titles: list[str] = []
    for p in files:
        data = load_json(p)
        if isinstance(data, dict):
            titles.append(data.get("title") or "(no title)")
            msgs = data.get("messages") or []
            total_msgs += len(msgs)
            proj = data.get("project")
            if isinstance(proj, dict):
                project_links[proj.get("uuid") or "?"] += 1
            elif isinstance(proj, str):
                project_links[proj] += 1
    return {
        "files": len(files),
        "total_msgs": total_msgs,
        "project_links": dict(project_links),
        "titles": titles,
    }


def analyze_projects(dir_: Path) -> dict:
    files = sorted(dir_.glob("*.json"))
    entries = []
    total_docs = 0
    starter_count = 0
    private_count = 0
    for p in files:
        d = load_json(p)
        docs = d.get("docs") or []
        total_docs += len(docs)
        if d.get("is_starter_project"):
            starter_count += 1
        if d.get("is_private"):
            private_count += 1
        entries.append({
            "uuid": d.get("uuid"),
            "name": d.get("name"),
            "docs": len(docs),
            "starter": bool(d.get("is_starter_project")),
            "private": bool(d.get("is_private")),
        })
    return {
        "files": len(files),
        "total_docs": total_docs,
        "starter_count": starter_count,
        "private_count": private_count,
        "entries": entries,
    }


def analyze_memories(path: Path) -> dict:
    data = load_json(path)
    if isinstance(data, list):
        total_chars = sum(len(m.get("conversations_memory") or "") for m in data if isinstance(m, dict))
        return {"entries": len(data), "total_chars": total_chars}
    return {"entries": 0, "total_chars": 0}


def analyze_users(path: Path) -> dict:
    data = load_json(path)
    return {"entries": len(data) if isinstance(data, list) else 0}


# ---------- レポート生成 ----------

def render_report(
    convs_size_mb: float,
    convs_stats: dict,
    design_stats: dict,
    project_stats: dict,
    mem_stats: dict,
    user_stats: dict,
) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Claude エクスポート 構造分析レポート")
    a("")
    a(f"生成日: {datetime.now().strftime('%Y-%m-%d %H:%M')} ／ 生成元: `scripts/analyze_export.py`")
    a("")
    a("> このレポートは boss-clone の学習データ前処理を始める前の **構造把握** が目的。")
    a("> 内容（PII・機密）は意図的に最小限の引用に留めている。")
    a("")

    a("## 1. ファイル一覧")
    a("")
    a("| ファイル | 役割 |")
    a("|---|---|")
    a(f"| `conversations.json` ({convs_size_mb:.1f} MB) | 通常チャット本体。配列 → 各対話 → `chat_messages[]` |")
    a(f"| `design_chats/*.json` ({design_stats['files']} 件) | Design Chat（β機能）のチャット |")
    a(f"| `projects/*.json` ({project_stats['files']} 件) | Project 機能のメタ + ドキュメント |")
    a(f"| `memories.json` ({mem_stats['entries']} エントリ) | Claude のプロジェクト記憶（ユーザー要約） |")
    a(f"| `users.json` ({user_stats['entries']} アカウント) | アカウント情報（氏名・メール・電話）|")
    a("")

    a("## 2. conversations.json")
    a("")
    a("### 2-1. 全体集計")
    a("")
    a("| 指標 | 値 |")
    a("|---|---:|")
    a(f"| 総対話数 | {fmt_int(convs_stats['total_convs'])} |")
    a(f"| 総メッセージ数 | {fmt_int(convs_stats['total_msgs'])} |")
    a(f"| 最古 created_at | {convs_stats['earliest'].isoformat() if convs_stats['earliest'] else '—'} |")
    a(f"| 最新 created_at | {convs_stats['latest'].isoformat() if convs_stats['latest'] else '—'} |")
    if convs_stats['earliest'] and convs_stats['latest']:
        days = (convs_stats['latest'] - convs_stats['earliest']).days
        a(f"| 期間（日数） | {days} 日 |")
    a(f"| 1対話あたりメッセージ数 (平均/中央/95%ile/最大) | "
      f"{convs_stats['msgs_per_conv_avg']:.1f} / "
      f"{convs_stats['msgs_per_conv_median']:.0f} / "
      f"{convs_stats['msgs_per_conv_p95']:.0f} / "
      f"{convs_stats['msgs_per_conv_max']} |")
    a("")

    a("### 2-2. sender 別の発言量")
    a("")
    a("| sender | メッセージ数 | 総文字数 | 概算トークン数（÷1.5） |")
    a("|---|---:|---:|---:|")
    for sender in sorted(convs_stats["sender_counts"], key=lambda k: -convs_stats["sender_counts"][k]):
        a(f"| `{sender}` | {fmt_int(convs_stats['sender_counts'][sender])} "
          f"| {fmt_int(convs_stats['sender_chars'].get(sender, 0))} "
          f"| {fmt_int(convs_stats['sender_tokens_est'].get(sender, 0))} |")
    a("")
    a("> トークン概算は日本語混在テキストの素朴な目安（1.5 文字/トークン）。"
      "実際に Gemini に投入する際は tokenizer で再計算する。")
    a("")

    a("### 2-3. データ品質")
    a("")
    a("| チェック | 結果 |")
    a("|---|---:|")
    a(f"| 空 text のメッセージ数 | {fmt_int(convs_stats['empty_text_msgs'])} |")
    a(f"| sender が null/不明のメッセージ数 | {fmt_int(convs_stats['null_sender'])} |")
    a(f"| 添付ファイル付きメッセージ | {fmt_int(convs_stats['has_attachments_msgs'])} |")
    a(f"| files 付きメッセージ | {fmt_int(convs_stats['has_files_msgs'])} |")
    a(f"| 重複 uuid の対話 | {fmt_int(convs_stats['duplicate_uuids'])} |")
    a(f"| タイトル空の対話 | {fmt_int(convs_stats['name_blank'])} |")
    a(f"| 枝分かれ（同一 parent_message_uuid に複数返信）のある対話 | {fmt_int(convs_stats['branching_convs'])} |")
    a("")

    a("### 2-4. 対話タイトル サンプル（先頭20件）")
    a("")
    for i, t in enumerate(convs_stats["titles_sample"], 1):
        a(f"{i:>2}. {t}")
    a("")

    a("### 2-5. メッセージのスキーマ")
    a("")
    a("各 `chat_messages[i]` の主フィールド:")
    a("")
    a("```")
    a("uuid                 メッセージ ID")
    a("sender               'human' | 'assistant'")
    a("text                 本文（プレーンテキスト）")
    a("content[]            multi-modal 構造。各 part に {type, text, start/stop_timestamp, flags}")
    a("created_at           ISO8601 (UTC, Z)")
    a("updated_at           ISO8601 (UTC, Z)")
    a("attachments[]        画像・ファイル等の添付メタ")
    a("files[]              添付ファイル本体メタ")
    a("parent_message_uuid  返信元メッセージ ID（枝分かれを表現）")
    a("```")
    a("")

    a("## 3. projects/")
    a("")
    a("| 指標 | 値 |")
    a("|---|---:|")
    a(f"| ファイル数 | {project_stats['files']} |")
    a(f"| 総ドキュメント数（docs[]）| {project_stats['total_docs']} |")
    a(f"| starter project 数 | {project_stats['starter_count']} |")
    a(f"| private project 数 | {project_stats['private_count']} |")
    a("")
    a("各 project の概要:")
    a("")
    a("| name | uuid | docs | starter | private |")
    a("|---|---|---:|---|---|")
    for e in project_stats["entries"]:
        a(f"| {e['name']} | `{e['uuid']}` | {e['docs']} | {'✓' if e['starter'] else ''} | {'✓' if e['private'] else ''} |")
    a("")
    a("**conversations.json との紐付け**: トップレベル conversation オブジェクトに `project` フィールドは無く、"
      "`conversations.json` 配下は **プロジェクト非紐付け（通常チャット）** と推定される。"
      "Project 機能のチャット履歴がエクスポートに含まれているか別経路で要確認。")
    a("")

    a("## 4. design_chats/")
    a("")
    a("| 指標 | 値 |")
    a("|---|---:|")
    a(f"| ファイル数 | {design_stats['files']} |")
    a(f"| 総メッセージ数 | {fmt_int(design_stats['total_msgs'])} |")
    a("")
    if design_stats["project_links"]:
        a("project への紐付け（design_chat → project.uuid）:")
        a("")
        for puuid, n in design_stats["project_links"].items():
            a(f"- `{puuid}` ← {n} 件")
        a("")

    a("## 5. memories.json / users.json")
    a("")
    a(f"- memories.json: {mem_stats['entries']} エントリ、総文字数 {fmt_int(mem_stats['total_chars'])}（PIIと業務機密を多数含むため本文は引用しない）")
    a(f"- users.json: {user_stats['entries']} アカウント（氏名・メール・電話番号を含む確定PII）")
    a("")

    a("## 6. データ品質の所感")
    a("")
    a("- **文字化け**: コンソール表示時に化けて見える箇所はあるが、ファイル自体は UTF-8 正常。"
      "Python 側で `encoding='utf-8'` を明示すれば本文は問題なく読める（[lessons.md L-001](../../lessons.md)）。")
    a("- **重複/欠損**: 上記 2-3 表のとおり。事前の dedup は不要レベル。")
    a("- **枝分かれ対話**: `parent_message_uuid` で retry / 別案分岐が表現されている。"
      "学習データ化する際は「最終ブランチ」だけを採るか、全ブランチ採るかを設計判断する必要あり。")
    a("- **multi-modal**: `attachments` / `files` フィールドあり。最初の RAG 化フェーズではテキストだけで進めて、"
      "画像・添付の扱いは後段。")
    a("")

    a("## 7. 学習データとして使う際の注意点（PII・機密）")
    a("")
    a("**重要: このエクスポートは公開リポジトリへ載せられないだけでなく、Gemini への投入経路も設計が要る。**")
    a("")
    a("- `users.json` に氏名・メアド・電話番号（**確定 PII**）")
    a("- `memories.json` に勤務先名・親会社名・部署・役職・社長名・社内検討中のビジネス構想（**業務機密**）")
    a("- `conversations.json` 本文にも同類の固有名詞が混入している可能性が高い")
    a("")
    a("### 7-1. CLAUDE.md トリガー判定の再評価")
    a("")
    a("初期判定で `[PII]` を ☐ としていたが、上記により **[PII] を ☑ に格上げ**するのが妥当。")
    a("ハッカソン提出物の範囲では『フル装備の C-6（個人情報保護対応）』は採用しないにせよ、")
    a("最低限の境界設計（下記）は必須として CLAUDE.md と lessons.md に反映する候補。")
    a("")
    a("### 7-2. 当面の運用ライン（提案）")
    a("")
    a("- **取り込み**: `data/raw/claude_export/` のまま git 管理外（既に `.gitignore` 済）")
    a("- **加工**: `data/processed/` で **固有名詞マスク版** と **生データ版** を分ける。")
    a("  Vector Search のインデックスはマスク版で作る、もしくは個人 GCP プロジェクトに閉じてアクセス制御する")
    a("- **公開デモ**: 部長クローンの『判断スタイル』を再現するのが目的。固有名詞や社内構想を出力させない設計を")
    a("  プロンプト + 出力フィルタの**2層**で担保する（nfr-base.md E-2「構造で境界を守る」）")
    a("- **学習データのコミット**: いかなる派生物（要約、ベクター生データ、サンプル抜粋）も git に含めない")
    a("")
    a("### 7-3. ストリーミング読み込み（ijson）の検討結果")
    a("")
    a("- conversations.json は 75MB。`json.load` で展開すると数百MB程度。個人ローカル環境なら余裕。")
    a("- 解析は基本1回限りで、本番処理（チャンク化・埋め込み生成）はメッセージ単位で逐次処理する設計にする。")
    a("- → **当面は標準 `json` で十分**。チャンク化フェーズで I/O プロファイルを見て、必要なら ijson に切替。")

    return "\n".join(lines) + "\n"


def main() -> None:
    convs_path = EXPORT / "conversations.json"
    convs_size_mb = convs_path.stat().st_size / (1024 * 1024)

    print(f"[load] {convs_path.name} ({convs_size_mb:.1f} MB)")
    convs = load_json(convs_path)
    print(f"[analyze] conversations: {len(convs)} convs")
    convs_stats = analyze_conversations(convs)

    print("[analyze] design_chats/")
    design_stats = analyze_design_chats(EXPORT / "design_chats")
    print("[analyze] projects/")
    project_stats = analyze_projects(EXPORT / "projects")
    print("[analyze] memories.json")
    mem_stats = analyze_memories(EXPORT / "memories.json")
    print("[analyze] users.json")
    user_stats = analyze_users(EXPORT / "users.json")

    report = render_report(convs_size_mb, convs_stats, design_stats, project_stats, mem_stats, user_stats)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(report, encoding="utf-8", newline="\n")
    print(f"[write] {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
