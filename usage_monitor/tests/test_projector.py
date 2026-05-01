"""Projection tests: given two readings, predict week-end %."""
import pytest
from datetime import datetime, timedelta
from usage_monitor.projector import project_final_pct as _project_final_pct, should_alert


def project_final_pct(readings, now, **kwargs):
    """Default to anchored in tests so they don't read the user's live config."""
    if "strategy" not in kwargs and "config" not in kwargs:
        kwargs["strategy"] = "anchored"
    return _project_final_pct(readings, now, **kwargs)


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


def test_project_session_final_pct_active_hours_strategy_damps_offhours():
    """active_hours strategy should not extrapolate the 5h block through
    inactive (e.g. overnight) hours."""
    from usage_monitor.projector import project_session_final_pct
    # Block 22:00 → 03:00 next day. We're at 23:00 (1h elapsed) with 8% used.
    # Active window: 08-22 every day → block_start..now has ~0 active hours,
    # remaining 03:00 also outside. Strategy should pin at current pct.
    reading = {
        "session": {
            "pct": 8,
            "reset_at": "2026-04-26T03:00:00",
        },
    }
    cfg = {
        "session_projection_strategy": "active_hours",
        "active_hours": {
            "mode": "manual",
            "start": 8,
            "end": 22,
            "weekdays_only": False,
        },
    }
    proj = project_session_final_pct(
        reading,
        now=datetime(2026, 4, 25, 23, 0),
        strategy="active_hours",
        config=cfg,
    )
    # No active hours covered → rate undefined → returns current pct.
    assert proj == 8.0


def test_project_session_final_pct_unknown_strategy_falls_back_to_anchored():
    from usage_monitor.projector import project_session_final_pct
    reading = {"session": {"pct": 8, "reset_at": "2026-04-25T20:00:00"}}
    proj = project_session_final_pct(
        reading,
        now=datetime(2026, 4, 25, 17, 0),
        strategy="blend",  # not a session strategy → anchored fallback
    )
    assert 19 <= proj <= 21


def test_session_strategies_constant_excludes_history_dependent():
    from usage_monitor.projector import SESSION_STRATEGIES
    assert SESSION_STRATEGIES == ("anchored", "active_hours")


# ---------- strategy: active_hours ----------

def _cfg(strategy, **overrides):
    base = {
        "projection_strategy": strategy,
        "min_elapsed_hours": 0.0,
        "active_hours": {
            "mode": "manual",
            "start": 9, "end": 19, "weekdays_only": True,
            "auto_lookback_days": 28, "auto_min_active_fraction": 0.25,
        },
        "blend": {"current_weight": 0.3, "historical_weight": 0.7, "history_window": 4},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def test_active_hours_damps_early_week_burst():
    """Tue 18:47, 9% used in ~3.78h since Tue 15:00 reset.
    Anchored: 9 + 9/3.78*164 ≈ 399%. Active-hours window 9-19 weekdays:
    Tue active elapsed = 15:00→18:47 ≈ 3.78h (within window).
    Tue remaining: 18:47→19:00 = 0.22h; Wed-Fri 9-19 = 30h; weekend 0; next Mon 9-15 = 6h.
    Active remaining ≈ 36.22h → projection = 9 + 9/3.78 * 36.22 ≈ 95% — far below 399%.
    """
    reset = datetime(2026, 5, 5, 15, 0)  # next Tue 15:00
    week_start = datetime(2026, 4, 28, 15, 0)  # this Tue 15:00
    captured = datetime(2026, 4, 28, 18, 47)
    r = _reading(captured, 9, reset)
    # sanity: this week_start is reset - 168h
    assert (reset - week_start).total_seconds() / 3600 == 168
    proj = project_final_pct([r], now=captured, config=_cfg("active_hours"))
    assert proj < 120, f"active_hours should damp burst, got {proj}"


def test_active_hours_zero_elapsed_returns_current():
    """If captured at exact week start (outside active window), no extrapolation."""
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 15, 0)  # Tue 15:00 (outside 9-19? actually 15 is inside)
    # Use a Saturday capture to land outside any active window
    sat_reset = datetime(2026, 5, 9, 15, 0)
    sat_capture = datetime(2026, 5, 2, 12, 0)  # Saturday → no active hours yet (weekdays_only)
    r = _reading(sat_capture, 0, sat_reset)
    proj = project_final_pct([r], now=sat_capture, config=_cfg("active_hours"))
    assert proj == 0.0


# ---------- strategy: blend ----------

def test_blend_falls_back_to_anchored_without_history(tmp_path, monkeypatch):
    from usage_monitor import projector
    monkeypatch.setattr(projector, "HISTORY_PATH", tmp_path / "no_such_file.jsonl")
    reset = datetime(2026, 4, 28, 15, 0)
    r = _reading(datetime(2026, 4, 24, 10, 49), 15, reset)
    proj_blend = project_final_pct([r], now=datetime(2026, 4, 24, 10, 49), config=_cfg("blend"))
    proj_anchored = project_final_pct([r], now=datetime(2026, 4, 24, 10, 49), config=_cfg("anchored"))
    assert abs(proj_blend - proj_anchored) < 0.01


