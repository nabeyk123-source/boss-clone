"""わたなべ部長クローン Web UI（Streamlit）— Phase 2-2 メインタブ完成版。

仕様: docs/web_ui_spec.md

設計:
- マルチターン対話: Turn 1（質問生成）→ ユーザー回答 → Turn 2（統合判断）
- Streamlit 制約のため System1 + System2 は Turn 1 で並列 gather（asyncio.gather）
- ユーザーが質問に答える時間が削減効果として残る（Turn 2 は Synthesizer のみ）
- 選択肢ボタンは st.pills（複数選択、カプセル型）
- 「その他」は自由入力欄を別途展開

起動:
    .venv/Scripts/streamlit run scripts/boss_clone_web.py
"""
from __future__ import annotations

import asyncio
import base64
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# L-001 サロゲート対策
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

# ===== Boss Clone Lib import =====

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from boss_clone_lib.agents.synthesizer_agent import SynthesizerAgent  # noqa: E402
from boss_clone_lib.agents.system1_agent import System1Agent  # noqa: E402
from boss_clone_lib.agents.system2_agent import System2Agent  # noqa: E402
from boss_clone_lib.document_loader import (  # noqa: E402
    MAX_CHARS as DOC_MAX_CHARS,
    SUPPORTED_FORMATS as DOC_SUPPORTED_FORMATS,
    load_document,
)
from boss_clone_lib.retrieval.service import RetrievalService  # noqa: E402
from boss_clone_lib.session import history as history_mod  # noqa: E402

import queue as queue_mod  # noqa: E402
import threading  # noqa: E402

APP_NAME = "boss_clone"
USER_ID = "watanabe"


# ===== Page config =====

