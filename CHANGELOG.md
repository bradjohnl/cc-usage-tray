# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Auto-detected active hours.** New `active_hours.mode` field
  (`"manual"` | `"auto"`). In `auto` mode, the projector derives the
  set of `(weekday, hour)` buckets where you actually burn weekly % from
  `readings.jsonl` over the last `auto_lookback_days` (default 28). A bucket
  becomes "active" only if it had positive burn in at least
  `auto_min_active_fraction` (default 25%) of the distinct weeks observed.
  This fixes the "I'm still working at 21:00 but the projection ignores it"
  failure mode.
- Manual default widened from `09–19 weekdays-only` to `08–22 every day`,
  so the manual fallback no longer silently excludes evenings.
- CLI subcommand `usage-monitor-cli active-hours [show|auto|manual|set]`
  with `--start H --end H --weekdays-only|--all-days`. `show` prints the
  current mode and renders the auto-derived mask grouped by weekday.
- `usage_monitor.projector.auto_active_mask(readings, cfg)` — public helper
  for the CLI / dashboard / future tray submenu.
- Tests for auto-detection (mask shape, no-data fallback, projection delta).

### Changed
- `_active_hours_between(start, end, mask)` now operates on a `(weekday, hour)`
  bucket mask instead of `(start_hour, end_hour, weekdays_only)` directly.
  Manual mode produces an equivalent mask via `_manual_mask(cfg)`.
- Active-hours strategy and DoW-curve fallback now resolve their mask via
  `_resolve_mask(cfg, readings)`, which falls back to disk-loaded readings
  when the caller passes only a recent tail.

## [0.4.0] - 2026-04-28

### Added
- **Pluggable projection strategies.** The week-end projection is no longer
  forced to be anchored linear extrapolation. Choose between:
  - `anchored` (current behavior, kept as default for backward compat),
  - `active_hours` — only counts 09–19 weekdays in elapsed/remaining time,
    so an early-week burst no longer extrapolates through nights/weekends,
  - `blend` — `0.3·anchored + 0.7·trailing_4_week_avg` from
    `~/.claude/usage_monitor/weekly_history.jsonl` (falls back to anchored
    when no history),
  - `dow_curve` — projects only the deviation from the historical
    by-active-hour curve (falls back to `active_hours` without history).
- **Configurable via `~/.claude/usage_monitor/config.json`** with optional
  env override `USAGE_PROJECTION_STRATEGY`. New `usage_monitor/config.py`
  centralizes the loader.
- **Auto-pause on hard limits.** When session hits 5h block 100% or week hits
  100%, alerts auto-pause until that limit's reset. Manual pause is honored
  and never silently overridden by auto-pause.
- **Tray menu**:
  - "Alert strategy ▸" radio submenu to switch the alerting strategy live.
  - "Pause alerts ▸" submenu (until session reset / weekly reset / 1h / 4h)
    plus a "Resume alerts" item that appears whenever a pause is active.
  - Pause status is shown in the menu while active.
- **CLI**: `usage-monitor-cli` (registered as a `[project.scripts]` entry)
  with `status`, `strategy [list|<name>]`, `pause [weekly|session|<dur>|--until]`,
  `resume`, and `reset-history` subcommands. Mirrors the tray menu so you
  can drive everything over SSH.
- **Dashboard**: a "Projection by strategy" comparison table renders all
  four projections side-by-side, highlights the alerting strategy, and
  exposes a "use for alerts" link per row.
- New modules `usage_monitor/pause_state.py` (auto-pause + manual pause
  persistence), `usage_monitor/cli.py` (CLI), and tests
  `tests/test_pause_state.py`, `tests/test_cli.py`. `tests/test_projector.py`
  grew 8 new tests for the new strategies + min-elapsed gate + env override.

### Changed
- `usage_monitor/projector.py` refactored: `project_final_pct(...)` now takes
  optional `strategy=` and `config=` kwargs and dispatches; existing callers
  keep their no-kwargs call site (`anchored` is still the default if no
  config file exists). New `project_all_strategies(readings, now, config=)`
  returns a `dict[strategy → projected_pct]` for the dashboard.
- `usage_monitor/main.py` invokes `pause_state.auto_pause_for_limit(...)`
  before evaluating whether to fire a desktop / ntfy alert; alerts are
  suppressed while a pause is active.
- Tray's `_update()` consults `pause_state` and suppresses the
  notify-send / ntfy emission when paused, while still updating the icon
  and menu numerics.

## [0.3.0] - 2026-04-28

### Added
- Configurable warn/alert thresholds via `CC_USAGE_WARN_PCT` and
  `CC_USAGE_ALERT_PCT` environment variables (defaults: 70 / 90). See the
  Configuration section in the README for a systemd override snippet.
- Dashboard now draws the warn-zone threshold line (in addition to the
  existing alert and limit lines) on both the weekly and 5-hour session
  charts, with a matching legend entry. The tray's amber state was
  previously invisible on the dashboard.
- `usage_monitor/thresholds.py` module exposing `WARN_PCT`, `ALERT_PCT`, and
  a `classify(pct)` helper, so every surface (tray, daemon, dashboard) reads
  thresholds from a single source of truth.
- `tests/test_thresholds.py` covering env-var overrides, invalid-value
  fallbacks, and state-classification boundaries.

### Changed
- Tray (`tray/usage-monitor-tray.py`), daemon (`usage_monitor/main.py`), and
  dashboard (`usage_monitor/dashboard.py`) now import thresholds from
  `usage_monitor.thresholds` instead of hardcoding `70` / `90` / `90.0`.
- Dashboard verdict text "✓ safe (under 90%)" is now driven by the
  configured alert threshold.

## [0.2.0] - 2026-04-27

### Added
- Zone-aware re-notification logic in `usage_monitor/notify_decision.py`:
  warn → re-notify when projection rises ≥1pp; alert → re-notify on any
  ≥1pp change in projection / week / session.
- Persistent notification state at `~/.claude/usage_monitor/notify_state.json`
  so tray restarts no longer re-fire stale notifications.
- ntfy v2.16+ `X-Sequence-ID` header for in-place message replacement on the
  phone subscriber.
- `tests/test_tray_notifications.py` (18 tests).

### Changed
- Tray escalation block replaced with `decide_notification(...)` from the new
  pure decision module.

## [0.1.0] - 2026-04-25

### Added
- Initial release: GTK3 + AppIndicator3 system tray for Claude Code's `/usage`
  numbers.
- Live week / session / projection percentages in the indicator label.
- HTML dashboard with weekly and 5-hour session SVG charts.
- Optional ntfy push notifications via `CLAUDE_USAGE_NTFY_URL`.
- systemd user units for the data daemon and the tray.

[Unreleased]: https://github.com/bradjohnl/cc-usage-tray/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/bradjohnl/cc-usage-tray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bradjohnl/cc-usage-tray/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bradjohnl/cc-usage-tray/releases/tag/v0.1.0
