"""対話のメッセージから (human → assistant) ペアを抽出。

仕様: masking_pipeline_spec.md §4.3 / §4.4
- parent_message_uuid を辿って末端 → 先頭の主要ブランチを取り出す
- 連続する human → assistant をペアとして拾う
"""
from __future__ import annotations

from typing import Any


def extract_main_branch(messages: list[dict]) -> list[dict]:
    if not messages:
        return []
    by_uuid = {m["uuid"]: m for m in messages if m.get("uuid")}
    has_child: set[str] = set()
    for m in messages:
        p = m.get("parent_message_uuid")
        if p and p in by_uuid:
            has_child.add(p)

    leaves = [m for m in messages if m.get("uuid") not in has_child]
    if not leaves:
        return messages  # フォールバック: 全部リーフ判定できなければそのまま返す

    latest_leaf = max(leaves, key=lambda m: m.get("created_at") or "")

    chain: list[dict] = []
    visited: set[str] = set()
    current: dict | None = latest_leaf
    while current is not None:
        uuid = current.get("uuid")
        if not uuid or uuid in visited:
            break
        visited.add(uuid)
        chain.append(current)
        parent_uuid = current.get("parent_message_uuid")
        current = by_uuid.get(parent_uuid) if parent_uuid else None

    return list(reversed(chain))


def collect_text(msg: dict) -> str:
    """text フィールドが空なら content[].text を連結。"""
    if msg.get("text"):
        return msg["text"]
    parts: list[str] = []
    for part in msg.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            parts.append(part["text"])
    return "\n".join(parts)


def extract_pairs(messages: list[dict]) -> list[dict]:
    """連続する human → assistant をペアとして抽出。空メッセージは飛ばす。"""
    pairs: list[dict] = []
    i = 0
    while i < len(messages) - 1:
        a, b = messages[i], messages[i + 1]
        if a.get("sender") == "human" and b.get("sender") == "assistant":
            human_text = collect_text(a)
            assistant_text = collect_text(b)
            if human_text.strip() or assistant_text.strip():
                pairs.append({
                    "human_text": human_text,
                    "assistant_text": assistant_text,
                    "human_uuid": a.get("uuid"),
                    "assistant_uuid": b.get("uuid"),
                    "created_at": a.get("created_at"),
                })
            i += 2
        else:
            i += 1
    return pairs


def is_blacklisted(conversation: dict, blacklist_patterns: list[str]) -> tuple[bool, str]:
    """対話全体のテキストを連結してブラックリストパターン照合。"""
    if not blacklist_patterns:
        return False, ""
    full_text_parts: list[str] = []
    for m in conversation.get("chat_messages") or []:
        t = collect_text(m)
        if t:
            full_text_parts.append(t)
    full_text = "\n".join(full_text_parts).lower()
    for pattern in blacklist_patterns:
        if not pattern:
            continue
        if pattern.lower() in full_text:
            return True, pattern
    return False, ""
