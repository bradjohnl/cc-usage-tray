#!/usr/bin/env python3
"""Entry point: read rate-limit cache + ccusage, project, alert if >90%.

Public version: data flows from
  (1) Claude Code's statusLine hook → ~/.claude/usage_monitor/rate_limits_cache.json
  (2) ccusage CLI → active 5h block tokens/cost/burn rate

No TUI scraping in this version.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from usage_monitor.ccusage_adapter import fetch_active_block
from usage_monitor.config import HISTORY_PATH
from usage_monitor.dashboard import render_dashboard
from usage_monitor import pause_state
from usage_monitor.projector import (
    project_final_pct,
    project_session_final_pct,
    rate_breakdown,
    should_alert,
)
from usage_monitor.rate_limits_cache import read_cache as read_rate_limits_cache
from usage_monitor.thresholds import ALERT_PCT

STATE_DIR = Path.home() / ".claude" / "usage_monitor"
STATUS_FILE = Path.home() / ".claude" / "usage_status.txt"
DASHBOARD_FILE = STATE_DIR / "dashboard.html"
THRESHOLD_PCT = ALERT_PCT
MAX_READINGS = 672  # 14 days * 48 readings/day @ 30min interval
ALERT_DEDUP_WINDOW_H = 4  # don't re-alert more often than this
RATE_LIMITS_CACHE_MAX_AGE_S = 600  # accept rate-limit cache up to 10 min old


def _load_readings(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _save_readings(path: Path, readings: list[dict]) -> None:
    trimmed = readings[-MAX_READINGS:]
    path.write_text("\n".join(json.dumps(r, default=str) for r in trimmed) + "\n")


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _notify(title: str, body: str) -> None:
    """Desktop notification + optional ntfy push.

    Both NTFY env vars must be set to enable push. No defaults on purpose —
    the public version doesn't bake in any ntfy server.
    """
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-i", "dialog-warning", title, body],
            check=False, timeout=5,
        )
    except Exception:
        pass

    ntfy_url = os.environ.get("CLAUDE_USAGE_NTFY_URL")
    ntfy_topic = os.environ.get("CLAUDE_USAGE_NTFY_TOPIC")
    if not ntfy_url or not ntfy_topic:
        return
    try:
        subprocess.Popen(
            ["curl", "-s", "--max-time", "5", "-X", "POST",
             f"{ntfy_url.rstrip('/')}/{ntfy_topic}",
             "-H", f"Title: {title}",
             "-H", "Priority: urgent",
             "-d", body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _write_status(projected: float, current: int, reset_at: datetime, alerting: bool,
                  rate_per_h: float = 0.0, session_pct: int | None = None,
                  session_tokens: int | None = None, session_cost_usd: float | None = None,
                  session_ends: datetime | None = None,
                  session_proj_pct: float | None = None,
                  last_fresh_at: datetime | None = None) -> None:
    """Single-line status, overwritten each run (never appended)."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    marker = "🚨" if alerting else "📊"
    verdict = "ON TRACK TO HIT LIMIT" if alerting else "safe"
    if session_pct is not None:
        end_str = session_ends.astimezone().strftime("%H:%M") if session_ends else None
        end_part = f" ends {end_str}" if end_str else ""
        proj_part = f" → proj {int(round(session_proj_pct))}%" if session_proj_pct is not None else ""
        session_str = f"sess {session_pct}%{proj_part}{end_part}"
    elif session_tokens is not None:
        end_str = session_ends.astimezone().strftime("%H:%M") if session_ends else "?"
        cost_str = f" · ${session_cost_usd:.0f}" if session_cost_usd is not None else ""
        session_str = f"sess {_format_tokens(session_tokens)} tok{cost_str} ends {end_str}"
    else:
        session_str = "session n/a"
    last_str = ""
    if last_fresh_at is not None:
        last_str = f" | last {last_fresh_at.astimezone().strftime('%H:%M')}"
    STATUS_FILE.write_text(
        f"{marker} Claude: week {current}% (+{rate_per_h:.2f}%/h) → proj {projected:.0f}% "
        f"by {reset_at.strftime('%a %H:%M')} | {session_str} | {verdict}{last_str}\n"
    )


