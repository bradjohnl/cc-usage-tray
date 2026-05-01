"""Linear projection of weekly usage to reset time.

Strategies (selectable via config.projection_strategy or env USAGE_PROJECTION_STRATEGY):

- anchored      : current_pct / hours_elapsed_since_week_start * hours_remaining + current_pct
                  (default; pessimistic on early-week bursts)
- active_hours  : same formula, but only counts hours within the active_hours window
                  (default 09:00-19:00 weekdays). Damps weekend/night bursts.
- blend         : 0.3*anchored + 0.7*historical_avg_final_pct (from weekly_history.jsonl).
                  Falls back to anchored when no history.
- dow_curve     : project = historical_avg + (current_pct - expected_pct_at_this_active_hour).
                  Compares actual vs expected curve; only flags when AHEAD of curve.
                  Falls back to active_hours when no history.

Plus min_elapsed_hours gate: returns current_pct when below, suppressing early-week noise.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from usage_monitor.config import HISTORY_PATH, READINGS_PATH, load_config

WEEK_HOURS = 168
SESSION_HOURS = 5


# ---------- shared helpers ----------

def _week_start(reading: dict) -> datetime:
    reset = datetime.fromisoformat(reading["week_all"]["reset_at"])
    return reset - timedelta(hours=WEEK_HOURS)


def _elapsed_h(reading: dict) -> float:
    t = datetime.fromisoformat(reading["captured_at"])
    return (t - _week_start(reading)).total_seconds() / 3600


def _hours_remaining(reading: dict) -> float:
    reset = datetime.fromisoformat(reading["week_all"]["reset_at"])
    t = datetime.fromisoformat(reading["captured_at"])
    return (reset - t).total_seconds() / 3600


def _recent_rate(readings: Sequence[dict]) -> tuple[float, float] | None:
    if len(readings) < 2:
        return None
    t1 = datetime.fromisoformat(readings[0]["captured_at"])
    t2 = datetime.fromisoformat(readings[-1]["captured_at"])
    span_h = (t2 - t1).total_seconds() / 3600
    if span_h < 0.5:
        return None
    p1 = readings[0]["week_all"]["pct"]
    p2 = readings[-1]["week_all"]["pct"]
    return (p2 - p1) / span_h, span_h


def _anchored_rate(reading: dict) -> float:
    elapsed = _elapsed_h(reading)
    if elapsed <= 0:
        return 0.0
    return reading["week_all"]["pct"] / elapsed


# ---------- active-hours window ----------

def _manual_mask(cfg: dict) -> set[tuple[int, int]]:
    """Build (weekday, hour) bucket set from manual start/end/weekdays_only."""
    win_start = int(cfg["active_hours"]["start"])
    win_end = int(cfg["active_hours"]["end"])
    weekdays_only = bool(cfg["active_hours"].get("weekdays_only", False))
    mask: set[tuple[int, int]] = set()
    for wd in range(7):
        if weekdays_only and wd >= 5:
            continue
        for h in range(24):
            if win_start <= h < win_end:
                mask.add((wd, h))
    return mask


def auto_active_mask(
    readings: Sequence[dict],
    cfg: dict,
    *,
    now: datetime | None = None,
) -> set[tuple[int, int]] | None:
    """Derive (weekday, hour) buckets where the user historically burns weekly %.

    Algorithm: for every consecutive pair of readings within the last
    `auto_lookback_days`, if the second reading shows a positive delta in
    week_all.pct, mark the (weekday, hour) of the second reading as active for
    that ISO-week. After scanning, keep buckets active in at least
    `auto_min_active_fraction` of the distinct ISO-weeks observed.

    Returns None when there's not enough data (< 2 readings inside the lookback
    window or fewer than 2 distinct days), so callers can fall back to manual.
    """
    if not readings:
        return None
    lookback_days = int(cfg["active_hours"].get("auto_lookback_days", 28))
    threshold = float(cfg["active_hours"].get("auto_min_active_fraction", 0.25))
    cutoff = (now or datetime.now()) - timedelta(days=lookback_days)
    # Group: (weekday, hour) → set of (iso_year, iso_week) where bucket had burn
    by_bucket: dict[tuple[int, int], set[tuple[int, int]]] = {}
    weeks_seen: set[tuple[int, int]] = set()
    distinct_days: set[tuple[int, int, int]] = set()
    prev = None
    for r in readings:
        try:
            t = datetime.fromisoformat(r["captured_at"])
            pct = r["week_all"]["pct"]
        except (KeyError, TypeError, ValueError):
            prev = None
            continue
        if t < cutoff:
            prev = (t, pct)
            continue
        iso_y, iso_w, _ = t.isocalendar()
        weeks_seen.add((iso_y, iso_w))
        distinct_days.add((t.year, t.month, t.day))
        if prev is not None:
            prev_t, prev_pct = prev
            delta = pct - prev_pct
            # Skip the week-reset boundary (delta < 0) and stale gaps (>3h).
            if delta > 0 and (t - prev_t) <= timedelta(hours=3):
                bucket = (t.weekday(), t.hour)
                by_bucket.setdefault(bucket, set()).add((iso_y, iso_w))
        prev = (t, pct)
    if len(distinct_days) < 2:
        return None
    if not weeks_seen:
        return None
    n_weeks = len(weeks_seen)
    min_weeks = max(1, int(round(threshold * n_weeks)))
    mask = {b for b, ws in by_bucket.items() if len(ws) >= min_weeks}
    if not mask:
        return None
    return mask


def _load_readings_from_disk() -> list[dict]:
    if not READINGS_PATH.exists():
        return []
    out: list[dict] = []
    try:
        for line in READINGS_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _resolve_mask(cfg: dict, readings: Sequence[dict] | None) -> set[tuple[int, int]]:
    mode = cfg["active_hours"].get("mode", "manual")
    if mode != "auto":
        return _manual_mask(cfg)
    # Prefer full disk history. Callers commonly pass a 24-reading tail (perf
    # hint for `_recent_rate`); deriving the mask from that slice yields only
    # the last day's buckets, which collapses `active_remaining` to ~0 and
    # pins the projection at current_pct.
    all_readings = _load_readings_from_disk()
    auto = auto_active_mask(all_readings, cfg) if all_readings else None
    if auto is None and readings:
        auto = auto_active_mask(list(readings), cfg)
    if auto is not None:
        return auto
    return _manual_mask(cfg)


def _active_hours_between(
    start: datetime,
    end: datetime,
    mask: set[tuple[int, int]],
) -> float:
    """Count hours within the (weekday, hour) bucket mask."""
    if end <= start:
        return 0.0
    total_seconds = 0.0
    cursor = start
    while cursor < end:
        # Walk one hour at a time so we handle masks where weekend evenings are
        # active but weekend mornings aren't, etc.
        next_hour = (cursor.replace(minute=0, second=0, microsecond=0)
                     + timedelta(hours=1))
        slice_end = min(next_hour, end)
        bucket = (cursor.weekday(), cursor.hour)
        if bucket in mask:
            total_seconds += (slice_end - cursor).total_seconds()
        cursor = slice_end
    return total_seconds / 3600


def _active_projection(
    reading: dict,
    cfg: dict,
    readings: Sequence[dict] | None = None,
) -> float:
    week_start = _week_start(reading)
    now = datetime.fromisoformat(reading["captured_at"])
    reset = datetime.fromisoformat(reading["week_all"]["reset_at"])
    mask = _resolve_mask(cfg, readings)
    active_elapsed = _active_hours_between(week_start, now, mask)
    active_remaining = _active_hours_between(now, reset, mask)
    pct = reading["week_all"]["pct"]
    if active_elapsed <= 0:
        return float(pct)
    rate = pct / active_elapsed
    return float(pct) + rate * active_remaining


# ---------- historical baseline ----------

def _load_history(cfg: dict) -> list[float]:
    """Return last N completed weeks' final_pct values."""
    if not HISTORY_PATH.exists():
        return []
    n = cfg["blend"]["history_window"]
    out: list[float] = []
    try:
        for line in HISTORY_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "final_pct" in rec:
                    out.append(float(rec["final_pct"]))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except OSError:
        return []
    return out[-n:]


