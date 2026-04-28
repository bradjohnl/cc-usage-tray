# cc-usage-tray

A Linux tray icon + dashboard for **Claude Code** users that shows live `/usage` percentages, projections to reset, and stale-data warnings.

> **Not affiliated with or endorsed by Anthropic.** This tool reads only data Claude Code already writes to your local disk plus the JSON Anthropic itself feeds to the user-configurable [`statusLine` hook](https://code.claude.com/docs/en/statusline). It does **not** proxy, resell, or otherwise abuse Claude API access.

![Tray icon: green dot with "31% → 53%" label showing current week % and projected week % at reset](https://raw.githubusercontent.com/bradjohnl/cc-usage-tray/main/docs/tray.png)

## Why this exists

Claude Code's `/usage` slash command shows two numbers users care about:

- **5-hour rolling session**: how close you are to your subscription's short-window cap
- **7-day rolling weekly**: how close you are to the weekly cap

Both are critical for avoiding mid-session rate-limit surprises. But:

- The numbers are buried inside a TUI dialog you have to type `/usage` to see
- They reset asynchronously — no obvious schedule
- Anthropic doesn't publish the underlying token budgets, so third-party heuristics drift from reality

This project surfaces those exact numbers (matching `/usage` to the percentage point) in your system tray, with linear projections for both windows and stale-data warnings if the underlying data feed stops updating.

## How it works

Three sanctioned data sources, no internal API access:

```
                     ┌─────────────────────────────────────────────┐
   Claude Code ──┬──▶│ statusLine hook → JSON via stdin            │ ← rate_limits.{five_hour,seven_day}.used_percentage
                 │   │ (documented at code.claude.com/docs/en/statusline)
                 │   └─────────┬───────────────────────────────────┘
                 │             ▼
                 │   ┌─────────────────────────────────────────────┐
                 │   │ statusline-rate-capture.sh                  │ ← writes ~/.claude/usage_monitor/
                 │   │   (your statusLine command, wraps existing) │   rate_limits_cache.json
                 │   └─────────┬───────────────────────────────────┘
                 │             ▼
                 │   ┌─────────────────────────────────────────────┐
   Local JSONL ──┴──▶│ ccusage CLI (npm: ccusage@18+)              │ ← ccusage blocks --active --json
   transcripts       │   reads ~/.claude/projects/*.jsonl          │   (active 5h block: tokens, cost, burn rate)
                     └─────────┬───────────────────────────────────┘
                               ▼
                     ┌─────────────────────────────────────────────┐
                     │ usage_monitor.main (systemd timer, ~5 min)  │
                     │   merges cache + ccusage → readings.jsonl   │
                     │   computes projections, writes status file  │
                     └─────────┬───────────────────────────────────┘
                               ▼
                     ┌─────────────────────────────────────────────┐
                     │ tray (GTK3 AppIndicator) reads status file  │
                     │ every 10s, renders icon + label + menu      │
                     └─────────────────────────────────────────────┘
```

## What you get

**Tray icon** with color-coded disk:
- 🟢 Safe (below the warn threshold)
- 🟡 Approaching (≥ warn threshold, default 70%)
- 🔴 Alert (≥ alert threshold, default 90%, or projected to exceed)
- ⚪ Stale (data older than 10 min — your active Claude Code session has been idle)

Both thresholds are configurable — see [Configuration](#configuration).

**Tray label** (next to icon):
```
30% → 53%
```
Current week % → projected week % at reset.

**Click menu**:
```
Week (all models):        30%
Sonnet week:              —
Session (5h):             13%  →  31%   ends 20:00
Projected at reset:       53%  @ Tue 15:00
Rate:                     +0.52%/h
✓ Safe
Last fresh data:          17:21 (5m ago)
─────────────────────────
Alert strategy            ▸  ⦿ Anchored
                             ○ Active hours window
                             ○ Blend with history
                             ○ Day-of-week deviation
Active hours              ▸  Auto: 32 active hours/week
                             ─────
                             ⦿ Auto-detect from history
                             ○ Manual window
Pause alerts              ▸  Until session reset (5h)
                             Until weekly reset
                             For 1 hour
                             For 4 hours
Resume alerts                (visible when paused)
─────────────────────────
Open dashboard
Refresh now
Open status file
─────────────────────────
Quit tray
```

**HTML dashboard** (served live by the tray at `http://127.0.0.1:38734/dashboard`):
- Per-strategy projection table — all four projections side-by-side, the alerting one highlighted, with one-click "use for alerts" buttons
- Weekly SVG chart with all four projection lines color-coded; the alerting strategy is bold, the others faded; Y axis auto-scales when projections overshoot 100% so they remain visible above the limit line
- 5-hour session block chart with the same auto-scaling
- 7×24 heatmap visualising the active-hours mask (auto-detected or manual)
- Pause banner shown whenever alerts are silenced; clicking auto / manual on the active-hours card switches modes via the same control endpoint

**Command-line client** `usage-monitor-cli` (installed by `pip install -e .`):
```
usage-monitor-cli status              # current usage + pause state
usage-monitor-cli strategy [list|<name>]
usage-monitor-cli active-hours [show|auto|manual|set --start H --end H]
usage-monitor-cli pause [weekly|session|<duration>] [--until ISO]
usage-monitor-cli resume
usage-monitor-cli reset-history
```

**Auto-pause** when the 5-hour session or the 7-day window hits 100% — alerts go quiet until that limit's reset, so you don't get re-pinged every minute when there's nothing to do about it. Manual pause is honoured and never overridden by auto-pause.

**Optional ntfy push notifications** when projection or current % crosses the alert threshold, plus a desktop notification via `notify-send`.

## Compatibility

The tray uses [AyatanaAppIndicator3](https://ayatanaindicators.github.io/), the
maintained successor to the legacy AppIndicator. Library packaging and tray
support varies by desktop:

| Desktop | Tray icon shows? |
|---|---|
| **KDE Plasma** | ✅ Native (StatusNotifierItem) |
| **Cinnamon / MATE / Budgie / Xfce / LXQt** | ✅ Native |
| **Pop!_OS / Ubuntu** (GNOME-based but ship the extension by default) | ✅ |
| **GNOME (stock, since 3.26 / Sep 2017)** | ⚠️ Requires the [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/) extension. Install: `sudo apt install gnome-shell-extension-appindicator` (Debian/Ubuntu) or `sudo dnf install gnome-shell-extension-appindicator` (Fedora), then enable in GNOME Extensions and log out/in |
| **Wayland sessions** | ✅ Same behavior as X11 (Ayatana uses StatusNotifierItem, not X11-specific code) |

### Library package by distro

The Python `gi` binding for AyatanaAppIndicator3 needs the typelib. Install the
appropriate package for your distro:

| Distro | Package |
|---|---|
| Debian / Ubuntu / Mint / Pop!_OS / Kali | `gir1.2-ayatanaappindicator3-0.1` |
| Fedora / RHEL / CentOS Stream | `libayatana-appindicator-gtk3` |
| Arch / Manjaro / EndeavourOS | `libayatana-appindicator` (`extra` repo) |
| openSUSE | `typelib-1_0-AyatanaAppIndicator3-0_1` |
| NixOS | `libayatana-appindicator` |
| Void / Alpine / musl-based | varies; you may need to build from source |

If you're on stock GNOME and don't want to install the extension, the
`usage_monitor` daemon still produces `~/.claude/usage_status.txt` and the HTML
dashboard — you just lose the tray icon and would need to glance at the file
or open the dashboard manually.

## Requirements

- Linux with systemd `--user` services (tested on Pop!_OS 22.04, GNOME)
- Python 3.10+
- GTK 3 + AyatanaAppIndicator3 typelibs:
  ```
  sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 python3-cairo
  ```
- Node.js 20+ for the `ccusage` CLI
- An active Claude Code subscription (Pro / Max). API-key-only users won't have a `/usage` to surface.
- `jq` (for the statusLine wrapper)

## Setup

### 1. Install ccusage (pinned)

```bash
npm install -g ccusage@18.0.11
```

> Why pinned? See [Verify before install](#supply-chain-safety).

### 2. Clone & install

```bash
git clone https://github.com/<your-username>/cc-usage-tray.git
cd cc-usage-tray
pip install --user -e .   # or just put usage_monitor/ on PYTHONPATH
```

### 3. Wire up the statusLine wrapper

Copy the wrapper to `~/.claude/`:

```bash
cp hooks/statusline-rate-capture.sh ~/.claude/
chmod +x ~/.claude/statusline-rate-capture.sh
```

If you already have a `statusLine` command configured in `~/.claude/settings.json`, the wrapper will delegate to it. Set it up:

```jsonc
// ~/.claude/settings.json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline-rate-capture.sh"
  }
}
```

You can keep your existing prompt logic — put it in `~/.claude/statusline-command.sh` and the wrapper will pipe through to it. Or leave that file absent and the wrapper just captures rate_limits silently.

### 4. Install systemd units

```bash
mkdir -p ~/.config/systemd/user
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cc-usage-monitor.timer
systemctl --user enable --now cc-usage-tray.service
```

### 5. (Optional) ntfy push notifications

To get phone push notifications when usage crosses thresholds, set in your shell profile:

```bash
export CLAUDE_USAGE_NTFY_URL="https://ntfy.sh"
export CLAUDE_USAGE_NTFY_TOPIC="your-private-secret-topic-string"
```

Or self-hosted:

```bash
export CLAUDE_USAGE_NTFY_URL="https://ntfy.your-domain.example"
export CLAUDE_USAGE_NTFY_TOPIC="claude-usage"
```

Then install the [ntfy app](https://ntfy.sh/) on your phone and subscribe to the same topic. **Use a long, unguessable topic string** — there's no auth on ntfy.sh public; anyone who knows the topic name can subscribe.

If both env vars are unset, only local desktop notifications fire.

## Configuration

### Environment variables

| Env var | Default | Purpose |
|---|---|---|
| `CC_USAGE_WARN_PCT` | `70` | Amber-zone threshold — tray turns yellow at or above |
| `CC_USAGE_ALERT_PCT` | `90` | Red-zone threshold — tray turns red, ntfy fires at or above |
| `CLAUDE_USAGE_NTFY_URL` | unset (no push) | ntfy server base URL |
| `CLAUDE_USAGE_NTFY_TOPIC` | unset (no push) | ntfy topic name |
| `CCUSAGE_BINARY` | `ccusage` (PATH lookup) | Path to ccusage if not on PATH |
| `USAGE_PROJECTION_STRATEGY` | reads `config.json` | Override the alert strategy for this process |
| `CC_USAGE_TRAY_CONTROL_PORT` | `38734` | Localhost port for the tray's HTTP control server |

### `~/.claude/usage_monitor/config.json`

Persistent settings. Created on first config change; defaults below apply when absent or when a key is missing:

```jsonc
{
  "projection_strategy": "anchored",   // anchored | active_hours | blend | dow_curve
  "min_elapsed_hours": 0.0,            // suppress projection for the first N hours of a week
  "active_hours": {
    "mode": "manual",                   // "manual" or "auto"
    "start": 8,                         // manual window start hour (0-23)
    "end": 22,                          // manual window end hour (1-24)
    "weekdays_only": false,             // manual only: skip Sat/Sun
    "auto_lookback_days": 28,           // auto only: rolling readings window
    "auto_min_active_fraction": 0.25    // auto only: bucket active when ≥this fraction of weeks had burn
  },
  "blend": {
    "current_weight": 0.3,
    "historical_weight": 0.7,
    "history_window": 4                 // last N completed weeks from weekly_history.jsonl
  }
}
```

Edit this file directly, switch from the tray, or use `usage-monitor-cli`. The tray, dashboard, and daemon all read the same file — a change in one place is visible everywhere within the next refresh tick (10 s tray, instant on dashboard click).

`CC_USAGE_WARN_PCT` must be strictly less than `CC_USAGE_ALERT_PCT`; invalid
values fall back to the defaults with a warning on stderr.

To override threshold values for the systemd services, drop a unit override:

```bash
systemctl --user edit cc-usage-monitor.service
```

```ini
[Service]
Environment=CC_USAGE_WARN_PCT=60
Environment=CC_USAGE_ALERT_PCT=85
```

Repeat for `cc-usage-tray.service` so the tray and the daemon agree (the
dashboard reads its thresholds from the daemon process). Then:

```bash
systemctl --user daemon-reload
systemctl --user restart cc-usage-monitor.service cc-usage-tray.service
```

The default scrape cadence is 5 minutes; edit `systemd/cc-usage-monitor.timer` `OnUnitActiveSec=` to change.

## Projection strategies

The week-end projection (`→ NN%`) is configurable. Pick whichever matches your usage shape best:

| Strategy | What it does | Best when |
|---|---|---|
| `anchored` | `current_pct / hours_elapsed_in_week × hours_remaining + current_pct` | You burn evenly across the whole week (rare). The "linear extrapolate everything" baseline. |
| `active_hours` | Same formula, but only counts hours inside an active-hours mask (default 08–22 every day, or auto-detected from your history). | You only use Claude during work / waking hours — projection no longer extrapolates an early-week burst through nights and weekends. |
| `blend` | `0.3 × anchored + 0.7 × trailing 4-week average final pct` (from `weekly_history.jsonl`). | You have a stable weekly average and want a smoothed projection that doesn't panic on a single bursty day. Falls back to `anchored` when there's no history yet. |
| `dow_curve` | `historical_avg + (current_pct − expected_pct_at_this_active_hour)` — flags only deviation from your usual curve. | You want the alert to fire only when you're meaningfully *ahead* of where you usually are at this point in the week. Falls back to `active_hours` without history. |

**Active-hours auto-detection** (`active_hours.mode = "auto"`, default for new installs): for each consecutive pair of readings within the last 28 days, if `week_all.pct` increased, the `(weekday, hour)` of the second reading is marked as active for that ISO-week. Buckets stay active when ≥25% of observed weeks had positive burn there. The mask is recomputed every projection — readings beyond the lookback window drop out automatically.

**weekly_history.jsonl** (used by `blend` and `dow_curve`) is auto-populated: every scraper run scans `readings.jsonl` for completed weeks (reset_at values not equal to the current week's) and records their max `week_all.pct` as the final pct. Idempotent.

Switch strategy via:
- Tray menu (`Alert strategy ▸ …`)
- Dashboard ("use for alerts" button on any row)
- CLI: `usage-monitor-cli strategy active_hours`
- Env var: `USAGE_PROJECTION_STRATEGY=active_hours`
- Config: `~/.claude/usage_monitor/config.json` → `projection_strategy`

## How accurate is this?

Session % and week % match `/usage` to the percentage point — they're literally the same numbers Claude Code itself displays, captured from the JSON Anthropic feeds to your statusLine hook.

The projection (`→ NN%`) depends on the strategy you pick (see above). The default `anchored` strategy is pessimistic-stable — it doesn't panic from short bursts (we measured a 38-minute +2% burst projecting to 324% before fixing the rate window), but it does flag sustained high burn early. If a flat anchored projection over-fires for your usage pattern, switch to `active_hours` (or `blend` once you have a few weeks of history).

## Limitations

- **Stale data when no Claude Code session is open**: the rate_limits cache only updates while a Claude Code TUI is running. If you close all Claude windows and stay closed for >10 min, the tray shows ⚠ stale.
- **No per-model breakdown**: only "all models" weekly is exposed by Anthropic. Sonnet-only is not in the statusLine JSON (you'd need the deprecated tmux probe to get it; we dropped that in this version).
- **Linux/GTK only**: AyatanaAppIndicator is GTK3. Mac/Windows not supported.

## Supply-chain safety

This project's only runtime npm dependency is `ccusage` (0 transitive dependencies, [13.3k stars](https://github.com/ryoppippi/ccusage), MIT). The pinned version is `18.0.11`. Audit before bumping.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [ccusage](https://github.com/ryoppippi/ccusage) (@ryoppippi) — the JSONL parser this project depends on
- [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) (@Maciek-roboblog) — different approach, same problem space
- The Claude Code team at Anthropic for documenting the [statusLine hook schema](https://code.claude.com/docs/en/statusline) — without that, this tool would be much hackier
