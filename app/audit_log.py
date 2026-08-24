"""
Audit trail: every single decision the agent makes gets written here, permanently.

Design choice: JSONL (one JSON object per line), not a single JSON array.
This means a crash mid-batch never corrupts previously-written records --
each line is independently valid. Appending is also cheap and safe for
concurrent/streaming use later, whereas rewriting a full JSON array on every
event would not be.
"""

import json
from pathlib import Path
from app.models import RecoveryOutcome


class AuditLogger:
    def __init__(self, log_path: str = "data/audit_log.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, outcome: RecoveryOutcome) -> None:
        with open(self.log_path, "a",encoding="utf-8") as f:
            f.write(outcome.model_dump_json() + "\n")

    def load_all(self) -> list[RecoveryOutcome]:
        """Reads every record back -- used by the dashboard to compute totals.
        Skips (and reports) any line that fails to parse, rather than crashing
        the whole dashboard over one corrupted line."""
        if not self.log_path.exists():
            return []

        outcomes = []
        corrupted_lines = 0
        with open(self.log_path,encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    outcomes.append(RecoveryOutcome.model_validate_json(line))
                except Exception:
                    corrupted_lines += 1

        if corrupted_lines:
            print(f"WARNING: {corrupted_lines} corrupted audit log line(s) skipped.")

        return outcomes

    def clear(self) -> None:
        """Only used in tests / to reset between full pipeline runs -- never
        call this if you want to preserve history across real runs."""
        if self.log_path.exists():
            self.log_path.unlink()