def _historical_avg(cfg: dict) -> float | None:
    hist = _load_history(cfg)
    if not hist:
        return None
    return sum(hist) / len(hist)


# ---------- strategies ----------

def _anchored_projection(reading: dict) -> float:
    return reading["week_all"]["pct"] + _anchored_rate(reading) * _hours_remaining(reading)


def _blend_projection(reading: dict, cfg: dict) -> float:
    anchored = _anchored_projection(reading)
    hist_avg = _historical_avg(cfg)
    if hist_avg is None:
        return anchored
    cw = cfg["blend"]["current_weight"]
    hw = cfg["blend"]["historical_weight"]
    total = cw + hw
    if total <= 0:
        return anchored
    return (cw * anchored + hw * hist_avg) / total


def _dow_curve_projection(
    reading: dict,
    cfg: dict,
    readings: Sequence[dict] | None = None,
) -> float:
    hist_avg = _historical_avg(cfg)
    if hist_avg is None:
        # No history yet — fall back to active_hours which already damps off-hours.
        return _active_projection(reading, cfg, readings)
    week_start = _week_start(reading)
    reset = datetime.fromisoformat(reading["week_all"]["reset_at"])
    now = datetime.fromisoformat(reading["captured_at"])
    mask = _resolve_mask(cfg, readings)
    active_elapsed = _active_hours_between(week_start, now, mask)
    active_total = _active_hours_between(week_start, reset, mask)
    if active_total <= 0:
        return float(reading["week_all"]["pct"])
    expected_now = hist_avg * (active_elapsed / active_total)
    deviation = reading["week_all"]["pct"] - expected_now
    return max(0.0, hist_avg + deviation)