def record_completed_weeks(readings: list[dict]) -> int:
    """Append one entry per completed week to weekly_history.jsonl.

    A completed week is identified by a `reset_at` value that no longer
    appears in the most recent reading (the active week). For each prior
    `reset_at`, we record the maximum `week_all.pct` ever observed for
    that reset window. Idempotent: only weeks not already in history get
    appended.

    Returns the number of new entries written.
    """
    if not readings:
        return 0
    active_reset = readings[-1]["week_all"].get("reset_at")
    by_reset: dict[str, int] = {}
    for r in readings:
        try:
            rst = r["week_all"]["reset_at"]
            pct = int(r["week_all"]["pct"])
        except (KeyError, TypeError, ValueError):
            continue
        if rst == active_reset:
            continue  # current week, not yet completed
        if pct > by_reset.get(rst, -1):
            by_reset[rst] = pct
    if not by_reset:
        return 0
    existing: set[str] = set()
    if HISTORY_PATH.exists():
        try:
            for line in HISTORY_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if "reset_at" in rec:
                        existing.add(rec["reset_at"])
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_count = 0
    with HISTORY_PATH.open("a") as fh:
        # Sort by reset_at so the file stays time-ordered.
        for rst in sorted(by_reset):
            if rst in existing:
                continue
            fh.write(json.dumps({"reset_at": rst, "final_pct": by_reset[rst]}) + "\n")
            new_count += 1
    return new_count


def refresh_outputs(readings: list[dict]) -> None:
    """Update status_file + dashboard.html from existing readings, in-process.

    Used by the tray's control endpoints to keep the user-facing surfaces in
    sync with config changes WITHOUT going through systemd or ccusage. Skips
    ntfy / desktop notifications and any subprocess work.
    """
    if not readings:
        return
    reading = readings[-1]
    now = datetime.now()
    projected = project_final_pct(readings[-24:], now=now)
    week_alerting = should_alert(projected, reading["week_all"]["pct"], THRESHOLD_PCT)
    session_pct_now = (
        reading["session"]["pct"]
        if isinstance(reading.get("session"), dict)
        and reading["session"].get("pct") is not None
        else None
    )
    session_alerting = (
        session_pct_now is not None and session_pct_now >= THRESHOLD_PCT
    )
    paused = pause_state.load(now=now)
    alerting = (week_alerting or session_alerting) and paused is None
    rb = rate_breakdown(readings[-24:], now=now)
    rate = (
        rb["recent_rate_pct_per_h"]
        if rb["recent_rate_pct_per_h"] is not None
        else rb["anchored_rate_pct_per_h"]
    )
    reset_dt = datetime.fromisoformat(reading["week_all"]["reset_at"])
    session_proj_pct = project_session_final_pct(reading, now=now)
    session_ends = (
        datetime.fromisoformat(reading["session"]["reset_at"])
        if isinstance(reading.get("session"), dict)
        and reading["session"].get("reset_at")
        else None
    )
    last_fresh_at = datetime.fromisoformat(reading["captured_at"])
    _write_status(
        projected, reading["week_all"]["pct"], reset_dt, alerting,
        rate_per_h=rate or 0.0, session_pct=session_pct_now,
        session_tokens=None, session_cost_usd=None,
        session_ends=session_ends,
        session_proj_pct=session_proj_pct,
        last_fresh_at=last_fresh_at,
    )
    try:
        DASHBOARD_FILE.write_text(render_dashboard(readings, now=now))
    except Exception as e:
        print(f"[usage_monitor] dashboard render failed: {e}", file=sys.stderr)


def _alert_recently_sent(readings: list[dict]) -> bool:
    now = datetime.now()
    cutoff = now - timedelta(hours=ALERT_DEDUP_WINDOW_H)
    for r in reversed(readings):
        if not r.get("alerted"):
            continue
        try:
            if datetime.fromisoformat(r["captured_at"]) >= cutoff:
                return True
        except ValueError:
            continue
    return False


