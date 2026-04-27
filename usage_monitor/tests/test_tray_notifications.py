"""Tests for tray threshold escalation detection and ntfy sending."""
from usage_monitor.notify_decision import (
    ALERT,
    SAFE,
    UNKNOWN,
    WARN,
    Notification,
    decide_notification,
    pcts_from_status,
)


def _p(proj=None, week=None, session=None):
    return {"proj": proj, "week": week, "session": session}


# --- pcts_from_status ---------------------------------------------------------


def test_pcts_from_status_extracts_three_keys():
    s = {"proj_pct": 75, "week_pct": 63, "session_pct": 44}
    assert pcts_from_status(s) == {"proj": 75, "week": 63, "session": 44}


def test_pcts_from_status_handles_missing_session():
    s = {"proj_pct": 50, "week_pct": 30, "session_pct": None}
    assert pcts_from_status(s) == {"proj": 50, "week": 30, "session": None}


# --- escalation ---------------------------------------------------------------


def test_safe_to_warn_fires_warning_escalation():
    n = decide_notification(SAFE, WARN, _p(50, 40), _p(75, 63))
    assert n is not None
    assert n.kind == "escalation"
    assert n.severity == "default"


def test_safe_to_alert_fires_critical_escalation():
    n = decide_notification(SAFE, ALERT, _p(50, 40), _p(95, 92))
    assert n is not None
    assert n.kind == "escalation"
    assert n.severity == "urgent"


def test_warn_to_alert_fires_critical_escalation():
    n = decide_notification(WARN, ALERT, _p(75, 63), _p(95, 92))
    assert n is not None
    assert n.kind == "escalation"
    assert n.severity == "urgent"


# --- warn re-notify on proj increase -----------------------------------------


def test_warn_same_zone_proj_unchanged_no_notify():
    assert (
        decide_notification(WARN, WARN, _p(75, 63), _p(75, 63)) is None
    )


def test_warn_same_zone_proj_increased_notifies():
    n = decide_notification(WARN, WARN, _p(75, 63), _p(76, 63))
    assert n is not None
    assert n.kind == "warn_proj_up"


def test_warn_same_zone_proj_decreased_no_notify():
    assert (
        decide_notification(WARN, WARN, _p(75, 63), _p(74, 63)) is None
    )


def test_warn_same_zone_only_week_increased_no_notify():
    assert (
        decide_notification(WARN, WARN, _p(75, 63), _p(75, 70)) is None
    )


# --- alert re-notify on any change -------------------------------------------


def test_alert_same_zone_unchanged_no_notify():
    assert (
        decide_notification(ALERT, ALERT, _p(95, 92, 80), _p(95, 92, 80))
        is None
    )


def test_alert_same_zone_proj_increased_notifies():
    n = decide_notification(ALERT, ALERT, _p(95, 92), _p(96, 92))
    assert n is not None
    assert n.kind == "alert_change"
    assert n.severity == "urgent"


def test_alert_same_zone_proj_decreased_by_1pct_notifies():
    n = decide_notification(ALERT, ALERT, _p(95, 92), _p(94, 92))
    assert n is not None
    assert n.kind == "alert_change"


def test_alert_same_zone_week_changed_notifies():
    n = decide_notification(ALERT, ALERT, _p(95, 92), _p(95, 93))
    assert n is not None
    assert n.kind == "alert_change"


def test_alert_same_zone_session_changed_notifies():
    n = decide_notification(
        ALERT, ALERT, _p(95, 92, 80), _p(95, 92, 81)
    )
    assert n is not None
    assert n.kind == "alert_change"


# --- de-escalation and recovery ----------------------------------------------


def test_alert_to_warn_notifies_de_escalation():
    n = decide_notification(ALERT, WARN, _p(95, 92), _p(85, 80))
    assert n is not None
    assert n.kind == "de-escalation"


def test_alert_to_safe_notifies_recovery():
    n = decide_notification(ALERT, SAFE, _p(95, 92), _p(50, 40))
    assert n is not None
    assert n.kind == "recovery"


def test_warn_to_safe_notifies_recovery():
    n = decide_notification(WARN, SAFE, _p(75, 63), _p(50, 40))
    assert n is not None
    assert n.kind == "recovery"


# --- unknown gating ----------------------------------------------------------


def test_unknown_curr_state_never_fires():
    assert decide_notification(WARN, UNKNOWN, _p(75), _p()) is None
    assert decide_notification(ALERT, UNKNOWN, _p(95), _p()) is None
