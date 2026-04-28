"""CLI smoke tests."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from usage_monitor import cli, config, pause_state


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    hist_path = tmp_path / "history.jsonl"
    pause_path = tmp_path / "pause.json"
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "HISTORY_PATH", hist_path)
    monkeypatch.setattr(cli, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(cli, "HISTORY_PATH", hist_path)
    monkeypatch.setattr(pause_state, "PAUSE_FILE", pause_path)
    return tmp_path


def test_strategy_list_default(isolated, capsys):
    rc = cli.main(["strategy", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anchored" in out
    assert "active_hours" in out
    assert "blend" in out
    assert "dow_curve" in out


def test_strategy_switch_writes_config(isolated, capsys):
    rc = cli.main(["strategy", "active_hours"])
    assert rc == 0
    cfg = json.loads((isolated / "config.json").read_text())
    assert cfg["projection_strategy"] == "active_hours"


def test_strategy_unknown_rejected(isolated, capsys):
    rc = cli.main(["strategy", "bogus"])
    assert rc == 2


def test_pause_duration(isolated, capsys):
    rc = cli.main(["pause", "30m"])
    assert rc == 0
    p = pause_state.load()
    assert p is not None
    assert p.manual is True
    delta = p.paused_until - datetime.now()
    assert timedelta(minutes=29) <= delta <= timedelta(minutes=31)


def test_pause_until_iso(isolated, capsys):
    future = (datetime.now() + timedelta(hours=2)).replace(microsecond=0)
    rc = cli.main(["pause", "--until", future.isoformat()])
    assert rc == 0
    p = pause_state.load()
    assert p.paused_until == future


def test_pause_invalid_target(isolated, capsys):
    rc = cli.main(["pause", "garbage"])
    assert rc == 2


def test_resume_clears_pause(isolated, capsys):
    pause_state.save(pause_state.Pause(datetime.now() + timedelta(hours=1), pause_state.REASON_MANUAL, manual=True))
    rc = cli.main(["resume"])
    assert rc == 0
    assert pause_state.load() is None


def test_reset_history(isolated, capsys):
    (isolated / "history.jsonl").write_text('{"final_pct": 50}\n')
    rc = cli.main(["reset-history"])
    assert rc == 0
    assert not (isolated / "history.jsonl").exists()
