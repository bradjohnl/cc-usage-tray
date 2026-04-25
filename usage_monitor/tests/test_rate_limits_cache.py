"""Tests for the statusLine rate-limit cache reader."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from usage_monitor.rate_limits_cache import RateLimitsSnapshot, read_cache


def _write_cache(tmp_path: Path, captured_at: float, payload: dict) -> Path:
    f = tmp_path / "rate_limits_cache.json"
    f.write_text(json.dumps({"captured_at": captured_at, **payload}))
    return f


def test_read_cache_parses_full_payload(tmp_path):
    f = _write_cache(tmp_path, 1738423800.0, {
        "session_id": "abc",
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        },
    })
    snap = read_cache(f)
    assert isinstance(snap, RateLimitsSnapshot)
    assert snap.five_hour_pct == 23.5
    assert snap.seven_day_pct == 41.2
    assert snap.five_hour_reset_at == datetime.fromtimestamp(1738425600, tz=timezone.utc)
    assert snap.seven_day_reset_at == datetime.fromtimestamp(1738857600, tz=timezone.utc)


def test_read_cache_handles_missing_file(tmp_path):
    assert read_cache(tmp_path / "nope.json") is None


def test_read_cache_handles_corrupt_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json")
    assert read_cache(f) is None


def test_read_cache_handles_missing_rate_limits(tmp_path):
    f = _write_cache(tmp_path, 1738423800.0, {"session_id": "abc"})
    snap = read_cache(f)
    assert snap is not None
    assert snap.five_hour_pct is None
    assert snap.seven_day_pct is None


def test_age_seconds():
    now = datetime(2026, 4, 25, 14, 0, tzinfo=timezone.utc)
    snap = RateLimitsSnapshot(
        captured_at=now - timedelta(seconds=120),
        five_hour_pct=10.0, five_hour_reset_at=None,
        seven_day_pct=20.0, seven_day_reset_at=None,
    )
    assert snap.age_seconds(now=now) == pytest.approx(120, abs=1)