st.set_page_config(
    page_title="わたなべ部長クローン",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ===== Asset paths =====

ASSETS_DIR = ROOT / "assets"
WATANABE_ICON = ASSETS_DIR / "watanabe_icon.png"


def _icon_html(size: int = 40, extra_class: str = "") -> str:
    """わたなべ部長アイコンを HTML として返す（サイズ指定可）。"""
    style = f"width: {size}px; height: {size}px; font-size: {int(size * 0.42)}px;"
    cls = f"watanabe-icon {extra_class}".strip()
    if WATANABE_ICON.exists():
        try:
            data = WATANABE_ICON.read_bytes()
            ext = WATANABE_ICON.suffix.lstrip(".").lower() or "png"
            mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
            b64 = base64.b64encode(data).decode("ascii")
            return f'<img class="{cls}" style="{style}" src="data:{mime};base64,{b64}" alt="わたなべ" />'
        except OSError:
            pass
    return f'<div class="{cls} watanabe-icon-placeholder" style="{style}">W</div>'


# ===== Custom CSS =====

CUSTOM_CSS = """
<style>
.main { background-color: #FAFAFA; }
.block-container {
    padding-top: 3.5rem;
    padding-bottom: 5rem;
    max-width: 1100px;
}

.app-header {
    font-family: 'Noto Sans JP', sans-serif;
    font-weight: 700;
    font-size: 24px;
    color: #212121;
    margin-top: 0.5rem;
    margin-bottom: 0.25rem;
    line-height: 1.4;
}
.app-subtitle {
    font-size: 13px;
    color: #757575;
    margin-bottom: 1rem;
}

.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background-color: transparent;
    border-bottom: 1px solid #E0E0E0;
}
.stTabs [data-baseweb="tab"] {
    font-size: 16px;
    font-weight: 500;
    color: #757575;
    padding: 8px 18px;
    background-color: transparent;
    border-radius: 8px 8px 0 0;
}
.stTabs [aria-selected="true"] {
    color: #1976D2 !important;
    background-color: #E3F2FD !important;
}

.dialog-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 8px 0 24px 0;
    padding: 12px 4px;
    border-bottom: 1px solid #E0E0E0;
}
.dialog-title {
    font-size: 20px;
    font-weight: 700;
    color: #212121;
    font-family: 'Noto Sans JP', sans-serif;
}
.dialog-subtitle {
    font-size: 12px;
    color: #757575;
    margin-top: 4px;
}
.persona-badge {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
}
.persona-label {
    font-size: 12px;
    color: #757575;
    font-family: 'Noto Sans JP', sans-serif;
    font-weight: 500;
}

.watanabe-icon { border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.watanabe-icon-placeholder {
    background: linear-gradient(135deg, #1976D2, #42A5F5);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-family: 'Noto Sans JP', sans-serif;
    user-select: none;
}

.message-row {
    display: flex;
    flex-direction: column;
    margin-bottom: 14px;
}
.message-row.assistant { align-items: flex-start; }
.message-row.user { align-items: flex-end; }
.sender-label {
    font-size: 11px;
    color: #757575;
    font-weight: 500;
    margin-bottom: 4px;
    padding: 0 6px;
    font-family: 'Noto Sans JP', sans-serif;
}
.message-bubble {
    max-width: 70%;
    padding: 14px 18px;
    border-radius: 14px;
    font-size: 16px;
    line-height: 1.7;
    color: #212121;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'Noto Sans JP', sans-serif;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.message-bubble.assistant { background-color: #E3F2FD; }
.message-bubble.user { background-color: #F5F5F5; }
.bubble-system {
    text-align: center;
    color: #757575;
    background-color: transparent;
    font-size: 13px;
    margin: 8px auto;
}

.question-block {
    background-color: white;
    border: 1px solid #E0E0E0;
    border-radius: 12px;
    padding: 16px 18px;
    margin-bottom: 14px;
}
.question-text {
    font-size: 15px;
    font-weight: 600;
    color: #212121;
    margin-bottom: 8px;
}

/* st.pills の見た目を寄せる */
button[kind="pillsItem"] {
    border-radius: 20px !important;
    border: 1.5px solid #1976D2 !important;
    color: #1976D2 !important;
    background-color: white !important;
    font-size: 14px !important;
    padding: 6px 14px !important;
}
button[kind="pillsItem"][aria-pressed="true"] {
    background-color: #1976D2 !important;
    color: white !important;
}

.agent-card {
    background-color: white;
    border: 1px solid #E0E0E0;
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 12px;
}
.agent-card-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 10px;
    border-bottom: 1px solid #E0E0E0;
    padding-bottom: 8px;
}
.agent-card-name { font-weight: 700; font-size: 18px; color: #212121; }
.agent-card-meta { font-size: 12px; color: #757575; }
.agent-card-body { font-size: 14px; color: #212121; line-height: 1.6; }
.agent-card-stat { font-size: 13px; color: #1976D2; font-weight: 500; margin-top: 6px; }

.history-item {
    background-color: white;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 10px;
}
.history-date { font-size: 12px; color: #757575; margin-bottom: 4px; }
.history-query { font-size: 15px; font-weight: 500; color: #212121; margin-bottom: 4px; }
.history-summary { font-size: 13px; color: #757575; }

.meta-text { font-size: 12px; color: #757575; margin-top: 4px; }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ===== Session State 初期化 =====

def _init_state():
    defaults = {
        "session_id": str(uuid.uuid4())[:8],
        "messages": [],  # [{role, content, kind, timestamp}]
        # input | processing_turn1 | answering | processing_turn2 | done
        "turn_state": "input",
        "current_query": "",
        "session_started_at": datetime.now(timezone.utc).isoformat(),
        "system1_output": None,
        "system2_output": None,
        "synthesizer_output": None,
        "agent_traces": [],
        "pending_questions": [],
        "pending_user_answers": None,
        "other_text": {},
        "history_save_status": None,  # 'ok' | 'failed' | None
        # 資料レビューモード
        "attached_document": None,         # 現在のターンで使う資料（dict or None）
        "uploader_key_seed": 0,            # uploader リセット用（key 更新で内容クリア）
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ===== 動的 spinner: 別スレッドで asyncio.run、メインで文言更新 =====

def run_with_dynamic_status(coro_factory, status_placeholder):
    """coro_factory() で生成した coroutine を別スレッドで実行、status を時間経過で更新。

    coro_factory: 引数無しで coroutine を返す callable。スレッド内で asyncio.run() に渡す。
    status_placeholder: st.empty() の戻り値（status_placeholder.info(...) で表示更新）
    返り値: (result, elapsed_s)
    例外は呼び出し側が処理（result が None で elapsed のみ取れる）
    """
    result_q = queue_mod.Queue()

    def worker():
        try:
            result_q.put(("ok", asyncio.run(coro_factory())))
        except Exception as e:  # noqa: BLE001
            result_q.put(("error", e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t0 = time.perf_counter()

    last_label = ""
    while t.is_alive():
        elapsed = time.perf_counter() - t0
        if elapsed < 5:
            label = "🤔 考えています…"
        elif elapsed < 15:
            label = "✍️ 論点を整理しています…"
        elif elapsed < 30:
            label = "⏳ あと少しで答えが出ます…"
        else:
            label = "⏳ 深く考えています…"
        if label != last_label:
            status_placeholder.info(f"{label}  ({elapsed:.0f}s)")
            last_label = label
        time.sleep(0.4)

    t.join(timeout=1.0)
    elapsed = time.perf_counter() - t0
    if result_q.empty():
        raise RuntimeError("worker thread finished without result")
    status, payload = result_q.get()
    if status == "error":
        raise payload
    return payload, elapsed


_init_state()


# ===== Retrieval (キャッシュ) =====

@st.cache_resource
def get_retrieval() -> RetrievalService:
    svc = RetrievalService()
    # warmup を非同期で呼ぶ
    try:
        asyncio.run(svc.embed_query("warmup"))
    except Exception:
        pass
    return svc


# ===== Async helpers =====

async def _run_single_agent(agent, session_id_suffix: str, state: dict, query: str):
    """1 つのエージェントだけを InMemoryRunner で実行して最終 state を返す。"""
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    sid = f"{st.session_state.session_id}-{session_id_suffix}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=USER_ID, session_id=sid, state=state,
    )
    msg = genai_types.Content(role="user", parts=[genai_types.Part(text=query)])
    async for _ in runner.run_async(user_id=USER_ID, session_id=sid, new_message=msg):
        pass
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=USER_ID, session_id=sid,
    )
    return sess.state


async def run_turn1(user_query: str, attached_document: dict | None = None):
    """System1 + System2 を並列で gather。両方の結果を返す。"""
    retrieval = get_retrieval()
    s1 = System1Agent(name="system1", description="直感")
    s1.set_retrieval(retrieval)
    s2 = System2Agent(name="system2", description="熟考")
    s2.set_retrieval(retrieval)

    base_state = {"user_query": user_query, "attached_document": attached_document}
    t0 = time.perf_counter()
    s1_task = _run_single_agent(s1, "s1", dict(base_state), user_query)
    s2_task = _run_single_agent(s2, "s2", dict(base_state), user_query)
    s1_state, s2_state = await asyncio.gather(s1_task, s2_task)
    elapsed = time.perf_counter() - t0
    return s1_state.get("system1_output", {}), s2_state.get("system2_output", {}), elapsed


async def run_turn2(
    user_query: str,
    s1_out: dict,
    s2_out: dict,
    user_answers: list[dict],
    attached_document: dict | None = None,
):
    """Synthesizer を実行して final_response を返す。"""
    syn = SynthesizerAgent(name="synthesizer", description="統合")
    t0 = time.perf_counter()
    syn_state = await _run_single_agent(
        syn, "syn",
        {
            "user_query": user_query,
            "system1_output": s1_out,
            "system2_output": s2_out,
            "user_answers": user_answers,
            "attached_document": attached_document,
        },
        user_query,
    )
    elapsed = time.perf_counter() - t0
    return syn_state.get("final_response", {}), elapsed


def _push_msg(role: str, content: str, kind: str = "text"):
    st.session_state.messages.append({
        "role": role,
        "content": content,
        "kind": kind,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _push_trace(agent: str, duration_s: float, raw: str, status: str = "ok", extra: dict | None = None):
    entry = {
        "agent": agent,
        "duration_ms": int(duration_s * 1000),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "raw_response": (raw or "")[:5000],
        "status": status,
    }
    if extra:
        entry.update(extra)
    st.session_state.agent_traces.append(entry)


# ===== Header =====

st.markdown('<div class="app-header">🎯 わたなべ部長クローン</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="app-subtitle">マルチエージェントで動く、わたなべ部長の判断 OS</div>',
    unsafe_allow_html=True,
)

# ===== Tabs =====

tab_main, tab_thinking, tab_history, tab_settings = st.tabs(
    ["💬 メイン", "🧠 思考プロセス", "📚 履歴", "⚙️ 設定"]
)


# ============================================
# Main Tab
# ============================================
with tab_main:
    # Dialog header
    persona_icon = _icon_html(size=80, extra_class="persona-icon")
    st.markdown(
        f"""
<div class="dialog-header">
  <div>
    <div class="dialog-title">💬 部長との対話</div>
    <div class="dialog-subtitle">相談を入力してください</div>
  </div>
  <div class="persona-badge">
    {persona_icon}
    <div class="persona-label">わたなべ部長</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # 初回挨拶（履歴が空の時だけ）
    if not st.session_state.messages:
        st.markdown(
            '<div class="message-row assistant" translate="no">'
            '<div class="sender-label">わたなべ部長</div>'
            '<div class="message-bubble assistant" lang="ja" translate="no">'
            'おつかれさま。よろしくお願いします。'
            '</div></div>',
            unsafe_allow_html=True,
        )

    # 履歴を順次表示（送信者ラベル + translate="no" でブラウザ翻訳抑止）
    for msg in st.session_state.messages:
        role = msg["role"]
        if role in ("assistant", "user"):
            safe = (msg["content"] or "").replace("<", "&lt;").replace(">", "&gt;")
            label = "わたなべ部長" if role == "assistant" else "あなた"
            st.markdown(
                f'<div class="message-row {role}" translate="no">'
                f'<div class="sender-label">{label}</div>'
                f'<div class="message-bubble {role}" lang="ja" translate="no">{safe}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="bubble-system">{msg["content"]}</div>',
                unsafe_allow_html=True,
            )

    # ===== 状態別 UI =====

    # processing_turn1: ユーザー入力済み・履歴描画済み → Turn 1 実行
    if st.session_state.turn_state == "processing_turn1":
        status_placeholder = st.empty()
        try:
            current_query = st.session_state.current_query
            current_doc = st.session_state.attached_document
            (s1_out, s2_out, total_elapsed), _wall = run_with_dynamic_status(
                lambda: run_turn1(current_query, current_doc),
                status_placeholder,
            )
            st.session_state.system1_output = s1_out
            st.session_state.system2_output = s2_out
            _push_trace(
                "system1",
                s1_out.get("_latency_s", 0.0),
                s1_out.get("raw_response", ""),
                status=s1_out.get("_llm_status", "ok"),
                extra={"conclusion": s1_out.get("intuitive_conclusion")},
            )
            _push_trace(
                "system2",
                s2_out.get("_latency_s", 0.0),
                s2_out.get("raw_response", ""),
                status=s2_out.get("_llm_status", "ok"),
                extra={"conclusion": s2_out.get("thoughtful_conclusion")},
            )
            questions = s1_out.get("questions") or []
            st.session_state.pending_questions = questions

            intuitive_phrase = s1_out.get("intuitive_phrase") or ""
            if intuitive_phrase and questions:
                intro = f"{intuitive_phrase}\n\nまずは、確認させてもらえる？"
            elif questions:
                intro = "確認させてもらえる？"
            else:
                intro = "状況、理解した。少し整理してみよう。"
            _push_msg("assistant", intro)

            if questions:
                st.session_state.turn_state = "answering"
            else:
                # 質問なし → 即 Turn 2
                st.session_state.pending_user_answers = []
                st.session_state.turn_state = "processing_turn2"
        except Exception as e:  # noqa: BLE001
            _push_msg("system", f"⚠️ Turn 1 でエラー: {type(e).__name__}: {e}")
            st.session_state.turn_state = "input"
        status_placeholder.empty()
        st.rerun()

    # processing_turn2: 回答済み・履歴描画済み → Synthesizer 実行
    if st.session_state.turn_state == "processing_turn2":
        status_placeholder = st.empty()
        try:
            current_query = st.session_state.current_query
            s1_out = st.session_state.system1_output
            s2_out = st.session_state.system2_output
            user_answers = st.session_state.pending_user_answers or []
            current_doc = st.session_state.attached_document
            (final, syn_elapsed), _wall = run_with_dynamic_status(
                lambda: run_turn2(current_query, s1_out, s2_out, user_answers, current_doc),
                status_placeholder,
            )
            st.session_state.synthesizer_output = final
            _push_trace(
                "synthesizer", syn_elapsed,
                final.get("raw_response", ""),
                status=final.get("_llm_status", "ok"),
                extra={"alignment": final.get("alignment")},
            )
            _push_msg("assistant", final.get("raw_response", "(empty)"))
            st.session_state.turn_state = "done"
            # Firestore session_history に保存
            saved = history_mod.save_session(
                session_id=st.session_state.session_id,
                user_id=USER_ID,
                user_query=current_query,
                user_answers=user_answers,
                system1_output=s1_out,
                system2_output=s2_out,
                final_response=final,
                agent_traces=st.session_state.agent_traces,
                message_count=len(st.session_state.messages),
                started_at=st.session_state.session_started_at,
            )
            st.session_state.history_save_status = "ok" if saved else "failed"
        except Exception as e:  # noqa: BLE001
            _push_msg("system", f"⚠️ 統合判断でエラー: {type(e).__name__}: {e}")
            st.session_state.turn_state = "done"
        status_placeholder.empty()
        st.rerun()

    if st.session_state.turn_state == "answering" and st.session_state.pending_questions:
        # 質問選択肢ボタン表示
        st.markdown('<div class="meta-text">気になるところを選んで、回答するを押してください</div>', unsafe_allow_html=True)

        for q_idx, q in enumerate(st.session_state.pending_questions):
            with st.container():
                qtext = q.get("question", "")
                options = q.get("options") or []
                st.markdown(
                    f'<div class="question-block"><div class="question-text">Q{q_idx + 1}: {qtext}</div></div>',
                    unsafe_allow_html=True,
                )
                # pills（複数選択）
                if options:
                    st.pills(
                        label=f"選択肢 (Q{q_idx + 1})",
                        options=options,
                        selection_mode="multi",
                        key=f"pills_q{q_idx}",
                        label_visibility="collapsed",
                    )
                # その他（自由入力）
                with st.expander("その他（自由入力）", expanded=False):
                    st.text_input(
                        label=f"その他 Q{q_idx + 1}",
                        key=f"other_q{q_idx}",
                        label_visibility="collapsed",
                        placeholder="該当する選択肢が無ければ、ここに自由に入力してください",
                    )

        # 「回答する」ボタン — ここでは answers を構築して messages にだけ追加、Turn 2 本処理は次サイクル
        if st.button("回答する", type="primary", use_container_width=True):
            user_answers = []
            answer_summary_lines = []
            for q_idx, q in enumerate(st.session_state.pending_questions):
                selected = st.session_state.get(f"pills_q{q_idx}") or []
                free_text = (st.session_state.get(f"other_q{q_idx}") or "").strip() or None
                user_answers.append({
                    "question": q.get("question", ""),
                    "selected_options": list(selected),
                    "free_text": free_text,
                })
                if selected or free_text:
                    parts = []
                    if selected:
                        parts.append(" / ".join(selected))
                    if free_text:
                        parts.append(f"（自由: {free_text}）")
                    answer_summary_lines.append(f"Q{q_idx + 1}: " + " ".join(parts))
                else:
                    answer_summary_lines.append(f"Q{q_idx + 1}: (未回答)")

            _push_msg("user", "\n".join(answer_summary_lines))
            st.session_state.pending_user_answers = user_answers
            st.session_state.turn_state = "processing_turn2"
            st.rerun()

    # ===== 資料添付（chat_input の直上）=====
    uploader_disabled = st.session_state.turn_state not in ("input", "done")
    with st.expander("📎 資料を添付してレビューしてもらう（任意）", expanded=False):
        st.markdown(
            f"<div class='meta-text'>対応形式: {', '.join('.' + f for f in sorted(DOC_SUPPORTED_FORMATS))} "
            f"／ {DOC_MAX_CHARS:,} 字を超える資料は先頭から切り詰めます。</div>",
            unsafe_allow_html=True,
        )
        uploader_key = f"file_uploader_{st.session_state.uploader_key_seed}"
        uploaded = st.file_uploader(
            label="資料ファイル",
            type=sorted(DOC_SUPPORTED_FORMATS),
            accept_multiple_files=False,
            key=uploader_key,
            disabled=uploader_disabled,
            label_visibility="collapsed",
        )
        if uploaded is not None and (
            st.session_state.attached_document is None
            or st.session_state.attached_document.get("filename") != uploaded.name
            or st.session_state.attached_document.get("original_char_count") != uploaded.size
        ):
            # 新規ファイル選択 → ロード
            try:
                data = uploaded.getvalue()
                doc = load_document(uploaded.name, data)
            except Exception as e:  # noqa: BLE001
                doc = {
                    "filename": uploaded.name, "format": "?", "content": "",
                    "char_count": 0, "original_char_count": 0, "truncated": False,
                    "warnings": [], "status": "error", "error": f"{type(e).__name__}: {e}",
                }
            st.session_state.attached_document = doc

        doc = st.session_state.attached_document
        if doc:
            if doc.get("status") == "ok":
                st.success(
                    f"📎 添付済み: **{doc['filename']}** "
                    f"(.{doc['format']} / {doc['char_count']:,} 字)"
                )
                for w in (doc.get("warnings") or []):
                    st.warning(w)
            else:
                st.error(f"📎 読み込み失敗: {doc.get('filename')} — {doc.get('error')}")
            if st.button("✖ 添付を外す", key="detach_doc"):
                st.session_state.attached_document = None
                st.session_state.uploader_key_seed += 1
                st.rerun()

    # chat_input は常に画面下部に固定（input / done で有効）
    user_input_disabled = st.session_state.turn_state not in ("input", "done")
    user_input = st.chat_input(
        "相談を入力...",
        disabled=user_input_disabled,
    )

    if user_input:
        # 新規対話開始 — 過去の done 状態なら state をリセット
        if st.session_state.turn_state == "done":
            st.session_state.system1_output = None
            st.session_state.system2_output = None
            st.session_state.synthesizer_output = None
            st.session_state.pending_questions = []
            st.session_state.pending_user_answers = None
            st.session_state.other_text = {}

        st.session_state.current_query = user_input
        # 添付がある場合は user バブルにメタ情報を併記
        doc = st.session_state.attached_document
        if doc and doc.get("status") == "ok":
            doc_line = f"📎 {doc['filename']} (.{doc['format']} / {doc['char_count']:,} 字)"
            _push_msg("user", f"{doc_line}\n\n{user_input}")
        else:
            _push_msg("user", user_input)
        # 状態フラグだけ立てて即 rerun → 次サイクルで user バブル描画 + Turn 1 実行
        st.session_state.turn_state = "processing_turn1"
        st.rerun()


# ============================================
# Thinking Process Tab
# ============================================
with tab_thinking:
    st.markdown("##### 🧠 マルチエージェント思考プロセス")
    if st.session_state.current_query:
        st.markdown(
            f'<div class="meta-text">現在の相談: 「{st.session_state.current_query[:100]}」</div>',
            unsafe_allow_html=True,
        )
    st.markdown("&nbsp;", unsafe_allow_html=True)

    # System1 / System2 横並び
    s1_out = st.session_state.system1_output
    s2_out = st.session_state.system2_output
    syn_out = st.session_state.synthesizer_output

    def _trace_for(agent_name: str) -> dict | None:
        for t in reversed(st.session_state.agent_traces):
            if t["agent"] == agent_name:
                return t
        return None

    c1, c2 = st.columns(2)
    with c1:
        t1 = _trace_for("system1")
        status_line = f"完了: {t1['duration_ms']}ms" if t1 else "— 待機中 —"
        conclusion_line = ""
        if s1_out:
            conclusion_line = f"結論: {s1_out.get('intuitive_conclusion')} (一致度: {s1_out.get('match_confidence')})"
        st.markdown(
            f"""
<div class="agent-card">
  <div class="agent-card-header">
    <div class="agent-card-name">🧠 System 1 — 直感</div>
    <div class="agent-card-meta">gemini-2.5-flash / thinking: 0</div>
  </div>
  <div class="agent-card-body">
    過去判断パターン (Vector Search top-5) から即決 + 確認質問を生成
  </div>
  <div class="agent-card-stat">{status_line}{('<br>' + conclusion_line) if conclusion_line else ''}</div>
</div>
""",
            unsafe_allow_html=True,
        )
        if s1_out and s1_out.get("raw_response"):
            with st.expander("System1 詳細を展開"):
                st.code(s1_out["raw_response"][:3000], language="text")

    with c2:
        t2 = _trace_for("system2")
        status_line = f"完了: {t2['duration_ms']}ms" if t2 else "— 待機中 —"
        conclusion_line = ""
        if s2_out:
            n_issues = len(s2_out.get("issues") or [])
            conclusion_line = f"結論: {s2_out.get('thoughtful_conclusion')} / 論点 {n_issues} 件"
        st.markdown(
            f"""
<div class="agent-card">
  <div class="agent-card-header">
    <div class="agent-card-name">🤔 System 2 — 熟考</div>
    <div class="agent-card-meta">gemini-2.5-pro / thinking: 1000</div>
  </div>
  <div class="agent-card-body">
    Acme KB (理念 / 規程 / 戦略 / 暗黙知) 4 層を網羅検討
  </div>
  <div class="agent-card-stat">{status_line}{('<br>' + conclusion_line) if conclusion_line else ''}</div>
</div>
""",
            unsafe_allow_html=True,
        )
        if s2_out and s2_out.get("raw_response"):
            with st.expander("System2 詳細を展開"):
                st.code(s2_out["raw_response"][:3000], language="text")

    # Synthesizer フル幅
    t3 = _trace_for("synthesizer")
    status_line = f"完了: {t3['duration_ms']}ms" if t3 else "— 待機中 —"
    alignment_line = ""
    if syn_out:
        alignment_line = f"Alignment: {syn_out.get('alignment')} / 出力長 {len(syn_out.get('raw_response', ''))} 文字"
    st.markdown(
        f"""
<div class="agent-card">
  <div class="agent-card-header">
    <div class="agent-card-name">🎯 Synthesizer — 統合判断</div>
    <div class="agent-card-meta">gemini-2.5-pro / thinking: 1000</div>
  </div>
  <div class="agent-card-body">
    System1 / System2 + ユーザー回答を統合し、わたなべ部長として 1 つの判断
  </div>
  <div class="agent-card-stat">{status_line}{('<br>' + alignment_line) if alignment_line else ''}</div>
</div>
""",
        unsafe_allow_html=True,
    )
    if syn_out and syn_out.get("raw_response"):
        with st.expander("Synthesizer 詳細を展開"):
            st.code(syn_out["raw_response"][:5000], language="text")

    # 並列実行効果
    if t1 and t2:
        seq = (t1["duration_ms"] + t2["duration_ms"]) / 1000
        par = max(t1["duration_ms"], t2["duration_ms"]) / 1000
        st.markdown("---")
        st.markdown(
            f"<div class='meta-text'><strong>並列実行効果</strong>: "
            f"System1 + System2 を逐次実行なら {seq:.1f}s、"
            f"並列実行で max = {par:.1f}s "
            f"（{seq - par:.1f}s 短縮）</div>",
            unsafe_allow_html=True,
        )


# ============================================
# History Tab
# ============================================
with tab_history:
    st.markdown("##### 📚 過去の対話履歴")
    st.markdown(
        '<div class="meta-text">Firestore <code>session_history</code> から最新 20 件を表示します（時系列降順）。</div>',
        unsafe_allow_html=True,
    )

    cols = st.columns([1, 5])
    with cols[0]:
        if st.button("🔄 再読み込み"):
            st.rerun()

    items = history_mod.list_recent(limit=20)
    if not items:
        st.info("まだ履歴がありません。メインタブで相談してみてください。")
    else:
        for item in items:
            started = (item.get("started_at") or "")[:19].replace("T", " ")
            query = (item.get("user_query") or "")[:60]
            alignment = item.get("alignment") or "?"
            summary = item.get("final_response_summary") or "(サマリなし)"
            s1c = item.get("system1_conclusion") or "?"
            s2c = item.get("system2_conclusion") or "?"
            safe_query = query.replace("<", "&lt;").replace(">", "&gt;")
            safe_summary = summary.replace("<", "&lt;").replace(">", "&gt;")
            st.markdown(
                f"""
<div class="history-item">
  <div class="history-date">{started} UTC — alignment: <strong>{alignment}</strong> (S1: {s1c} / S2: {s2c})</div>
  <div class="history-query">{safe_query}</div>
  <div class="history-summary">論点要約: {safe_summary}</div>
</div>
""",
                unsafe_allow_html=True,
            )


# ============================================
# Settings Tab
# ============================================
with tab_settings:
    st.markdown("##### ⚙️ 設定")
    st.markdown(
        '<div class="meta-text">Phase 2 では読み取り専用。Day 5 以降で編集可能化検討。</div>',
        unsafe_allow_html=True,
    )
    st.markdown("**モデル設定**")
    st.code(
        "System1 model:    gemini-2.5-flash  (thinking_budget=0)\n"
        "System2 model:    gemini-2.5-pro    (thinking_budget=1000)\n"
        "Synthesizer:      gemini-2.5-pro    (thinking_budget=1000)\n"
        "Embedding model:  text-multilingual-embedding-002 (dim=768)",
        language="text",
    )
    st.markdown("**検索設定**")
    st.code("pair_summaries top_k: 5\nacme_kb top_k per layer: 3", language="text")

    st.markdown("**現在のセッション**")
    st.code(
        f"session_id: {st.session_state.session_id}\n"
        f"turn_state: {st.session_state.turn_state}\n"
        f"messages:   {len(st.session_state.messages)} 件\n"
        f"agent_traces: {len(st.session_state.agent_traces)} 件",
        language="text",
    )

    if st.button("🔄 セッションをリセット", type="secondary"):
        keys = list(st.session_state.keys())
        for k in keys:
            del st.session_state[k]
        st.rerun()


# ===== Footer =====

st.markdown("---")
st.caption(
    "Boss Clone Agent — Day 4 Phase 2 (Streamlit Web UI) / DevOps × AI Agent Hackathon 2026"
)
