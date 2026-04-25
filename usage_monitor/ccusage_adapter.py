"""Adapter around the `ccusage` CLI for 5h-block session data.

Why we use ccusage: Claude Code v2.1.114's `/usage` TUI only renders the
"Current session" section after the process has API activity, so our throwaway
tmux probe never sees it. ccusage instead reads the JSONL transcripts that
Claude Code writes to disk at ~/.claude/projects/*, so it surfaces the active
5h block without needing a live session.

Docs: https://ccusage.com/guide/blocks-reports
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


CCUSAGE_TIMEOUT_S = 180  # ccusage can be slow on accounts with many JSONL files


def _resolve_binary() -> str:
    """Locate ccusage — PATH first, then common NVM install dirs.

    systemd --user services get a minimal PATH that excludes NVM's node bin,
    so `which ccusage` fails there even when the CLI is installed.
    """
    env_override = os.environ.get("CCUSAGE_BINARY")
    if env_override and os.path.isfile(env_override) and os.access(env_override, os.X_OK):
        return env_override
    found = shutil.which("ccusage")
    if found:
        return found
    candidates = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/ccusage"))
    candidates += ["/usr/local/bin/ccusage", "/usr/bin/ccusage"]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError(
        "ccusage not found. Install with: npm install -g ccusage@18.0.11"
    )


@dataclass
class ActiveBlock:
    start: datetime
    end: datetime
    actual_end: datetime
    total_tokens: int
    cost_usd: float
    tokens_per_minute: float
    cost_per_hour: float
    projected_tokens: int
    projected_cost_usd: float
    models: list[str]

    def time_remaining_seconds(self, now: datetime) -> float:
        return (self.end - now).total_seconds()


def _parse_iso(s: str) -> datetime:
    # ccusage returns UTC ISO with milliseconds + Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_active_block(
    binary: Optional[str] = None, offline: bool = True, timeout_s: int = CCUSAGE_TIMEOUT_S
) -> Optional[ActiveBlock]:
    """Call `ccusage blocks --active --json`. Return None if no active block."""
    resolved = binary or _resolve_binary()
    cmd = [resolved, "blocks", "--active", "--json"]
    if offline:
        cmd.append("--offline")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=True
        )
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "")[-500:]
        raise RuntimeError(f"ccusage exit {e.returncode}: {stderr_tail}") from e
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise RuntimeError(f"ccusage call failed: {e}") from e

    data = json.loads(result.stdout)
    blocks = data.get("blocks", [])
    if not blocks:
        return None
    # --active should return only the active block, but guard anyway
    active = next((b for b in blocks if b.get("isActive")), None)
    if active is None:
        return None

    burn = active.get("burnRate") or {}
    proj = active.get("projection") or {}
    return ActiveBlock(
        start=_parse_iso(active["startTime"]),
        end=_parse_iso(active["endTime"]),
        actual_end=_parse_iso(active["actualEndTime"]),
        total_tokens=int(active.get("totalTokens", 0)),
        cost_usd=float(active.get("costUSD", 0.0)),
        tokens_per_minute=float(burn.get("tokensPerMinute", 0.0)),
        cost_per_hour=float(burn.get("costPerHour", 0.0)),
        projected_tokens=int(proj.get("totalTokens", 0)),
        projected_cost_usd=float(proj.get("totalCost", 0.0)),
        models=list(active.get("models") or []),
    )
