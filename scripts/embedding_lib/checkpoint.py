"""ステージ別チェックポイント JSONL の読み書き。

仕様: 案B（pipeline 改修）の中核。
- 各ステージ完了時、key 単位で 1 行 JSONL を append（次の chunk へ進む前に fsync）
- 起動時、ckpt をロードして「すでに完了済み」の key を skip
- メモリ上の `asyncio.gather` に積む pending task を chunk_size 件以内に抑える

各行のフォーマット:
{ "key": "<pair_key>", "value": <JSON 可能な型>, "status": "ok|empty|retry_exhausted:...|...", "ts": "ISO8601" }
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_done(path: Path) -> dict[str, tuple[Any, str]]:
    """ckpt JSONL を読み {key: (value, status)} を返す。"""
    done: dict[str, tuple[Any, str]] = {}
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = row.get("key")
            if k is None:
                continue
            done[k] = (row.get("value"), row.get("status", "ok"))
    return done


class CheckpointAppender:
    """with 文で開閉する追記専用 writer。各 append で flush + fsync。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = None

    def __enter__(self) -> "CheckpointAppender":
        self._f = self.path.open("a", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def append(self, key: str, value: Any, status: str = "ok") -> None:
        assert self._f is not None
        row = {
            "key": key,
            "value": value,
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._f.flush()
        try:
            import os
            os.fsync(self._f.fileno())
        except OSError:
            pass
