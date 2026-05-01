"""Usage-monitor config loader.

Reads ~/.claude/usage_monitor/config.json. Env var USAGE_PROJECTION_STRATEGY
overrides config.json. Falls back to defaults when neither is set.
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "usage_monitor" / "config.json"
HISTORY_PATH = Path.home() / ".claude" / "usage_monitor" / "weekly_history.jsonl"
READINGS_PATH = Path.home() / ".claude" / "usage_monitor" / "readings.jsonl"

DEFAULTS = {
    "projection_strategy": "anchored",  # anchored | active_hours | blend | dow_curve
    # Session (5h block) projection strategy. Independent from weekly so the
    # user can pick e.g. anchored for the long week curve but active_hours for
    # the 5h block (which damps overnight drift). Only `anchored` and
    # `active_hours` apply — blend/dow_curve need historical session baselines
    # the daemon doesn't keep, so they collapse to anchored.
    "session_projection_strategy": "anchored",  # anchored | active_hours
    "min_elapsed_hours": 0.0,           # below this, no projection (returns current_pct)
    "active_hours": {
        # mode: "manual" uses start/end/weekdays_only.
        # mode: "auto"   derives the active (weekday, hour) buckets from
        #                readings.jsonl over the last `auto_lookback_days`.
        "mode": "manual",
        # Manual fallback / starting window. Widened from 09–19 weekdays-only
        # to 08–22 every day so the manual default doesn't silently exclude
        # evenings — common for users who code after dinner.
        "start": 8,
        "end": 22,
        "weekdays_only": False,
        # Auto-mode parameters.
        "auto_lookback_days": 28,       # window of readings to learn from
        "auto_min_active_fraction": 0.25,  # (weekday, hour) counts as active
                                            # if it had positive burn in ≥this
                                            # fraction of weeks observed
    },
    "blend": {
        "current_weight": 0.3,
        "historical_weight": 0.7,
        "history_window": 4,            # last N completed weeks
    },
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["active_hours"] = dict(DEFAULTS["active_hours"])
    cfg["blend"] = dict(DEFAULTS["blend"])
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    env_strategy = os.environ.get("USAGE_PROJECTION_STRATEGY")
    if env_strategy:
        cfg["projection_strategy"] = env_strategy
    return cfg
