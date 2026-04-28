"""Render a self-contained HTML dashboard from readings.jsonl.

Chart design: X-axis spans the full week (reset - 168h → reset). Y-axis fixed
0-100% (the plan ceiling). Horizontal threshold lines at the configured warn
and alert percentages plus a 100% limit line. Data points are a growing tail
inside the week window. A dashed line shows the anchored projection from
"now" to the reset.
"""
from datetime import datetime, timedelta
from html import escape
from typing import Sequence

from usage_monitor.projector import (
    project_final_pct,
    project_session_final_pct,
    rate_breakdown,
)
from usage_monitor.thresholds import ALERT_PCT, WARN_PCT

THRESHOLD_PCT = ALERT_PCT
WEEK_HOURS = 168
CHART_W = 720
CHART_H = 240
PAD_L = 44
PAD_R = 14
PAD_T = 14
PAD_B = 36


def _x_for(t: datetime, t_start: datetime, t_end: datetime) -> float:
    span = max((t_end - t_start).total_seconds(), 1)
    frac = (t - t_start).total_seconds() / span
    return PAD_L + (CHART_W - PAD_L - PAD_R) * frac


def _y_for(pct: float) -> float:
    frac = max(0.0, min(1.0, pct / 100.0))
    return PAD_T + (CHART_H - PAD_T - PAD_B) * (1 - frac)


def _chart_svg(readings: Sequence[dict], now: datetime, projected: float) -> str:
    if not readings:
        return ""
    last = readings[-1]
    reset = datetime.fromisoformat(last["week_all"]["reset_at"])
    week_start = reset - timedelta(hours=WEEK_HOURS)

    # Axes / grid
    parts = []
    # Y grid every 25%
    for pct in (0, 25, 50, 75, 100):
        y = _y_for(pct)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W-PAD_R}" y2="{y:.1f}" '
                     f'stroke="#262a33" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{pct}%</text>')

    # Threshold lines: warn, alert, 100% limit
    y_warn = _y_for(WARN_PCT)
    y_alert = _y_for(ALERT_PCT)
    y100 = _y_for(100)
    parts.append(f'<line x1="{PAD_L}" y1="{y_warn:.1f}" x2="{CHART_W-PAD_R}" y2="{y_warn:.1f}" '
                 f'stroke="#f1c40f" stroke-width="1" stroke-dasharray="4,3" opacity="0.55"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y_warn-4:.1f}" text-anchor="end" font-size="10" fill="#f1c40f">warn {WARN_PCT:g}%</text>')
    parts.append(f'<line x1="{PAD_L}" y1="{y_alert:.1f}" x2="{CHART_W-PAD_R}" y2="{y_alert:.1f}" '
                 f'stroke="#f39c12" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y_alert-4:.1f}" text-anchor="end" font-size="10" fill="#f39c12">alert {ALERT_PCT:g}%</text>')
    parts.append(f'<line x1="{PAD_L}" y1="{y100:.1f}" x2="{CHART_W-PAD_R}" y2="{y100:.1f}" '
                 f'stroke="#e74c3c" stroke-width="1" opacity="0.8"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y100-4:.1f}" text-anchor="end" font-size="10" fill="#e74c3c">limit 100%</text>')

    # X-axis: day labels for each day of the week window
    cur = week_start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while cur < reset:
        x = _x_for(cur, week_start, reset)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{CHART_H-PAD_B}" '
                     f'stroke="#1f2329" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="middle" '
                     f'font-size="10" fill="#666">{cur.strftime("%a")}</text>')
        cur += timedelta(days=1)
    # Start + end labels
    parts.append(f'<text x="{PAD_L:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="start" '
                 f'font-size="10" fill="#888">{week_start.strftime("%b %d")}</text>')
    parts.append(f'<text x="{CHART_W-PAD_R:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="end" '
                 f'font-size="10" fill="#888">reset {reset.strftime("%b %d %H:%M")}</text>')

    # Week % data path — from (week_start, 0%) through readings
    path_pts = [(_x_for(week_start, week_start, reset), _y_for(0))]
    for r in readings:
        t = datetime.fromisoformat(r["captured_at"])
        if t < week_start or t > reset:
            continue
        path_pts.append((_x_for(t, week_start, reset), _y_for(r["week_all"]["pct"])))
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts)
    parts.append(f'<polyline fill="none" stroke="#4ecdc4" stroke-width="2.5" points="{pts_str}"/>')

    # Projection line: from current point to (reset, projected)
    t_last = datetime.fromisoformat(last["captured_at"])
    x_last, y_last = _x_for(t_last, week_start, reset), _y_for(last["week_all"]["pct"])
    x_reset, y_reset = _x_for(reset, week_start, reset), _y_for(min(projected, 100))
    parts.append(f'<line x1="{x_last:.1f}" y1="{y_last:.1f}" x2="{x_reset:.1f}" y2="{y_reset:.1f}" '
                 f'stroke="#4ecdc4" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.6"/>')
    parts.append(f'<circle cx="{x_reset:.1f}" cy="{y_reset:.1f}" r="3" fill="#4ecdc4" opacity="0.6"/>')
    parts.append(f'<text x="{x_reset-6:.1f}" y="{y_reset-6:.1f}" text-anchor="end" '
                 f'font-size="10" fill="#4ecdc4">proj {projected:.0f}%</text>')

    # "Now" vertical marker
    x_now = _x_for(min(now, reset), week_start, reset)
    parts.append(f'<line x1="{x_now:.1f}" y1="{PAD_T}" x2="{x_now:.1f}" y2="{CHART_H-PAD_B}" '
                 f'stroke="#fff" stroke-width="1" stroke-dasharray="2,2" opacity="0.4"/>')
    parts.append(f'<text x="{x_now+4:.1f}" y="{PAD_T+10:.1f}" text-anchor="start" '
                 f'font-size="10" fill="#aaa">now</text>')

    return "\n".join(parts)


