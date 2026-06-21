"""Firestore へのバッチ書き込み。

仕様: schema_spec.md §3 / §5.1 Step 6
- google-cloud-firestore SDK が必要（pip install google-cloud-firestore）
- batch_writer で 500 件以下ずつコミット
- ImportError 時は明確なメッセージで終端
"""
from __future__ import annotations

from typing import Iterable


def _import_firestore():
    try:
        from google.cloud import firestore  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "google-cloud-firestore が未インストールです。\n"
            "  .venv/Scripts/pip.exe install google-cloud-firestore\n"
            "を実行してください。"
        ) from e
    return firestore


def get_client(project: str, database: str = "(default)"):
    firestore = _import_firestore()
    return firestore.Client(project=project, database=database)


def batch_write(
    client,
    collection: str,
    docs: Iterable[tuple[str, dict]],
    chunk_size: int = 500,
) -> int:
    """(doc_id, data) のイテラブルを chunk_size 件ずつ commit。書き込み件数を返す。"""
    total = 0
    chunk: list[tuple[str, dict]] = []
    coll = client.collection(collection)

    def flush(c: list[tuple[str, dict]]) -> None:
        nonlocal total
        if not c:
            return
        batch = client.batch()
        for doc_id, data in c:
            batch.set(coll.document(doc_id), data)
        batch.commit()
        total += len(c)

    for entry in docs:
        chunk.append(entry)
        if len(chunk) >= chunk_size:
            flush(chunk)
            chunk = []
    flush(chunk)
    return total


def count_docs(client, collection: str) -> int:
    coll = client.collection(collection)
    # Firestore SDK の count aggregation
    agg = coll.count().get()
    # 返り値はリスト構造。1要素目の value を取る
    try:
        return int(agg[0][0].value)
    except Exception:
        # SDK バージョン差異の保険: 全件 stream して数える（小規模時のみ）
        return sum(1 for _ in coll.stream())
