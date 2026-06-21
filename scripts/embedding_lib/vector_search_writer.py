"""Vector Search 用の JSONL を出力。

仕様: schema_spec.md §4 / §5.1 Step 7
- BATCH モード前提：JSONL ファイルを GCS にアップロード → gcloud で index 作成
- このモジュールはローカル JSONL 出力までを担当
- インデックス作成・デプロイは scripts/ 外の gcloud / API 呼び出しで実施

JSONL の各行（Vertex AI Vector Search 仕様）:
{
  "id": "<datapoint_id>",
  "embedding": [<float>, ...],
  "restricts": [
    {"namespace": "tag", "allow": ["OK"]},
    {"namespace": "topic_tags", "allow": ["kabe", "design"]}
  ]
}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def write_index_jsonl(
    path: Path,
    datapoints: Iterable[dict],
) -> int:
    """各 datapoint は {"id": str, "embedding": list[float], "restricts": list[dict]}。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for dp in datapoints:
            f.write(json.dumps(dp, ensure_ascii=False) + "\n")
            count += 1
    return count


def build_pair_datapoint(
    doc_id: str,
    embedding: list[float],
    tag: str,
    topic_tags: list[str],
    decision_type: str,
    created_at_iso: str | None,
) -> dict:
    """pair_summaries インデックス用 datapoint を組み立てる。"""
    restricts: list[dict] = [
        {"namespace": "tag", "allow": [tag] if tag else []},
        {"namespace": "topic_tags", "allow": topic_tags or []},
        {"namespace": "decision_type", "allow": [decision_type] if decision_type else []},
    ]
    if created_at_iso:
        # YYYY-MM までで切ってフィルタしやすくする
        ym = created_at_iso[:7]
        restricts.append({"namespace": "created_at_year_month", "allow": [ym]})
    return {
        "id": doc_id,
        "embedding": embedding,
        "restricts": [r for r in restricts if r.get("allow")],
    }


def build_acme_datapoint(
    doc_id: str,
    embedding: list[float],
    layer: str,
    category: str,
    applies_to: list[str],
) -> dict:
    restricts: list[dict] = [
        {"namespace": "layer", "allow": [layer] if layer else []},
        {"namespace": "category", "allow": [category] if category else []},
        {"namespace": "applies_to", "allow": applies_to or []},
    ]
    return {
        "id": doc_id,
        "embedding": embedding,
        "restricts": [r for r in restricts if r.get("allow")],
    }
