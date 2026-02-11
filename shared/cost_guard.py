"""The Gatekeeper — API cost control module.

Tracks daily API call count and enforces budget limits per role.
All API calls MUST go through this guard. No exceptions.

Design:
  - Persists call counts per day in /tmp so restarts don't reset.
  - Auto-resets at midnight UTC.
  - Provides remaining-budget introspection for logging.
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path


class CostGuard:
    """Tracks and limits API calls per day to control spending."""

    def __init__(self, role: str, max_daily_calls: int | None = None):
        self.role = role
        self.max_daily = max_daily_calls or int(os.getenv("MAX_DAILY_API_CALLS", 200))
        self._state_file = Path(f"/tmp/cost_guard_{role}.json")
        self._load()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self):
        today = self._today()
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                if data.get("date") == today:
                    self.count = data["count"]
                    self.date = today
                    return
            except (json.JSONDecodeError, KeyError):
                pass
        self.count = 0
        self.date = today
        self._save()

    def _save(self):
        self._state_file.write_text(json.dumps({
            "date": self.date,
            "count": self.count,
            "role": self.role,
        }))

    def can_call(self) -> bool:
        """Check if we're within budget. Auto-resets on new day."""
        today = self._today()
        if today != self.date:
            self.count = 0
            self.date = today
        return self.count < self.max_daily

    def record_call(self):
        """Record one API call."""
        today = self._today()
        if today != self.date:
            self.count = 0
            self.date = today
        self.count += 1
        self._save()

    @property
    def remaining(self) -> int:
        return max(0, self.max_daily - self.count)

    @property
    def usage_pct(self) -> float:
        return (self.count / self.max_daily) * 100 if self.max_daily > 0 else 100.0

    def __repr__(self):
        return f"CostGuard({self.role}: {self.count}/{self.max_daily} calls today, {self.remaining} remaining)"
