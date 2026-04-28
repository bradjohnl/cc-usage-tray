"""Auto-pause logic for usage alerts.

When the user hits the 5-hour or weekly limit, alerts auto-pause until that
limit's reset. Manual pause is also supported (CLI / tray menu). Pause expires
automatically when wall-clock passes `paused_until`.

State file: ~/.claude/usage_monitor/pause_state.json
Schema: {"paused_until": "<iso8601>", "reason": "<text>", "manual": <bool>}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

PAUSE_FILE = Path.home() / ".claude" / "usage_monitor" / "pause_state.json"

REASON_WEEKLY = "weekly_limit"
REASON_SESSION = "session_limit"
REASON_MANUAL = "manual"


@dataclass
class Pause:
    paused_until: datetime
    reason: str
    manual: bool

    def to_json(self) -> dict:
        return {
            "paused_until": self.paused_until.isoformat(),
            "reason": self.reason,
            "manual": self.manual,
        }

    @staticmethod
    def from_json(data: dict) -> Optional["Pause"]:
        try:
            return Pause(
                paused_until=datetime.fromisoformat(data["paused_until"]),
                reason=data.get("reason", ""),
                manual=bool(data.get("manual", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def load(now: datetime | None = None) -> Optional[Pause]:
    """Return active pause, or None if no pause / pause expired.

    Auto-clears the file when expired so callers don't need to.
    """
    if not PAUSE_FILE.exists():
        return None
    try:
        data = json.loads(PAUSE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    p = Pause.from_json(data)
    if p is None:
        return None
    cur = now or datetime.now()
    if cur >= p.paused_until:
        clear()
        return None
    return p


def save(pause: Pause) -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text(json.dumps(pause.to_json(), separators=(",", ":")))


def clear() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def auto_pause_for_limit(
    week_pct: int | None,
    session_pct: int | None,
    week_reset_at: datetime | None,
    session_reset_at: datetime | None,
    now: datetime | None = None,
) -> Optional[Pause]:
    """If a hard limit is hit (≥100%), set an auto-pause until that reset.

    Weekly limit takes precedence over session because the weekly reset is
    further away — pausing until then covers the session reset too.
    Returns the pause that was set, or None if nothing changed.
    """
    cur = now or datetime.now()
    existing = load(now=cur)
    if existing is not None and existing.manual:
        return None  # don't override an explicit manual pause

    target: Optional[Pause] = None
    if week_pct is not None and week_pct >= 100 and week_reset_at is not None:
        target = Pause(week_reset_at, REASON_WEEKLY, manual=False)
    elif session_pct is not None and session_pct >= 100 and session_reset_at is not None:
        target = Pause(session_reset_at, REASON_SESSION, manual=False)
    if target is None:
        return None
    if existing is not None and existing.paused_until >= target.paused_until and existing.reason == target.reason:
        return None  # already paused at least as long
    save(target)
    return target


def is_paused(now: datetime | None = None) -> bool:
    return load(now=now) is not None


def describe(p: Pause, now: datetime | None = None) -> str:
    cur = now or datetime.now()
    delta = p.paused_until - cur
    hours = max(0, int(delta.total_seconds() // 3600))
    minutes = max(0, int((delta.total_seconds() % 3600) // 60))
    label = {
        REASON_WEEKLY: "weekly limit hit",
        REASON_SESSION: "5h session limit hit",
        REASON_MANUAL: "manual pause",
    }.get(p.reason, p.reason)
    return f"alerts paused ({label}) until {p.paused_until:%a %H:%M} · {hours}h{minutes:02d}m left"
