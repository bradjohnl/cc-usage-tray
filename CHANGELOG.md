# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
