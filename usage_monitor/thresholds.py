"""Warn/alert threshold configuration.

Reads from environment variables with sane defaults so users can override
without patching code. Set in a systemd unit drop-in or shell profile and
every surface (tray, daemon, dashboard) picks them up.

Weekly limits:
    CC_USAGE_WARN_PCT          default 70.0   amber zone lower bound
    CC_USAGE_ALERT_PCT         default 90.0   red zone lower bound

5-hour session limits (independent from weekly):
    CC_USAGE_SESSION_WARN_PCT  default = CC_USAGE_WARN_PCT  (70.0)
    CC_USAGE_SESSION_ALERT_PCT default = CC_USAGE_ALERT_PCT (90.0)

Invalid values (non-numeric, or warn >= alert) fall back to defaults.
"""
from __future__ import annotations

import os
import sys

DEFAULT_WARN_PCT = 70.0
DEFAULT_ALERT_PCT = 90.0


def _read_pct(env: str, default: float) -> float:
    raw = os.environ.get(env)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"[cc-usage-tray] invalid {env}={raw!r}, using default {default}",
            file=sys.stderr,
        )
        return default


def load() -> tuple[float, float]:
    """Read (warn, alert) pcts from env; fall back to defaults if invalid."""
    warn = _read_pct("CC_USAGE_WARN_PCT", DEFAULT_WARN_PCT)
    alert = _read_pct("CC_USAGE_ALERT_PCT", DEFAULT_ALERT_PCT)
    if warn >= alert:
        print(
            f"[cc-usage-tray] CC_USAGE_WARN_PCT ({warn}) must be < "
            f"CC_USAGE_ALERT_PCT ({alert}); using defaults "
            f"{DEFAULT_WARN_PCT}/{DEFAULT_ALERT_PCT}",
            file=sys.stderr,
        )
        return DEFAULT_WARN_PCT, DEFAULT_ALERT_PCT
    return warn, alert


WARN_PCT, ALERT_PCT = load()


def load_session() -> tuple[float, float]:
    """Read session-specific (warn, alert) pcts from env; fall back to weekly defaults."""
    warn = _read_pct("CC_USAGE_SESSION_WARN_PCT", WARN_PCT)
    alert = _read_pct("CC_USAGE_SESSION_ALERT_PCT", ALERT_PCT)
    if warn >= alert:
        print(
            f"[cc-usage-tray] CC_USAGE_SESSION_WARN_PCT ({warn}) must be < "
            f"CC_USAGE_SESSION_ALERT_PCT ({alert}); using weekly defaults "
            f"{WARN_PCT}/{ALERT_PCT}",
            file=sys.stderr,
        )
        return WARN_PCT, ALERT_PCT
    return warn, alert


SESSION_WARN_PCT, SESSION_ALERT_PCT = load_session()


def classify(pct: float, warn: float | None = None, alert: float | None = None) -> str:
    """Return 'safe' | 'warn' | 'alert' for a percentage value."""
    w = WARN_PCT if warn is None else warn
    a = ALERT_PCT if alert is None else alert
    if pct >= a:
        return "alert"
    if pct >= w:
        return "warn"
    return "safe"


def classify_session(pct: float) -> str:
    """Return 'safe' | 'warn' | 'alert' using the session-specific thresholds."""
    return classify(pct, warn=SESSION_WARN_PCT, alert=SESSION_ALERT_PCT)
