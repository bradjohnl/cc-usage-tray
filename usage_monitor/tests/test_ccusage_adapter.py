"""Tests for the ccusage adapter. Does not shell out — mocks subprocess."""
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from usage_monitor.ccusage_adapter import (
    ActiveBlock,
    fetch_active_block,
)


# Representative output from `ccusage blocks --active --offline --json`
# captured on 2026-04-24. Values slightly abbreviated for readability.
FIXTURE_ACTIVE_BLOCK_JSON = json.dumps({
    "blocks": [{
        "id": "2026-04-24T16:00:00.000Z",
        "startTime": "2026-04-24T16:00:00.000Z",
        "endTime": "2026-04-24T21:00:00.000Z",
        "actualEndTime": "2026-04-24T20:58:15.901Z",
        "isActive": True,
        "isGap": False,
        "entries": 478,
        "tokenCounts": {
            "inputTokens": 3102,
            "outputTokens": 329534,
            "cacheCreationInputTokens": 6850532,
            "cacheReadInputTokens": 48596957,
        },
        "totalTokens": 55780125,
        "costUSD": 44.446109799999995,
        "models": ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"],
        "burnRate": {
            "tokensPerMinute": 207875.49,
            "tokensPerMinuteForIndicator": 1239.63,
            "costPerHour": 9.93,
        },
        "projection": {
            "totalTokens": 56052522,
            "totalCost": 45.01,
        },
    }]
})

FIXTURE_EMPTY_JSON = json.dumps({"blocks": []})


def _mock_run_success(stdout: str):
    def _fake(*args, **kwargs):
        m = MagicMock()
        m.stdout = stdout
        return m
    return _fake


def test_fetch_active_block_parses_success():
    with patch("subprocess.run", side_effect=_mock_run_success(FIXTURE_ACTIVE_BLOCK_JSON)):
        block = fetch_active_block()
    assert block is not None
    assert isinstance(block, ActiveBlock)
    assert block.total_tokens == 55_780_125
    assert block.cost_usd == pytest.approx(44.446, rel=1e-3)
    assert block.tokens_per_minute == pytest.approx(207_875.49, rel=1e-3)
    assert block.projected_tokens == 56_052_522
    assert "claude-sonnet-4-6" in block.models


def test_fetch_active_block_returns_none_if_no_blocks():
    with patch("subprocess.run", side_effect=_mock_run_success(FIXTURE_EMPTY_JSON)):
        block = fetch_active_block()
    assert block is None


def test_time_remaining_seconds_positive_while_active():
    with patch("subprocess.run", side_effect=_mock_run_success(FIXTURE_ACTIVE_BLOCK_JSON)):
        block = fetch_active_block()
    # Block ends at 2026-04-24T21:00:00Z; 'now' at 2026-04-24T20:00:00Z -> 3600s left
    now = datetime(2026, 4, 24, 20, 0, 0, tzinfo=timezone.utc)
    assert block.time_remaining_seconds(now) == pytest.approx(3600, abs=1)


def test_fetch_active_block_raises_on_ccusage_failure():
    import subprocess as _sp
    def _fail(*a, **kw):
        raise _sp.CalledProcessError(1, "ccusage")
    with patch("subprocess.run", side_effect=_fail):
        with pytest.raises(RuntimeError):
            fetch_active_block()
