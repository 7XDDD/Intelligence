"""本地归档：排名快照、事件 JSON、Markdown 日报、运行日志。

目录结构：
data/
├── cache/                  # 可重新拉取的缓存（如 CIK 映射）
├── rankings/YYYY-MM-DD.json
├── events/YYYY-MM-DD.json
├── reports/YYYY/MM/YYYY-MM-DD.md
└── runs/YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, data_dir: Path, cache_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        for sub in ("rankings", "events", "reports", "runs"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 排名快照 ----------
    def ranking_path(self, day: date) -> Path:
        return self.data_dir / "rankings" / f"{day.isoformat()}.json"

    def save_ranking(self, day: date, payload: dict[str, Any]) -> Path:
        path = self.ranking_path(day)
        _write_json_atomic(path, payload)
        return path

    def load_ranking(self, day: date) -> dict[str, Any] | None:
        path = self.ranking_path(day)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def previous_ranking(self, day: date) -> dict[str, Any] | None:
        """取严格早于 day 的最近一份排名快照。"""
        files = sorted(p for p in (self.data_dir / "rankings").glob("*.json") if p.stem < day.isoformat())
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def last_success_run_time(self) -> datetime | None:
        """用于“上次成功运行之后”的新闻窗口。"""
        runs_dir = self.data_dir / "runs"
        files = sorted(runs_dir.glob("*.jsonl"))
        for path in reversed(files):
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("status") == "ok":
                    return datetime.fromisoformat(entry["finished_at"])
        return None

    # ---------- 事件 ----------
    def save_events(self, day: date, payload: dict[str, Any]) -> Path:
        path = self.data_dir / "events" / f"{day.isoformat()}.json"
        _write_json_atomic(path, payload)
        return path

    # ---------- 日报 ----------
    def save_report(self, day: date, markdown: str) -> Path:
        path = self.data_dir / "reports" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path

    def latest_report_dir(self) -> Path | None:
        reports = sorted((self.data_dir / "reports").rglob("*.md"))
        return reports[-1] if reports else None

    # ---------- 运行日志 ----------
    def append_run(self, entry: dict[str, Any]) -> Path:
        day = datetime.fromisoformat(entry["finished_at"]).date()
        path = self.data_dir / "runs" / f"{day.isoformat()}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    # ---------- 缓存 ----------
    def cache_file(self, name: str) -> Path:
        return self.cache_dir / name


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