def _chart_svg_session(readings: Sequence[dict], now: datetime, projected: float | None) -> str:
    """Render the 5-hour session block chart (mirrors _chart_svg shape)."""
    if not readings:
        return ""
    # Find the most recent reading that has a session block — that defines the
    # current 5h window. Older readings may have None or a previous block.
    last_with_session = None
    for r in reversed(readings):
        if isinstance(r.get("session"), dict) and r["session"].get("reset_at"):
            last_with_session = r
            break
    if last_with_session is None:
        return ""
    reset = datetime.fromisoformat(last_with_session["session"]["reset_at"])
    block_start = reset - timedelta(hours=5)

    parts = []
    # Y grid every 25%
    for pct in (0, 25, 50, 75, 100):
        y = _y_for(pct)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W-PAD_R}" y2="{y:.1f}" '
                     f'stroke="#262a33" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{pct}%</text>')

    # Threshold lines: warn, alert, 100% limit
    y_warn = _y_for(WARN_PCT)
    y_alert = _y_for(ALERT_PCT)
    y100 = _y_for(100)
    parts.append(f'<line x1="{PAD_L}" y1="{y_warn:.1f}" x2="{CHART_W-PAD_R}" y2="{y_warn:.1f}" '
                 f'stroke="#f1c40f" stroke-width="1" stroke-dasharray="4,3" opacity="0.55"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y_warn-4:.1f}" text-anchor="end" font-size="10" fill="#f1c40f">warn {WARN_PCT:g}%</text>')
    parts.append(f'<line x1="{PAD_L}" y1="{y_alert:.1f}" x2="{CHART_W-PAD_R}" y2="{y_alert:.1f}" '
                 f'stroke="#f39c12" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y_alert-4:.1f}" text-anchor="end" font-size="10" fill="#f39c12">alert {ALERT_PCT:g}%</text>')
    parts.append(f'<line x1="{PAD_L}" y1="{y100:.1f}" x2="{CHART_W-PAD_R}" y2="{y100:.1f}" '
                 f'stroke="#e74c3c" stroke-width="1" opacity="0.8"/>')
    parts.append(f'<text x="{CHART_W-PAD_R-4}" y="{y100-4:.1f}" text-anchor="end" font-size="10" fill="#e74c3c">limit 100%</text>')

    # X-axis: hour ticks across the 5h block
    cur = block_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while cur < reset:
        x = _x_for(cur, block_start, reset)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{CHART_H-PAD_B}" '
                     f'stroke="#1f2329" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="middle" '
                     f'font-size="10" fill="#666">{cur.strftime("%H:%M")}</text>')
        cur += timedelta(hours=1)
    parts.append(f'<text x="{PAD_L:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="start" '
                 f'font-size="10" fill="#888">{block_start.strftime("%H:%M")}</text>')
    parts.append(f'<text x="{CHART_W-PAD_R:.1f}" y="{CHART_H-PAD_B+14:.1f}" text-anchor="end" '
                 f'font-size="10" fill="#888">reset {reset.strftime("%H:%M")}</text>')

    # Session % path — readings within the current block, with matching reset_at
    path_pts = [(_x_for(block_start, block_start, reset), _y_for(0))]
    for r in readings:
        sess = r.get("session")
        if not isinstance(sess, dict) or sess.get("reset_at") != last_with_session["session"]["reset_at"]:
            continue
        if sess.get("pct") is None:
            continue
        t = datetime.fromisoformat(r["captured_at"])
        if t < block_start or t > reset:
            continue
        path_pts.append((_x_for(t, block_start, reset), _y_for(sess["pct"])))
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts)
    parts.append(f'<polyline fill="none" stroke="#a78bfa" stroke-width="2.5" points="{pts_str}"/>')

    # Projection line for session
    if projected is not None and last_with_session["session"].get("pct") is not None:
        t_last = datetime.fromisoformat(last_with_session["captured_at"])
        x_last, y_last = _x_for(min(t_last, reset), block_start, reset), _y_for(last_with_session["session"]["pct"])
        x_reset, y_reset = _x_for(reset, block_start, reset), _y_for(min(projected, 100))
        parts.append(f'<line x1="{x_last:.1f}" y1="{y_last:.1f}" x2="{x_reset:.1f}" y2="{y_reset:.1f}" '
                     f'stroke="#a78bfa" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.6"/>')
        parts.append(f'<circle cx="{x_reset:.1f}" cy="{y_reset:.1f}" r="3" fill="#a78bfa" opacity="0.6"/>')
        parts.append(f'<text x="{x_reset-6:.1f}" y="{y_reset-6:.1f}" text-anchor="end" '
                     f'font-size="10" fill="#a78bfa">proj {projected:.0f}%</text>')

    # "Now" marker
    x_now = _x_for(min(now, reset), block_start, reset)
    parts.append(f'<line x1="{x_now:.1f}" y1="{PAD_T}" x2="{x_now:.1f}" y2="{CHART_H-PAD_B}" '
                 f'stroke="#fff" stroke-width="1" stroke-dasharray="2,2" opacity="0.4"/>')
    parts.append(f'<text x="{x_now+4:.1f}" y="{PAD_T+10:.1f}" text-anchor="start" '
                 f'font-size="10" fill="#aaa">now</text>')

    return "\n".join(parts)


