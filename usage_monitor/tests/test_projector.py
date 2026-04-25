"""Projection tests: given two readings, predict week-end %."""
import pytest
from datetime import datetime, timedelta
from usage_monitor.projector import project_final_pct, should_alert


def _reading(dt, week_pct, reset_at):
    return {
        "captured_at": dt.isoformat(),
        "week_all": {"pct": week_pct, "reset_at": reset_at.isoformat()},
    }


def test_project_uses_anchored_not_recent():
    """Even with a huge recent burn, projection stays anchored to week-start."""
    reset = datetime(2026, 4, 28, 15, 0)
    r1 = _reading(datetime(2026, 4, 24, 10, 0), 10, reset)
    r2 = _reading(datetime(2026, 4, 24, 12, 0), 14, reset)
    proj = project_final_pct([r1, r2], now=datetime(2026, 4, 24, 12, 0))
    # Week start Apr 21 15:00. At Apr 24 12:00 elapsed = 69h. Anchored rate = 14/69 = 0.203%/h.
    # Remaining from Apr 24 12:00 to Apr 28 15:00 = 99h. Projection = 14 + 0.203*99 ≈ 34.1
    assert 30 <= proj <= 38


def test_project_stable_rate_low():
    """Anchored rate gives stable projection regardless of recent variation."""
    reset = datetime(2026, 4, 28, 15, 0)
    r1 = _reading(datetime(2026, 4, 23, 10, 0), 10, reset)
    r2 = _reading(datetime(2026, 4, 24, 10, 0), 12, reset)
    proj = project_final_pct([r1, r2], now=datetime(2026, 4, 24, 10, 0))
    # anchored at 12% / ~67h = 0.18%/h, *101h remaining + 12 ≈ 30
    assert 25 <= proj <= 35


def test_alert_when_projected_exceeds_threshold():
    assert should_alert(projected_pct=95, current_pct=40, threshold=90)
    assert should_alert(projected_pct=200, current_pct=14, threshold=90)


def test_no_alert_when_projected_safe():
    assert not should_alert(projected_pct=30, current_pct=14, threshold=90)


def test_alert_on_already_over_threshold_even_if_flat_rate():
    # Already at 91%, projection would be same — still alert
    assert should_alert(projected_pct=91, current_pct=91, threshold=90)


def test_empty_readings_raises():
    with pytest.raises(ValueError):
        project_final_pct([], now=datetime(2026, 4, 24))


def test_single_reading_uses_anchored():
    """With one reading at 15% and 67h into the week, projects via full-week rate."""
    reset = datetime(2026, 4, 28, 15, 0)
    # Week started Apr 21 15:00. At Apr 24 10:49 → 67.82h into week at 15%
    r = _reading(datetime(2026, 4, 24, 10, 49), 15, reset)
    proj = project_final_pct([r], now=datetime(2026, 4, 24, 10, 49))
    # rate = 15/67.82 = 0.221%/h; remaining = 100.18h; proj = 15 + 0.221*100.18 ≈ 37.2
    assert 35 <= proj <= 40


def test_short_burst_does_not_cause_panic_projection():
    """Real-world bug (2026-04-24): 38min burst of +2% was extrapolated to 324%."""
    reset = datetime(2026, 4, 28, 15, 0)
    # Mirror the real incident: 10:49 → 15%, 11:28 → 17%
    r1 = _reading(datetime(2026, 4, 24, 10, 49), 15, reset)
    r2 = _reading(datetime(2026, 4, 24, 11, 28), 17, reset)
    proj = project_final_pct([r1, r2], now=datetime(2026, 4, 24, 11, 28))
    # Anchored: week start Apr 21 15:00, elapsed ~68.5h, rate = 17/68.5 = 0.248%/h
    # Remaining 99.5h. Projection = 17 + 0.248*99.5 ≈ 41.7. Safe.
    assert proj < 60, f"anchored rate should prevent short-burst panic, got {proj}"


def test_project_session_final_pct_basic():
    from usage_monitor.projector import project_session_final_pct
    # Block ends at 20:00, started at 15:00. Now is 17:00 (2h elapsed, 3h left).
    # Session at 8% → rate = 4%/h → projection = 8 + 4*3 = 20%
    reading = {
        "session": {
            "pct": 8,
            "reset_at": "2026-04-25T20:00:00",
        },
    }
    proj = project_session_final_pct(reading, now=datetime(2026, 4, 25, 17, 0))
    assert 19 <= proj <= 21


def test_project_session_final_pct_returns_none_when_session_missing():
    from usage_monitor.projector import project_session_final_pct
    assert project_session_final_pct({}, now=datetime(2026, 4, 25, 17, 0)) is None
    assert project_session_final_pct({"session": None}, now=datetime(2026, 4, 25, 17, 0)) is None
    reading = {"session": {"pct": None, "reset_at": "2026-04-25T20:00:00"}}
    assert project_session_final_pct(reading, now=datetime(2026, 4, 25, 17, 0)) is None


def test_project_session_final_pct_handles_block_end_in_past():
    from usage_monitor.projector import project_session_final_pct
    reading = {"session": {"pct": 8, "reset_at": "2026-04-25T15:00:00"}}
    proj = project_session_final_pct(reading, now=datetime(2026, 4, 25, 17, 0))
    # Block already ended → return current value
    assert proj == 8.0
