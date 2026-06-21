"""監査ログ・既処理判定のヘルパー。

仕様: masking_pipeline_spec.md §4.10 / §7
"""
from __future__ import annotations

import json
from pathlib import Path


def is_already_processed(audit_log_path: Path, processing_id: str) -> bool:
    if not audit_log_path.exists():
        return False
    with audit_log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("processing_id") == processing_id:
                return True
    return False


class JsonlWriter:
    """with 文で開閉する jsonl 書き出しヘルパー。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = None
        self.count = 0

    def __enter__(self) -> "JsonlWriter":
        self._f = self.path.open("w", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def write(self, obj: dict) -> None:
        assert self._f is not None
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.count += 1