def test_blend_uses_history_when_available(tmp_path, monkeypatch):
    from usage_monitor import projector
    hist = tmp_path / "history.jsonl"
    hist.write_text(
        '{"final_pct": 40}\n{"final_pct": 50}\n{"final_pct": 45}\n{"final_pct": 55}\n'
    )
    monkeypatch.setattr(projector, "HISTORY_PATH", hist)
    # Anchored would project ~399% (early-week burst); blend with avg=47.5 should pull it down.
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 18, 47)
    r = _reading(captured, 9, reset)
    proj = project_final_pct([r], now=captured, config=_cfg("blend"))
    # 0.3*399 + 0.7*47.5 = ~152
    assert 100 < proj < 200, f"blend should temper extrapolation, got {proj}"


# ---------- strategy: dow_curve ----------

def test_dow_curve_falls_back_to_active_hours_without_history(tmp_path, monkeypatch):
    from usage_monitor import projector
    monkeypatch.setattr(projector, "HISTORY_PATH", tmp_path / "missing.jsonl")
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 18, 47)
    r = _reading(captured, 9, reset)
    proj_dow = project_final_pct([r], now=captured, config=_cfg("dow_curve"))
    proj_active = project_final_pct([r], now=captured, config=_cfg("active_hours"))
    assert abs(proj_dow - proj_active) < 0.01


def test_dow_curve_only_flags_when_ahead_of_curve(tmp_path, monkeypatch):
    from usage_monitor import projector
    hist = tmp_path / "history.jsonl"
    hist.write_text('{"final_pct": 50}\n{"final_pct": 50}\n')
    monkeypatch.setattr(projector, "HISTORY_PATH", hist)
    # If user is roughly on the curve (current ≈ expected), projection ≈ historical avg.
    # active_total over the week (Wed-Fri 9-19, Mon 9-15) — accept any fraction; we
    # test that being 'close to curve' projects near historical avg, not 399%.
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 18, 47)
    r = _reading(captured, 9, reset)
    proj = project_final_pct([r], now=captured, config=_cfg("dow_curve"))
    assert 0 <= proj <= 80, f"dow_curve should not panic-project, got {proj}"


# ---------- min-elapsed gate ----------

def test_min_elapsed_gate_returns_current_when_too_early():
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 17, 0)  # 2h into new week
    r = _reading(captured, 5, reset)
    proj = project_final_pct(
        [r], now=captured, config=_cfg("anchored", min_elapsed_hours=6.0)
    )
    assert proj == 5.0


# ---------- env override ----------

def test_env_var_overrides_config_strategy(monkeypatch, tmp_path):
    from usage_monitor.config import load_config
    monkeypatch.setenv("USAGE_PROJECTION_STRATEGY", "active_hours")
    cfg = load_config()
    assert cfg["projection_strategy"] == "active_hours"


# ---------- auto-detected active hours ----------

def test_auto_active_mask_detects_late_evening_burn():
    """User burning at 21:00–22:00 should yield a mask covering those hours."""
    from usage_monitor.projector import auto_active_mask
    cfg = _cfg("active_hours", active_hours={
        "mode": "auto", "auto_lookback_days": 60, "auto_min_active_fraction": 0.25,
    })
    reset = datetime(2026, 5, 5, 15, 0)
    readings = []
    pct = 0
    for week_off in (14, 7):
        base = datetime(2026, 4, 28, 0, 0) - timedelta(days=week_off)
        for hour in (21, 22):
            readings.append(_reading(base + timedelta(hours=hour), pct, reset))
            pct += 2
            readings.append(_reading(base + timedelta(hours=hour, minutes=15), pct, reset))
            pct += 2
    mask = auto_active_mask(readings, cfg, now=datetime(2026, 4, 28, 23, 0))
    assert mask is not None
    late = {(wd, h) for (wd, h) in mask if h in (21, 22)}
    assert late, f"expected late-evening buckets, got {sorted(mask)[:10]}…"


def test_auto_active_mask_returns_none_with_no_data():
    from usage_monitor.projector import auto_active_mask
    cfg = _cfg("active_hours", active_hours={"mode": "auto"})
    assert auto_active_mask([], cfg) is None


def test_auto_active_hours_projects_when_capture_is_outside_manual_window():
    """Capture at 21:00 with evening-burn history: auto mode produces a real
    projection (>current_pct) instead of being stuck at the current pct as a
    manual 09–19 window with empty intersection would.
    """
    cfg_auto = _cfg("active_hours", active_hours={
        "mode": "auto", "auto_lookback_days": 60, "auto_min_active_fraction": 0.25,
    })
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 21, 0)
    readings = []
    pct = 0
    for week_off in (21, 14, 7):
        base = datetime(2026, 4, 28, 0, 0) - timedelta(days=week_off)
        for hour in (20, 21, 22):
            readings.append(_reading(base + timedelta(hours=hour), pct, reset))
            pct += 1
            readings.append(_reading(base + timedelta(hours=hour, minutes=15), pct, reset))
            pct += 1
    readings.append(_reading(captured, 11, reset))
    proj_auto = project_final_pct(readings, now=captured, config=cfg_auto)
    # Auto mask captures the Tue 20-22 evening pattern, so there's at least
    # one more "active" hour ahead → projection moves above current.
    assert proj_auto > 11
    # And it stays bounded — the mask is narrow so we don't get a 400% panic.
    assert proj_auto < 200


def test_active_hours_empty_mask_returns_current():
    """Mask with zero buckets means no active hours → just report current pct."""
    cfg = _cfg("active_hours", active_hours={
        "mode": "manual", "start": 0, "end": 0, "weekdays_only": False,
    })
    reset = datetime(2026, 5, 5, 15, 0)
    captured = datetime(2026, 4, 28, 21, 0)
    r = _reading(captured, 9, reset)
    proj = project_final_pct([r], now=captured, config=cfg)
    assert proj == 9.0
