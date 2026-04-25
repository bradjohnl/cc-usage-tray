"""Read rate-limit percentages cached by the Claude Code statusLine wrapper.

The wrapper at ~/.claude/statusline-rate-capture.sh writes the same numbers
that `/usage` shows, harvested from the JSON Claude Code passes to the
statusLine hook. Schema: https://code.claude.com/docs/en/statusline

Cache is fresh whenever any Claude Code session runs anything. Stale =
no Claude Code activity in a while.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CACHE_FILE = Path.home() / ".claude" / "usage_monitor" / "rate_limits_cache.json"


@dataclass
class RateLimitsSnapshot:
    captured_at: datetime
    five_hour_pct: Optional[float]
    five_hour_reset_at: Optional[datetime]
    seven_day_pct: Optional[float]
    seven_day_reset_at: Optional[datetime]

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(tz=timezone.utc)
        if self.captured_at.tzinfo is None:
            captured = self.captured_at.replace(tzinfo=timezone.utc)
        else:
            captured = self.captured_at
        return (now - captured).total_seconds()


def read_cache(path: Path = CACHE_FILE) -> Optional[RateLimitsSnapshot]:
    """Return the latest cached rate-limit snapshot, or None if unavailable."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    captured_raw = data.get("captured_at")
    if captured_raw is None:
        return None
    # captured_at written by jq's `now` is a Unix epoch seconds float
    try:
        captured_at = datetime.fromtimestamp(float(captured_raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None

    rate = data.get("rate_limits") or {}
    five = rate.get("five_hour") or {}
    seven = rate.get("seven_day") or {}

    def _to_dt(epoch) -> Optional[datetime]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    return RateLimitsSnapshot(
        captured_at=captured_at,
        five_hour_pct=five.get("used_percentage"),
        five_hour_reset_at=_to_dt(five.get("resets_at")),
        seven_day_pct=seven.get("used_percentage"),
        seven_day_reset_at=_to_dt(seven.get("resets_at")),
    )
