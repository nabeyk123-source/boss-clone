"""session_history Firestore コレクション操作。

仕様: docs/web_ui_spec.md §5

Firestore に保存する 1 セッション = 1 相談（user_query から最終 final_response まで）。
ドキュメント ID = session_id（uuid prefix）。

Fields:
  session_id, user_id, started_at, finished_at,
  user_query, user_answers,
  system1_conclusion, system2_conclusion, alignment,
  final_response_text, final_response_summary,
  agent_traces, message_count
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def _get_client():
    """Lazy import + 同じ client を使い回す。"""
    from google.cloud import firestore
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "boss-clone-2026")
    return firestore.Client(project=project, database="(default)")


def save_session(
    *,
    session_id: str,
    user_id: str,
    user_query: str,
    user_answers: list[dict] | None,
    system1_output: dict | None,
    system2_output: dict | None,
    final_response: dict | None,
    agent_traces: list[dict] | None,
    message_count: int,
    started_at: str | None = None,
) -> bool:
    """1 セッションを Firestore に upsert。例外時は False を返す。"""
    try:
        client = _get_client()
        coll = client.collection("session_history")
        doc = {
            "session_id": session_id,
            "user_id": user_id,
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "user_query": user_query,
            "user_answers": user_answers or [],
            "system1_conclusion": (system1_output or {}).get("intuitive_conclusion"),
            "system1_phrase": (system1_output or {}).get("intuitive_phrase"),
            "system2_conclusion": (system2_output or {}).get("thoughtful_conclusion"),
            "alignment": (final_response or {}).get("alignment"),
            "final_response_text": (final_response or {}).get("raw_response"),
            "final_response_summary": _make_summary(final_response or {}),
            "agent_traces": [_sanitize_trace(t) for t in (agent_traces or [])],
            "message_count": message_count,
        }
        coll.document(session_id).set(doc)
        return True
    except Exception:  # noqa: BLE001
        return False


def list_recent(limit: int = 20) -> list[dict]:
    """時系列降順で最新セッションを取得。例外時は空リスト。"""
    try:
        client = _get_client()
        coll = client.collection("session_history")
        q = coll.order_by("started_at", direction="DESCENDING").limit(limit)
        return [snap.to_dict() for snap in q.stream()]
    except Exception:  # noqa: BLE001
        return []


def _make_summary(final_response: dict) -> str:
    """final_response から先頭 80 字程度のサマリを作る。"""
    issues = final_response.get("issues_summary") or []
    if issues:
        return " / ".join(str(i) for i in issues[:3])[:120]
    raw = (final_response.get("raw_response") or "").strip()
    return raw[:120]


def _sanitize_trace(t: dict) -> dict:
    """agent_traces を Firestore 保存用に軽量化（raw_response 全文は重いので 1000 字まで）。"""
    out = {k: v for k, v in t.items() if k != "raw_response"}
    out["raw_response_preview"] = (t.get("raw_response") or "")[:1000]
    return out
