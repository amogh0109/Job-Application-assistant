"""
Structured logger (JSONL) for application runs.
Creates a timestamped file per run and resets the latest log at start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from datetime import datetime, timezone


class RunLogger:
    def __init__(self, log_dir: str, reset_latest: bool = True):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_file = self.log_dir / f"run-{run_id}.jsonl"
        self.latest_file = self.log_dir / "run-latest.jsonl"

        if reset_latest:
            self.latest_file.write_text("", encoding="utf-8")

    def log(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        for path in (self.run_file, self.latest_file):
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + line + "\n")