def render_dashboard(readings: Sequence[dict], now: datetime) -> str:
    if not readings:
        body = '<div class="empty">No readings yet. Timer runs every 15 min.</div>'
    else:
        last = readings[-1]
        week_pct = last["week_all"]["pct"]
        session_block = last.get("session")
        session_pct = session_block["pct"] if session_block else None
        sonnet_pct = last["week_sonnet"]["pct"]
        reset_dt = datetime.fromisoformat(last["week_all"]["reset_at"])
        session_reset = (
            datetime.fromisoformat(session_block["reset_at"]) if session_block else None
        )
        hrs_to_reset = (reset_dt - now).total_seconds() / 3600

        proj = project_final_pct(readings[-24:], now=now)
        rb = rate_breakdown(readings[-24:], now=now)
        anchored = rb["anchored_rate_pct_per_h"]
        recent = rb["recent_rate_pct_per_h"]
        alerting = proj >= THRESHOLD_PCT or week_pct >= THRESHOLD_PCT
        verdict = "⚠ ON TRACK TO HIT LIMIT" if alerting else f"✓ safe (under {ALERT_PCT:g}%)"
        verdict_class = "alert" if alerting else "safe"

        chart = _chart_svg(readings, now=now, projected=proj)
        sess_proj = project_session_final_pct(last, now=now) if session_block else None
        chart_session = _chart_svg_session(readings, now=now, projected=sess_proj)
        recent_str = f"{recent:+.2f}%/h" if recent is not None else "n/a"

        body = f"""
        <div class="grid">
          <div class="card">
            <div class="label">Week (all models)</div>
            <div class="value">{week_pct}%</div>
            <div class="sub">Resets {escape(reset_dt.strftime('%a %Y-%m-%d %H:%M'))} · {hrs_to_reset:.1f}h from now</div>
          </div>
          <div class="card">
            <div class="label">Session (5h rolling)</div>
            <div class="value">{session_pct if session_pct is not None else '—'}{'%' if session_pct is not None else ''}</div>
            <div class="sub">{('Resets ' + escape(session_reset.strftime('%H:%M'))) if session_reset else 'Not active in probe'}</div>
          </div>
          <div class="card">
            <div class="label">Week (Sonnet only)</div>
            <div class="value">{sonnet_pct}%</div>
            <div class="sub">&nbsp;</div>
          </div>
          <div class="card {verdict_class}">
            <div class="label">Projected at reset</div>
            <div class="value">{proj:.0f}%</div>
            <div class="sub">{escape(verdict)}</div>
          </div>
        </div>

        <div class="rates">
          <span>Anchored rate (week-to-date): <strong>{anchored:+.2f}%/h</strong></span>
          <span>Recent rate: <strong>{escape(recent_str)}</strong></span>
          <span>Readings: <strong>{len(readings)}</strong></span>
        </div>

        <div class="chart-box">
          <div class="chart-title">Weekly usage across the current reset window</div>
          <svg viewBox="0 0 {CHART_W} {CHART_H}" width="100%" preserveAspectRatio="xMidYMid meet">
            {chart}
          </svg>
          <div class="legend">
            <span><i style="background:#4ecdc4"></i>Week %</span>
            <span><i class="dash" style="background:linear-gradient(to right,#4ecdc4 60%,transparent 60%)"></i>Projection to reset</span>
            <span><i style="background:#f1c40f"></i>Warn {WARN_PCT:g}%</span>
            <span><i style="background:#f39c12"></i>Alert {ALERT_PCT:g}%</span>
            <span><i style="background:#e74c3c"></i>Limit 100%</span>
          </div>
        </div>

        {f'''<div class="chart-box">
          <div class="chart-title">5-hour session block</div>
          <svg viewBox="0 0 {CHART_W} {CHART_H}" width="100%" preserveAspectRatio="xMidYMid meet">
            {chart_session}
          </svg>
          <div class="legend">
            <span><i style="background:#a78bfa"></i>Session %</span>
            <span><i class="dash" style="background:linear-gradient(to right,#a78bfa 60%,transparent 60%)"></i>Projection to reset</span>
            <span><i style="background:#f1c40f"></i>Warn {WARN_PCT:g}%</span>
            <span><i style="background:#f39c12"></i>Alert {ALERT_PCT:g}%</span>
            <span><i style="background:#e74c3c"></i>Limit 100%</span>
          </div>
        </div>''' if chart_session else ''}
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Claude Usage Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f1115; color: #e8e8e8; margin: 0; padding: 24px;
    min-height: 100vh;
  }}
  h1 {{ font-size: 1.1rem; font-weight: 500; margin: 0 0 4px 0; color: #fff; }}
  .ts {{ font-size: 0.78rem; color: #888; margin-bottom: 20px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }}
  .card {{
    background: #1a1d24; border: 1px solid #262a33; border-radius: 6px; padding: 14px;
  }}
  .card.safe {{ border-color: #27ae60; }}
  .card.alert {{ border-color: #e74c3c; background: #2a1a1a; }}
  .label {{ font-size: 0.72rem; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
  .value {{ font-size: 1.6rem; font-weight: 600; margin: 6px 0; color: #fff; }}
  .sub {{ font-size: 0.72rem; color: #999; }}
  .rates {{
    display: flex; gap: 20px; font-size: 0.85rem; color: #aaa;
    margin-bottom: 18px; flex-wrap: wrap;
  }}
  .rates strong {{ color: #fff; }}
  .chart-box {{
    background: #1a1d24; border: 1px solid #262a33; border-radius: 6px; padding: 14px;
  }}
  .chart-title {{ font-size: 0.78rem; color: #aaa; margin-bottom: 6px; }}
  .legend {{ display: flex; gap: 16px; font-size: 0.75rem; color: #aaa; margin-top: 8px; flex-wrap: wrap; }}
  .legend i {{
    display: inline-block; width: 14px; height: 10px; border-radius: 2px; margin-right: 6px;
    vertical-align: middle;
  }}
  .legend i.dash {{ background: repeating-linear-gradient(to right,#4ecdc4 0,#4ecdc4 4px,transparent 4px,transparent 7px) !important; }}
  .empty {{ text-align: center; padding: 60px; color: #666; }}
  footer {{ font-size: 0.7rem; color: #555; margin-top: 20px; text-align: center; }}
</style>
</head>
<body>
<h1>Claude Code Usage Dashboard</h1>
<div class="ts">Generated {escape(now.strftime('%Y-%m-%d %H:%M:%S %Z') or now.strftime('%Y-%m-%d %H:%M:%S'))}</div>
{body}
<footer>Updated every 15 min by claude-usage-monitor.timer · static snapshot · reopen to refresh</footer>
</body>
</html>
"""
    return html
