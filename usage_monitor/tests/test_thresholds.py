"""Tests for usage_monitor.thresholds."""
from __future__ import annotations

import os
from unittest import mock

import pytest

from usage_monitor import thresholds


def _env(**overrides):
    """Context manager that scrubs CC_USAGE_* and applies overrides."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("CC_USAGE_")}
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


def test_defaults():
    with _env():
        warn, alert = thresholds.load()
    assert warn == 70.0
    assert alert == 90.0


def test_env_override():
    with _env(CC_USAGE_WARN_PCT="60", CC_USAGE_ALERT_PCT="85"):
        warn, alert = thresholds.load()
    assert warn == 60.0
    assert alert == 85.0


def test_partial_env_override():
    with _env(CC_USAGE_ALERT_PCT="95"):
        warn, alert = thresholds.load()
    assert warn == 70.0
    assert alert == 95.0


def test_invalid_warn_falls_back():
    with _env(CC_USAGE_WARN_PCT="garbage"):
        warn, alert = thresholds.load()
    assert warn == 70.0
    assert alert == 90.0


def test_inverted_falls_back_both():
    # warn >= alert is invalid — both reset to defaults
    with _env(CC_USAGE_WARN_PCT="95", CC_USAGE_ALERT_PCT="80"):
        warn, alert = thresholds.load()
    assert (warn, alert) == (70.0, 90.0)


def test_equal_falls_back():
    with _env(CC_USAGE_WARN_PCT="80", CC_USAGE_ALERT_PCT="80"):
        warn, alert = thresholds.load()
    assert (warn, alert) == (70.0, 90.0)


@pytest.mark.parametrize(
    "pct,expected",
    [
        (0.0, "safe"),
        (69.9, "safe"),
        (70.0, "warn"),
        (70.1, "warn"),
        (89.9, "warn"),
        (90.0, "alert"),
        (90.1, "alert"),
        (100.0, "alert"),
        (150.0, "alert"),
    ],
)
def test_classify_boundaries_default(pct, expected):
    assert thresholds.classify(pct, 70.0, 90.0) == expected


def test_classify_with_custom_thresholds():
    assert thresholds.classify(55.0, 50.0, 75.0) == "warn"
    assert thresholds.classify(76.0, 50.0, 75.0) == "alert"
    assert thresholds.classify(49.0, 50.0, 75.0) == "safe"


# ── Session-specific threshold tests ──────────────────────────────────────────

def test_session_defaults_match_weekly():
    """SESSION_* defaults must equal the weekly defaults when no env override."""
    with _env():
        warn, alert = thresholds.load_session()
    assert warn == 70.0
    assert alert == 90.0


def test_session_env_override():
    with _env(CC_USAGE_SESSION_WARN_PCT="50", CC_USAGE_SESSION_ALERT_PCT="75"):
        warn, alert = thresholds.load_session()
    assert warn == 50.0
    assert alert == 75.0


def test_session_partial_override_warn_only():
    with _env(CC_USAGE_SESSION_WARN_PCT="55"):
        warn, alert = thresholds.load_session()
    assert warn == 55.0
    assert alert == 90.0


def test_session_partial_override_alert_only():
    with _env(CC_USAGE_SESSION_ALERT_PCT="80"):
        warn, alert = thresholds.load_session()
    assert warn == 70.0
    assert alert == 80.0


def test_session_invalid_falls_back():
    with _env(CC_USAGE_SESSION_WARN_PCT="not_a_number"):
        warn, alert = thresholds.load_session()
    assert warn == 70.0
    assert alert == 90.0


def test_session_inverted_falls_back():
    with _env(CC_USAGE_SESSION_WARN_PCT="90", CC_USAGE_SESSION_ALERT_PCT="60"):
        warn, alert = thresholds.load_session()
    assert (warn, alert) == (70.0, 90.0)


def test_session_independent_of_weekly():
    """Session thresholds must not change when only weekly env vars are set."""
    with _env(CC_USAGE_WARN_PCT="60", CC_USAGE_ALERT_PCT="85"):
        sess_warn, sess_alert = thresholds.load_session()
    assert sess_warn == 70.0
    assert sess_alert == 90.0


@pytest.mark.parametrize(
    "pct,expected",
    [
        (0.0, "safe"),
        (69.9, "safe"),
        (70.0, "warn"),
        (89.9, "warn"),
        (90.0, "alert"),
        (100.0, "alert"),
    ],
)
def test_classify_session_boundaries_default(pct, expected):
    with _env():
        result = thresholds.classify_session(pct)
    assert result == expected


def test_classify_session_uses_session_thresholds():
    """classify_session must use SESSION_WARN_PCT/SESSION_ALERT_PCT, not weekly vars."""
    with (mock.patch.object(thresholds, "SESSION_WARN_PCT", 50.0),
          mock.patch.object(thresholds, "SESSION_ALERT_PCT", 75.0)):
        # 60% is warn under session (>=50) but safe under default weekly (70)
        assert thresholds.classify_session(60.0) == "warn"
        # 76% is alert under session (>=75) but only warn under default weekly (90)
        assert thresholds.classify_session(76.0) == "alert"
        # 49% is safe under both
        assert thresholds.classify_session(49.0) == "safe"
