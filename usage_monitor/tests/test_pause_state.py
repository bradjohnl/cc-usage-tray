"""Pause-state behavior: auto-pause on limit hit, manual pause respected, expiry."""
from datetime import datetime, timedelta

from usage_monitor import pause_state


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(pause_state, "PAUSE_FILE", tmp_path / "pause_state.json")


def test_no_pause_when_no_file(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert pause_state.load() is None
    assert pause_state.is_paused() is False


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    until = datetime(2026, 5, 5, 15, 0)
    pause_state.save(pause_state.Pause(until, pause_state.REASON_MANUAL, manual=True))
    p = pause_state.load(now=datetime(2026, 4, 28, 20, 0))
    assert p is not None
    assert p.paused_until == until
    assert p.manual is True


def test_pause_auto_clears_when_expired(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    pause_state.save(pause_state.Pause(datetime(2026, 4, 28, 18, 0), pause_state.REASON_MANUAL, manual=True))
    assert pause_state.load(now=datetime(2026, 4, 28, 20, 0)) is None
    assert not (tmp_path / "pause_state.json").exists()


def test_auto_pause_on_weekly_limit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    week_reset = datetime(2026, 5, 5, 15, 0)
    p = pause_state.auto_pause_for_limit(
        week_pct=100, session_pct=42,
        week_reset_at=week_reset, session_reset_at=datetime(2026, 4, 29, 1, 0),
        now=datetime(2026, 4, 28, 20, 0),
    )
    assert p is not None
    assert p.reason == pause_state.REASON_WEEKLY
    assert p.paused_until == week_reset
    assert p.manual is False


def test_auto_pause_on_session_limit_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    sess_reset = datetime(2026, 4, 29, 1, 0)
    p = pause_state.auto_pause_for_limit(
        week_pct=42, session_pct=100,
        week_reset_at=datetime(2026, 5, 5, 15, 0), session_reset_at=sess_reset,
        now=datetime(2026, 4, 28, 20, 0),
    )
    assert p is not None
    assert p.reason == pause_state.REASON_SESSION
    assert p.paused_until == sess_reset


def test_no_auto_pause_when_under_limits(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    p = pause_state.auto_pause_for_limit(
        week_pct=85, session_pct=70,
        week_reset_at=datetime(2026, 5, 5, 15, 0),
        session_reset_at=datetime(2026, 4, 29, 1, 0),
        now=datetime(2026, 4, 28, 20, 0),
    )
    assert p is None
    assert pause_state.load(now=datetime(2026, 4, 28, 20, 0)) is None


def test_manual_pause_blocks_auto_pause(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    manual_until = datetime(2026, 4, 28, 22, 0)
    pause_state.save(pause_state.Pause(manual_until, pause_state.REASON_MANUAL, manual=True))
    # Even though we hit weekly limit, the existing manual pause is preserved unchanged.
    p = pause_state.auto_pause_for_limit(
        week_pct=100, session_pct=100,
        week_reset_at=datetime(2026, 5, 5, 15, 0),
        session_reset_at=datetime(2026, 4, 29, 1, 0),
        now=datetime(2026, 4, 28, 20, 0),
    )
    assert p is None  # auto-pause didn't override
    still = pause_state.load(now=datetime(2026, 4, 28, 20, 0))
    assert still.manual is True
    assert still.paused_until == manual_until


def test_clear(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    pause_state.save(pause_state.Pause(datetime(2026, 5, 5, 15, 0), pause_state.REASON_MANUAL, manual=True))
    pause_state.clear()
    assert pause_state.load() is None