# ---------- public API ----------

def project_final_pct(
    readings: Sequence[dict],
    now: datetime,
    *,
    strategy: str | None = None,
    config: dict | None = None,
) -> float:
    """Project week_all.pct at reset using the configured strategy.

    `now` is kept for signature stability with callers/tests but is not used
    directly — the projection anchors on the last reading's captured_at.
    """
    if not readings:
        raise ValueError("need at least 1 reading")
    cfg = config if config is not None else load_config()
    strat = strategy or cfg.get("projection_strategy", "anchored")
    last = readings[-1]

    # Min-elapsed gate: too early in the week → don't extrapolate, just report current.
    min_elapsed = float(cfg.get("min_elapsed_hours", 0))
    if min_elapsed > 0 and _elapsed_h(last) < min_elapsed:
        return float(last["week_all"]["pct"])

    if strat == "active_hours":
        return _active_projection(last, cfg, readings)
    if strat == "blend":
        return _blend_projection(last, cfg)
    if strat == "dow_curve":
        return _dow_curve_projection(last, cfg, readings)
    return _anchored_projection(last)


STRATEGIES = ("anchored", "active_hours", "blend", "dow_curve")
# Session strategies are a strict subset — blend/dow_curve need historical
# session baselines the daemon doesn't keep, so they're not exposed.
SESSION_STRATEGIES = ("anchored", "active_hours")


def project_all_strategies(
    readings: Sequence[dict],
    now: datetime,
    *,
    config: dict | None = None,
) -> dict[str, float]:
    """Return projected pct for every strategy. Used by the dashboard."""
    if not readings:
        raise ValueError("need at least 1 reading")
    cfg = config if config is not None else load_config()
    return {
        s: project_final_pct(readings, now=now, strategy=s, config=cfg)
        for s in STRATEGIES
    }


def rate_breakdown(readings: Sequence[dict], now: datetime) -> dict:
    last = readings[-1]
    anchored = _anchored_rate(last)
    recent = _recent_rate(readings)
    return {
        "anchored_rate_pct_per_h": anchored,
        "recent_rate_pct_per_h": recent[0] if recent else None,
        "recent_span_h": recent[1] if recent else None,
    }


def should_alert(projected_pct: float, current_pct: float, threshold: float = 90) -> bool:
    return projected_pct >= threshold or current_pct >= threshold


def project_session_final_pct(
    reading: dict,
    now: datetime,
    *,
    strategy: str | None = None,
    config: dict | None = None,
    readings: Sequence[dict] | None = None,
) -> float | None:
    """Project session (5h block) final pct using the configured strategy.

    Strategy resolution: explicit arg > config['session_projection_strategy'] >
    'anchored'. Only `anchored` and `active_hours` are honored; any other
    value falls back to anchored.
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
    pct = float(sess["pct"])
    if reset <= now:
        return pct

    cfg = config if config is not None else load_config()
    strat = strategy or cfg.get("session_projection_strategy", "anchored")

    if strat == "active_hours":
        mask = _resolve_mask(cfg, readings)
        active_elapsed = _active_hours_between(block_start, now, mask)
        active_remaining = _active_hours_between(now, reset, mask)
        if active_elapsed <= 0:
            return pct
        rate = pct / active_elapsed
        return pct + rate * active_remaining

    elapsed_h = (now - block_start).total_seconds() / 3600
    if elapsed_h <= 0:
        return pct
    rate = pct / elapsed_h
    hours_remaining = (reset - now).total_seconds() / 3600
    if hours_remaining <= 0:
        return pct
    return pct + rate * hours_remaining
