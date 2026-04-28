# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/bradjohnl/cc-usage-tray/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bradjohnl/cc-usage-tray/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bradjohnl/cc-usage-tray/releases/tag/v0.1.0
