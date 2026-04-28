"""Command-line interface for cc-usage-tray.

Mirrors the tray menu so the same controls work over SSH / in scripts.

  usage-monitor-cli status              # print current status + pause state
  usage-monitor-cli strategy list       # list available + show active
  usage-monitor-cli strategy <name>     # switch alert strategy
  usage-monitor-cli pause weekly        # pause until weekly reset
  usage-monitor-cli pause session       # pause until 5h session reset
  usage-monitor-cli pause 90m           # pause for 90 minutes (m/h/d suffix)
  usage-monitor-cli pause --until <iso> # pause until explicit ISO datetime
  usage-monitor-cli resume              # clear any pause
  usage-monitor-cli reset-history       # wipe weekly_history.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from usage_monitor import pause_state
from usage_monitor.config import CONFIG_PATH, HISTORY_PATH, READINGS_PATH, load_config
from usage_monitor.projector import STRATEGIES, auto_active_mask

STATUS_FILE = Path.home() / ".claude" / "usage_status.txt"


def _load_last_reading() -> dict | None:
    p = Path.home() / ".claude" / "usage_monitor" / "readings.jsonl"
    if not p.exists():
        return None
    last = None
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def cmd_status(_args) -> int:
    cfg = load_config()
    print(f"Alert strategy: {cfg.get('projection_strategy', 'anchored')}")
    if STATUS_FILE.exists():
        print(f"Status: {STATUS_FILE.read_text().strip()}")
    else:
        print("Status: (no scraper run yet)")
    p = pause_state.load()
    if p is None:
        print("Pause:  (none)")
    else:
        print(f"Pause:  {pause_state.describe(p)}")
    return 0


def cmd_strategy(args) -> int:
    name = args.value
    if name in (None, "list"):
        cfg = load_config()
        active = cfg.get("projection_strategy", "anchored")
        for s in STRATEGIES:
            marker = "*" if s == active else " "
            print(f" {marker} {s}")
        return 0
    if name not in STRATEGIES:
        print(f"unknown strategy {name!r} (choose from: {', '.join(STRATEGIES)})", file=sys.stderr)
        return 2
    cfg = load_config()
    cfg["projection_strategy"] = name
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Alert strategy → {name}")
    return 0


_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)


def _parse_duration(spec: str) -> timedelta | None:
    m = _DURATION_RE.match(spec.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def cmd_pause(args) -> int:
    now = datetime.now()
    until: datetime | None = None
    if args.until:
        try:
            until = datetime.fromisoformat(args.until)
        except ValueError:
            print(f"--until expects ISO 8601 datetime, got {args.until!r}", file=sys.stderr)
            return 2
    elif args.target == "weekly":
        last = _load_last_reading()
        if last and last.get("week_all", {}).get("reset_at"):
            until = datetime.fromisoformat(last["week_all"]["reset_at"])
        else:
            until = now + timedelta(days=7)
    elif args.target == "session":
        last = _load_last_reading()
        if last and isinstance(last.get("session"), dict) and last["session"].get("reset_at"):
            until = datetime.fromisoformat(last["session"]["reset_at"])
        else:
            until = now + timedelta(hours=5)
    else:
        delta = _parse_duration(args.target)
        if delta is None:
            print(
                f"target {args.target!r} must be 'weekly', 'session', a duration like '90m', "
                f"or pass --until ISO",
                file=sys.stderr,
            )
            return 2
        until = now + delta
    if until <= now:
        print("until must be in the future", file=sys.stderr)
        return 2
    pause_state.save(pause_state.Pause(until, pause_state.REASON_MANUAL, manual=True))
    print(f"Alerts paused until {until.isoformat(timespec='minutes')}")
    return 0


def cmd_resume(_args) -> int:
    pause_state.clear()
    print("Alerts resumed.")
    return 0


def _load_all_readings() -> list[dict]:
    if not READINGS_PATH.exists():
        return []
    out = []
    for line in READINGS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_mask(mask: set[tuple[int, int]]) -> list[str]:
    """Render mask as 'Mon 09–19, Tue 09–19, ...' compressed."""
    by_day: dict[int, set[int]] = {}
    for wd, h in mask:
        by_day.setdefault(wd, set()).add(h)
    lines = []
    for wd in range(7):
        hours = sorted(by_day.get(wd, set()))
        if not hours:
            lines.append(f"{_WEEKDAYS[wd]}: —")
            continue
        # Collapse contiguous ranges.
        ranges = []
        run_start = hours[0]
        prev = hours[0]
        for h in hours[1:]:
            if h == prev + 1:
                prev = h
                continue
            ranges.append((run_start, prev))
            run_start = h
            prev = h
        ranges.append((run_start, prev))
        rstr = ", ".join(f"{a:02d}–{b+1:02d}" for a, b in ranges)
        lines.append(f"{_WEEKDAYS[wd]}: {rstr}")
    return lines


def cmd_active_hours(args) -> int:
    cfg = load_config()
    sub = args.action or "show"
    if sub == "show":
        mode = cfg["active_hours"].get("mode", "manual")
        print(f"Mode: {mode}")
        print(f"Manual window: {cfg['active_hours']['start']:02d}:00–"
              f"{cfg['active_hours']['end']:02d}:00 "
              f"(weekdays_only={cfg['active_hours']['weekdays_only']})")
        if mode == "auto":
            readings = _load_all_readings()
            mask = auto_active_mask(readings, cfg)
            if mask is None:
                print("Auto mask: <not enough history yet — falling back to manual>")
            else:
                print(f"Auto mask ({len(mask)} hours/week):")
                for line in _format_mask(mask):
                    print(f"  {line}")
        return 0
    if sub in ("auto", "manual"):
        cfg["active_hours"]["mode"] = sub
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Active-hours mode → {sub}")
        return 0
    if sub == "set":
        if args.start is None or args.end is None:
            print("usage: active-hours set --start H --end H [--weekdays-only|--all-days]", file=sys.stderr)
            return 2
        cfg["active_hours"]["mode"] = "manual"
        cfg["active_hours"]["start"] = int(args.start)
        cfg["active_hours"]["end"] = int(args.end)
        if args.weekdays_only:
            cfg["active_hours"]["weekdays_only"] = True
        if args.all_days:
            cfg["active_hours"]["weekdays_only"] = False
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Active hours: {args.start:02d}:00–{args.end:02d}:00 "
              f"(weekdays_only={cfg['active_hours']['weekdays_only']}, mode=manual)")
        return 0
    print(f"unknown active-hours action {sub!r}", file=sys.stderr)
    return 2


def cmd_reset_history(_args) -> int:
    if HISTORY_PATH.exists():
        HISTORY_PATH.unlink()
        print(f"Removed {HISTORY_PATH}")
    else:
        print(f"No history file at {HISTORY_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="usage-monitor-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show current usage + pause state").set_defaults(func=cmd_status)

    s_strategy = sub.add_parser("strategy", help="switch or list alert strategies")
    s_strategy.add_argument("value", nargs="?", help=f"one of: {', '.join(STRATEGIES)}, or 'list'")
    s_strategy.set_defaults(func=cmd_strategy)

    s_pause = sub.add_parser("pause", help="pause alerts")
    s_pause.add_argument(
        "target",
        nargs="?",
        default="session",
        help="'weekly', 'session', or duration like '90m', '4h', '1d' (default: session)",
    )
    s_pause.add_argument("--until", help="ISO 8601 datetime to pause until")
    s_pause.set_defaults(func=cmd_pause)

    sub.add_parser("resume", help="clear any pause").set_defaults(func=cmd_resume)
    sub.add_parser("reset-history", help="wipe weekly_history.jsonl").set_defaults(func=cmd_reset_history)

    s_active = sub.add_parser("active-hours", help="show or set active-hours window")
    s_active.add_argument("action", nargs="?", default="show",
                          choices=["show", "auto", "manual", "set"])
    s_active.add_argument("--start", type=int, help="start hour (0-23) for 'set'")
    s_active.add_argument("--end", type=int, help="end hour (1-24) for 'set'")
    s_active.add_argument("--weekdays-only", action="store_true")
    s_active.add_argument("--all-days", action="store_true")
    s_active.set_defaults(func=cmd_active_hours)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