def _build_reading_from_cache(rate_cache, now: datetime) -> dict | None:
    """Build a reading dict from the rate-limit cache snapshot."""
    if rate_cache is None:
        return None
    if rate_cache.seven_day_pct is None or rate_cache.seven_day_reset_at is None:
        return None
    reading = {
        "captured_at": now.isoformat(timespec="seconds"),
        "week_all": {
            "pct": int(round(rate_cache.seven_day_pct)),
            "reset_at": rate_cache.seven_day_reset_at.astimezone().replace(tzinfo=None).isoformat(),
        },
        "week_sonnet": None,  # not exposed in statusLine JSON
    }
    if rate_cache.five_hour_pct is not None and rate_cache.five_hour_reset_at is not None:
        reading["session"] = {
            "pct": int(round(rate_cache.five_hour_pct)),
            "reset_at": rate_cache.five_hour_reset_at.astimezone().replace(tzinfo=None).isoformat(),
        }
    else:
        reading["session"] = None
    return reading


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "readings.jsonl"
    readings = _load_readings(log_path)

    rate_cache = read_rate_limits_cache()
    if rate_cache is not None:
        age_s = rate_cache.age_seconds()
        if age_s > RATE_LIMITS_CACHE_MAX_AGE_S:
            print(
                f"[usage_monitor] rate cache stale ({age_s:.0f}s, threshold "
                f"{RATE_LIMITS_CACHE_MAX_AGE_S}s) — keeping last reading",
                file=sys.stderr,
            )
            rate_cache = None

    now = datetime.now()
    reading = _build_reading_from_cache(rate_cache, now)
    if reading is None:
        if not readings:
            print(
                "[usage_monitor] no rate-limit cache yet (statusLine wrapper "
                "not configured?) — see README.md",
                file=sys.stderr,
            )
            return 2
        reading = readings[-1]
        fresh = False
    else:
        readings.append(reading)
        fresh = True

    projected = project_final_pct(readings[-24:], now=now)
    week_alerting = should_alert(projected, reading["week_all"]["pct"], THRESHOLD_PCT)

    session_pct_now = (
        reading["session"]["pct"]
        if isinstance(reading.get("session"), dict) and reading["session"].get("pct") is not None
        else None
    )
    session_alerting = (
        session_pct_now is not None and session_pct_now >= THRESHOLD_PCT
    )
    alerting = week_alerting or session_alerting

    # Auto-pause if a hard limit is hit; pause persists until that reset.
    week_reset_dt = datetime.fromisoformat(reading["week_all"]["reset_at"])
    session_reset_dt = (
        datetime.fromisoformat(reading["session"]["reset_at"])
        if isinstance(reading.get("session"), dict) and reading["session"].get("reset_at")
        else None
    )
    pause_state.auto_pause_for_limit(
        week_pct=reading["week_all"]["pct"],
        session_pct=session_pct_now,
        week_reset_at=week_reset_dt,
        session_reset_at=session_reset_dt,
        now=now,
    )
    paused = pause_state.load(now=now)

    if alerting and paused is None and not _alert_recently_sent(readings):
        reading["alerted"] = True
        if session_alerting and not week_alerting:
            title = "Claude 5-hour-session limit risk"
            msg = (
                f"Session now {session_pct_now}% of 5h block. "
                f"Resets at {reading['session']['reset_at']}. Throttle now."
            )
        elif week_alerting and not session_alerting:
            title = "Claude weekly-limit risk"
            msg = (
                f"Week now {reading['week_all']['pct']}%, projected {projected:.0f}% "
                f"by {reading['week_all']['reset_at']}. Throttle now."
            )
        else:
            title = "Claude usage ALERT (week + session)"
            msg = (
                f"Week {reading['week_all']['pct']}% (proj {projected:.0f}%); "
                f"5h session {session_pct_now}%. Throttle now."
            )
        _notify(title, msg)

    if fresh:
        _save_readings(log_path, readings)
        try:
            n = record_completed_weeks(readings)
            if n:
                print(f"[usage_monitor] recorded {n} completed week(s) to {HISTORY_PATH.name}", file=sys.stderr)
        except Exception as e:
            print(f"[usage_monitor] history recording failed: {e}", file=sys.stderr)

    reset_dt = datetime.fromisoformat(reading["week_all"]["reset_at"])
    rb = rate_breakdown(readings[-24:], now=now)
    rate = (
        rb["recent_rate_pct_per_h"]
        if rb["recent_rate_pct_per_h"] is not None
        else rb["anchored_rate_pct_per_h"]
    )

    # ccusage enrichment for the active 5h block (tokens, cost, burn rate).
    active_block = None
    try:
        active_block = fetch_active_block()
    except Exception as e:
        print(f"[usage_monitor] ccusage fetch failed (non-fatal): {e}", file=sys.stderr)

    session_tokens = active_block.total_tokens if active_block else None
    session_cost = active_block.cost_usd if active_block else None
    session_ends = active_block.end if active_block else None

    session_proj_pct = project_session_final_pct(reading, now=now)
    last_fresh_at = datetime.fromisoformat(reading["captured_at"])

    _write_status(
        projected, reading["week_all"]["pct"], reset_dt, alerting,
        rate_per_h=rate, session_pct=session_pct_now,
        session_tokens=session_tokens, session_cost_usd=session_cost,
        session_ends=session_ends,
        session_proj_pct=session_proj_pct,
        last_fresh_at=last_fresh_at,
    )

    try:
        DASHBOARD_FILE.write_text(render_dashboard(readings, now=now))
    except Exception as e:
        print(f"[usage_monitor] dashboard render failed: {e}", file=sys.stderr)

    session_str = (
        f"{session_pct_now}%" if session_pct_now is not None else "n/a"
    )
    print(
        f"week_all={reading['week_all']['pct']}% session={session_str} "
        f"projected={projected:.1f}% alert={alerting}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
