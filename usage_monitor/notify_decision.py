"""Pure decision logic for usage tray notifications.

Rules:
- Escalation (rank up: safe<warn<alert): always notify.
- Warning, same zone: re-notify only when projected % increases (>= 1%).
- Alert, same zone: re-notify on any change (>= 1%, either direction)
  in current week %, projected %, or session %.
- De-escalation alert -> warn: notify.
- Recovery to safe (from warn or alert): notify.

The caller persists state between runs so tray restarts don't re-fire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SAFE = "safe"
WARN = "warn"
ALERT = "alert"
UNKNOWN = "unknown"

_RANK = {SAFE: 0, UNKNOWN: 0, WARN: 1, ALERT: 2}
_MIN_STEP = 1  # percent points


@dataclass(frozen=True)
class Notification:
    title: str
    severity: str  # "low" | "default" | "urgent"
    kind: str     # "escalation" | "warn_proj_up" | "alert_change"
                  # | "de-escalation" | "recovery"


def pcts_from_status(s: dict) -> dict:
    """Extract proj/week/session ints from a parsed status dict.

    Missing or non-int values become None so the diff logic stays simple.
    """
    sess = s.get("session_pct")
    return {
        "proj": s.get("proj_pct") if isinstance(s.get("proj_pct"), int) else None,
        "week": s.get("week_pct") if isinstance(s.get("week_pct"), int) else None,
        "session": sess if isinstance(sess, int) else None,
    }


def _changed(prev: Optional[int], curr: Optional[int]) -> bool:
    if prev is None or curr is None:
        return False
    return abs(curr - prev) >= _MIN_STEP


def decide_notification(
    prev_state: str,
    curr_state: str,
    prev_pcts: dict,
    curr_pcts: dict,
) -> Optional[Notification]:
    """Return a Notification if the tray should fire, else None."""
    if curr_state == UNKNOWN:
        return None

    prev_rank = _RANK.get(prev_state, 0)
    curr_rank = _RANK.get(curr_state, 0)

    if curr_rank > prev_rank:
        if curr_state == ALERT:
            return Notification(
                "Claude usage ALERT \U0001f6a8", "urgent", "escalation"
            )
        if curr_state == WARN:
            return Notification(
                "Claude usage warning \u26a0\ufe0f", "default", "escalation"
            )

    if curr_rank < prev_rank:
        if prev_state == ALERT and curr_state == WARN:
            return Notification(
                "Claude usage de-escalated to warning \u26a0\ufe0f",
                "default",
                "de-escalation",
            )
        if curr_state == SAFE:
            return Notification(
                "Claude usage recovered \u2713", "low", "recovery"
            )

    if curr_state == WARN and prev_state == WARN:
        prev_proj = prev_pcts.get("proj")
        curr_proj = curr_pcts.get("proj")
        if (
            prev_proj is not None
            and curr_proj is not None
            and curr_proj - prev_proj >= _MIN_STEP
        ):
            return Notification(
                "Claude usage warning \u26a0\ufe0f (proj rising)",
                "default",
                "warn_proj_up",
            )

    if curr_state == ALERT and prev_state == ALERT:
        for k in ("proj", "week", "session"):
            if _changed(prev_pcts.get(k), curr_pcts.get(k)):
                return Notification(
                    "Claude usage ALERT \U0001f6a8 (update)",
                    "urgent",
                    "alert_change",
                )

    return None
