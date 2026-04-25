"""Linear projection of weekly usage to reset time.

Two strategies, hybrid selection:
- recent: rate from first vs last reading. Good once readings span ≥12h.
- anchored: rate from week-start (reset - 168h, 0%) to last reading. Good always;
  uses the full week's real usage history, not just the sample window.

We use the MAX of the two projections (whichever predicts faster burn), so the
alert is pessimistic.
"""
from datetime import datetime, timedelta
from typing import Sequence

WEEK_HOURS = 168
SESSION_HOURS = 5


def _recent_rate(readings: Sequence[dict]) -> tuple[float, float] | None:
    """Return (rate_pct_per_h, span_h) from first-vs-last, or None if too short."""
    if len(readings) < 2:
        return None
    t1 = datetime.fromisoformat(readings[0]["captured_at"])
    t2 = datetime.fromisoformat(readings[-1]["captured_at"])
    span_h = (t2 - t1).total_seconds() / 3600
    if span_h < 0.5:  # <30 min → too noisy with 1% granularity
        return None
    p1 = readings[0]["week_all"]["pct"]
    p2 = readings[-1]["week_all"]["pct"]
    return (p2 - p1) / span_h, span_h


def _anchored_rate(reading: dict, now: datetime) -> float:
    """Rate = current_pct / hours_since_week_start. Week starts at reset - 168h."""
    reset = datetime.fromisoformat(reading["week_all"]["reset_at"])
    week_start = reset - timedelta(hours=WEEK_HOURS)
    t = datetime.fromisoformat(reading["captured_at"])
    elapsed_h = (t - week_start).total_seconds() / 3600
    if elapsed_h <= 0:
        return 0.0
    return reading["week_all"]["pct"] / elapsed_h


def project_final_pct(readings: Sequence[dict], now: datetime) -> float:
    """Project week_all.pct at reset using anchored rate only.

    Anchored = current_pct / hours_elapsed_since_week_start. This uses the full
    week's real burn as the baseline — stable, doesn't panic from short bursts.
    Recent rate is still computed in rate_breakdown() for display, but never
    drives the projection or alert.
    """
    if not readings:
        raise ValueError("need at least 1 reading")
    last = readings[-1]
    reset = datetime.fromisoformat(last["week_all"]["reset_at"])
    t_last = datetime.fromisoformat(last["captured_at"])
    hours_remaining = (reset - t_last).total_seconds() / 3600
    return last["week_all"]["pct"] + _anchored_rate(last, now) * hours_remaining


def rate_breakdown(readings: Sequence[dict], now: datetime) -> dict:
    """Expose both rates for UI display."""
    last = readings[-1]
    anchored = _anchored_rate(last, now)
    recent = _recent_rate(readings)
    return {
        "anchored_rate_pct_per_h": anchored,
        "recent_rate_pct_per_h": recent[0] if recent else None,
        "recent_span_h": recent[1] if recent else None,
    }


def should_alert(projected_pct: float, current_pct: float, threshold: float = 90) -> bool:
    """Alert if either the projection OR the current value crosses threshold."""
    return projected_pct >= threshold or current_pct >= threshold


def project_session_final_pct(reading: dict, now: datetime) -> float | None:
    """Project the 5-hour session % at block end using anchored rate.

    Mirrors project_final_pct but for the session block. The block runs from
    (reset_at - 5h) to reset_at; elapsed time is measured from block start.
    Returns None if the reading lacks usable session data.
    """
    sess = reading.get("session")
    if not isinstance(sess, dict):
        return None
    if sess.get("pct") is None or sess.get("reset_at") is None:
        return None
    try:
        reset = datetime.fromisoformat(sess["reset_at"])
    except (TypeError, ValueError):
        return None
    block_start = reset - timedelta(hours=SESSION_HOURS)
    elapsed_h = (now - block_start).total_seconds() / 3600
    if elapsed_h <= 0:
        return float(sess["pct"])
    rate = sess["pct"] / elapsed_h
    hours_remaining = (reset - now).total_seconds() / 3600
    if hours_remaining <= 0:
        return float(sess["pct"])
    return float(sess["pct"]) + rate * hours_remaining
