#!/bin/bash
# Wrapper for Claude Code's statusLine hook.
#
# Two jobs:
# 1. Capture the rate_limits JSON Claude Code passes via stdin and write it
#    atomically to ~/.claude/usage_monitor/rate_limits_cache.json. The tray
#    reads this for the same five_hour / seven_day usage % that /usage shows.
# 2. Forward the original JSON to the existing statusline script so the
#    terminal status bar keeps working exactly as before.
#
# Schema reference: https://code.claude.com/docs/en/statusline
set -u
CACHE_DIR="$HOME/.claude/usage_monitor"
CACHE_FILE="$CACHE_DIR/rate_limits_cache.json"
INNER="$HOME/.claude/statusline-command.sh"

mkdir -p "$CACHE_DIR"

input=$(cat)

# Tee rate_limits + minimal context to the cache (atomic via .tmp + mv).
# ONLY write when both rate-limit percentages are present, otherwise we'd
# blow away a good cache when an early statusLine event hasn't populated
# rate_limits yet. Failures must never block the status line.
if printf '%s' "$input" | jq -e '
    .rate_limits.five_hour.used_percentage != null and
    .rate_limits.seven_day.used_percentage != null
' >/dev/null 2>&1; then
    {
        printf '%s' "$input" | jq -c '
            {
                captured_at: now,
                session_id: (.session_id // null),
                rate_limits: .rate_limits,
                cost: (.cost // null),
                context_window: { used_percentage: (.context_window.used_percentage // null) }
            }
        ' > "$CACHE_FILE.tmp" 2>/dev/null && mv -f "$CACHE_FILE.tmp" "$CACHE_FILE"
    } || true
fi

# Delegate to the existing status-line renderer.
if [ -x "$INNER" ] || [ -f "$INNER" ]; then
    printf '%s' "$input" | bash "$INNER"
fi
