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

from usage_monitor import pause_state
from usage_monitor.config import load_config
from usage_monitor.projector import (
    auto_active_mask,
    project_all_strategies,
    project_final_pct,
    project_session_final_pct,
    rate_breakdown,
)
from usage_monitor.thresholds import ALERT_PCT, WARN_PCT

STRATEGY_LABELS = {
    "anchored": "Anchored (current/elapsed × remaining)",
    "active_hours": "Active hours window",
    "blend": "Blend (0.3·anchored + 0.7·history)",
    "dow_curve": "DoW curve (deviation from history)",
}

STRATEGY_COLORS = {
    "anchored":     "#4ecdc4",
    "active_hours": "#a78bfa",
    "blend":        "#f1c40f",
    "dow_curve":    "#e67e73",
}

CONTROL_BASE = "http://127.0.0.1:38734"

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


def _y_for(pct: float, y_max: float = 100.0) -> float:
    """Map pct to a y coordinate inside the plot area, using y_max as ceiling.

    Default y_max=100 keeps the original behavior for callers that don't care
    about overshoot (legend lines, thresholds, etc.). The weekly chart sets a
    larger y_max when projections exceed 100% so they render visibly above the
    100% limit line instead of getting clipped to it.
    """
    if y_max <= 0:
        y_max = 100.0
    frac = max(0.0, min(1.0, pct / y_max))
    return PAD_T + (CHART_H - PAD_T - PAD_B) * (1 - frac)


def _gridline_steps(y_max: float) -> list[int]:
    """Pick reasonable horizontal gridline values from 0 to y_max."""
    if y_max <= 100:
        return [0, 25, 50, 75, 100]
    if y_max <= 200:
        return [0, 50, 100, 150, 200]
    if y_max <= 400:
        return [0, 100, 200, 300, 400]
    step = int(((y_max + 99) // 5) // 100) * 100 or 100
    return list(range(0, int(y_max) + 1, step))


def _chart_svg(
    readings: Sequence[dict],
    now: datetime,
    projected: float,
    all_projections: dict[str, float] | None = None,
    active_strategy: str = "anchored",
) -> str:
    if not readings:
        return ""
    last = readings[-1]
    reset = datetime.fromisoformat(last["week_all"]["reset_at"])
    week_start = reset - timedelta(hours=WEEK_HOURS)

    # Auto-scale Y so projections that overshoot 100% are visible. Round up
    # to the next 50% step beyond max(week_pcts, all projections).
    candidates: list[float] = [r["week_all"]["pct"] for r in readings if "week_all" in r]
    if all_projections:
        candidates.extend(v for v in all_projections.values() if v is not None)
    else:
        candidates.append(projected)
    raw_max = max(candidates) if candidates else 100.0
    if raw_max <= 100:
        y_max = 100.0
    else:
        y_max = float(((int(raw_max) // 50) + 1) * 50)

    # Axes / grid
    parts = []
    for pct in _gridline_steps(y_max):
        y = _y_for(pct, y_max)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W-PAD_R}" y2="{y:.1f}" '
                     f'stroke="#262a33" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{pct}%</text>')

    # Threshold lines: warn, alert, 100% limit
    y_warn = _y_for(WARN_PCT, y_max)
    y_alert = _y_for(ALERT_PCT, y_max)
    y100 = _y_for(100, y_max)
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
    path_pts = [(_x_for(week_start, week_start, reset), _y_for(0, y_max))]
    for r in readings:
        t = datetime.fromisoformat(r["captured_at"])
        if t < week_start or t > reset:
            continue
        path_pts.append((_x_for(t, week_start, reset), _y_for(r["week_all"]["pct"], y_max)))
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts)
    parts.append(f'<polyline fill="none" stroke="#4ecdc4" stroke-width="2.5" points="{pts_str}"/>')

    # Projection lines: one per strategy, color-coded; alerting one is bold.
    t_last = datetime.fromisoformat(last["captured_at"])
    x_last = _x_for(t_last, week_start, reset)
    y_last = _y_for(last["week_all"]["pct"], y_max)
    x_reset = _x_for(reset, week_start, reset)
    proj_map = all_projections or {active_strategy: projected}
    # Draw faded lines first, then the active one on top.
    ordered = [s for s in proj_map if s != active_strategy] + [active_strategy]
    for s in ordered:
        v = proj_map.get(s)
        if v is None:
            continue
        color = STRATEGY_COLORS.get(s, "#4ecdc4")
        is_active = s == active_strategy
        opacity = "0.95" if is_active else "0.45"
        width = "2" if is_active else "1"
        y_reset = _y_for(v, y_max)  # no clipping — y_max grows to fit overshoot
        parts.append(f'<line x1="{x_last:.1f}" y1="{y_last:.1f}" x2="{x_reset:.1f}" y2="{y_reset:.1f}" '
                     f'stroke="{color}" stroke-width="{width}" stroke-dasharray="5,4" opacity="{opacity}"/>')
        parts.append(f'<circle cx="{x_reset:.1f}" cy="{y_reset:.1f}" r="{3 if is_active else 2}" fill="{color}" opacity="{opacity}"/>')
        if is_active:
            parts.append(f'<text x="{x_reset-6:.1f}" y="{y_reset-6:.1f}" text-anchor="end" '
                         f'font-size="11" fill="{color}" font-weight="600">{s} {v:.0f}%</text>')
        else:
            parts.append(f'<text x="{x_reset-6:.1f}" y="{y_reset+12:.1f}" text-anchor="end" '
                         f'font-size="9" fill="{color}" opacity="0.7">{s} {v:.0f}%</text>')

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

    # Auto-scale Y so a session projection that overshoots 100% is visible.
    sess_candidates: list[float] = []
    for r in readings:
        sess = r.get("session")
        if isinstance(sess, dict) and sess.get("reset_at") == last_with_session["session"]["reset_at"]:
            p = sess.get("pct")
            if isinstance(p, (int, float)):
                sess_candidates.append(float(p))
    if projected is not None:
        sess_candidates.append(float(projected))
    raw_max = max(sess_candidates) if sess_candidates else 100.0
    if raw_max <= 100:
        y_max = 100.0
    else:
        y_max = float(((int(raw_max) // 50) + 1) * 50)

    parts = []
    for pct in _gridline_steps(y_max):
        y = _y_for(pct, y_max)
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W-PAD_R}" y2="{y:.1f}" '
                     f'stroke="#262a33" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{pct}%</text>')

    # Threshold lines: warn, alert, 100% limit
    y_warn = _y_for(WARN_PCT, y_max)
    y_alert = _y_for(ALERT_PCT, y_max)
    y100 = _y_for(100, y_max)
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
    path_pts = [(_x_for(block_start, block_start, reset), _y_for(0, y_max))]
    for r in readings:
        sess = r.get("session")
        if not isinstance(sess, dict) or sess.get("reset_at") != last_with_session["session"]["reset_at"]:
            continue
        if sess.get("pct") is None:
            continue
        t = datetime.fromisoformat(r["captured_at"])
        if t < block_start or t > reset:
            continue
        path_pts.append((_x_for(t, block_start, reset), _y_for(sess["pct"], y_max)))
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts)
    parts.append(f'<polyline fill="none" stroke="#a78bfa" stroke-width="2.5" points="{pts_str}"/>')

    # Projection line for session
    if projected is not None and last_with_session["session"].get("pct") is not None:
        t_last = datetime.fromisoformat(last_with_session["captured_at"])
        x_last = _x_for(min(t_last, reset), block_start, reset)
        y_last = _y_for(last_with_session["session"]["pct"], y_max)
        x_reset = _x_for(reset, block_start, reset)
        y_reset = _y_for(projected, y_max)  # no clipping
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


_WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _active_mask_heatmap_svg(mask: set[tuple[int, int]]) -> str:
    """7×24 SVG grid; cells in mask get colored, others stay dim."""
    cell_w, cell_h = 22, 18
    pad_left = 36
    pad_top = 18
    width = pad_left + cell_w * 24 + 8
    height = pad_top + cell_h * 7 + 8
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" preserveAspectRatio="xMidYMid meet">']
    # Hour labels (every 3h to keep it readable)
    for h in range(0, 24, 3):
        x = pad_left + h * cell_w + cell_w / 2
        parts.append(f'<text x="{x:.1f}" y="{pad_top - 4}" text-anchor="middle" font-size="9" fill="#888">{h:02d}</text>')
    # Cells
    for wd in range(7):
        y = pad_top + wd * cell_h
        parts.append(f'<text x="{pad_left - 6}" y="{y + cell_h - 4}" text-anchor="end" font-size="10" fill="#aaa">{_WEEKDAYS_SHORT[wd]}</text>')
        for h in range(24):
            x = pad_left + h * cell_w
            in_mask = (wd, h) in mask
            fill = "#4ecdc4" if in_mask else "#262a33"
            opacity = "0.85" if in_mask else "0.5"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_w - 1}" height="{cell_h - 1}" '
                f'rx="2" fill="{fill}" opacity="{opacity}"><title>{_WEEKDAYS_SHORT[wd]} {h:02d}:00–{h+1:02d}:00 — {"active" if in_mask else "inactive"}</title></rect>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def _manual_mask_for_display(cfg: dict) -> set[tuple[int, int]]:
    """Mirror of projector._manual_mask, kept here to avoid a circular import."""
    s = int(cfg["active_hours"]["start"])
    e = int(cfg["active_hours"]["end"])
    weekdays_only = bool(cfg["active_hours"].get("weekdays_only", False))
    mask: set[tuple[int, int]] = set()
    for wd in range(7):
        if weekdays_only and wd >= 5:
            continue
        for h in range(24):
            if s <= h < e:
                mask.add((wd, h))
    return mask


def render_dashboard(readings: Sequence[dict], now: datetime) -> str:
    if not readings:
        body = '<div class="empty">No readings yet. Timer runs every 15 min.</div>'
    else:
        last = readings[-1]
        week_pct = last["week_all"]["pct"]
        session_block = last.get("session")
        session_pct = session_block["pct"] if session_block else None
        sonnet_block = last.get("week_sonnet")
        sonnet_pct = sonnet_block["pct"] if isinstance(sonnet_block, dict) else None
        reset_dt = datetime.fromisoformat(last["week_all"]["reset_at"])
        session_reset = (
            datetime.fromisoformat(session_block["reset_at"]) if session_block else None
        )
        hrs_to_reset = (reset_dt - now).total_seconds() / 3600

        cfg = load_config()
        active_strategy = cfg.get("projection_strategy", "anchored")
        all_projs = project_all_strategies(readings[-24:], now=now, config=cfg)
        proj = all_projs.get(active_strategy, project_final_pct(readings[-24:], now=now, config=cfg))
        rb = rate_breakdown(readings[-24:], now=now)
        anchored = rb["anchored_rate_pct_per_h"]
        recent = rb["recent_rate_pct_per_h"]
        alerting = proj >= THRESHOLD_PCT or week_pct >= THRESHOLD_PCT
        verdict = "⚠ ON TRACK TO HIT LIMIT" if alerting else f"✓ safe (under {ALERT_PCT:g}%)"
        verdict_class = "alert" if alerting else "safe"

        chart = _chart_svg(
            readings, now=now, projected=proj,
            all_projections=all_projs, active_strategy=active_strategy,
        )
        sess_proj = project_session_final_pct(last, now=now) if session_block else None
        chart_session = _chart_svg_session(readings, now=now, projected=sess_proj)
        recent_str = f"{recent:+.2f}%/h" if recent is not None else "n/a"

        # Strategy comparison rows: each shows projected pct, label, and whether
        # it's the active alerting strategy.
        strategy_rows = []
        for s in ("anchored", "active_hours", "blend", "dow_curve"):
            v = all_projs.get(s, 0.0)
            is_active = s == active_strategy
            badge = (
                '<span class="badge active">alerting</span>'
                if is_active
                else f'<a class="badge cc-control" href="{CONTROL_BASE}/set_strategy?name={s}" title="Switch alert strategy to {s} (via tray control server)">use for alerts</a>'
            )
            danger = "alert" if v >= ALERT_PCT else ("warn" if v >= WARN_PCT else "safe")
            color_dot = (
                f'<span class="color-dot" style="background:{STRATEGY_COLORS[s]}"></span>'
            )
            strategy_rows.append(
                f'<tr class="{ "active" if is_active else "" }">'
                f'<td class="name">{color_dot}{escape(STRATEGY_LABELS[s])}</td>'
                f'<td class="pct {danger}">{v:.0f}%</td>'
                f'<td class="action">{badge}</td>'
                f'</tr>'
            )
        strategy_table = "\n".join(strategy_rows)

        # Active-hours block: mode + mask + heatmap.
        ah = cfg["active_hours"]
        ah_mode = ah.get("mode", "manual")
        if ah_mode == "auto":
            mask = auto_active_mask(readings, cfg, now=now)
            if mask is None:
                ah_summary = "Auto mode (waiting for history — falling back to manual window)"
                heatmap_mask = _manual_mask_for_display(cfg)
            else:
                ah_summary = f"Auto mode: {len(mask)} active hours/week (learned from {len(readings)} readings)"
                heatmap_mask = mask
        else:
            wd_str = "weekdays only" if ah.get("weekdays_only") else "every day"
            ah_summary = f"Manual window: {ah['start']:02d}:00–{ah['end']:02d}:00 ({wd_str})"
            heatmap_mask = _manual_mask_for_display(cfg)
        heatmap_svg = _active_mask_heatmap_svg(heatmap_mask)

        # Pause banner (if any).
        active_pause = pause_state.load(now=now)
        pause_banner = ""
        if active_pause is not None:
            pause_banner = (
                f'<div class="pause-banner">'
                f'⏸ {escape(pause_state.describe(active_pause, now=now))}'
                f'</div>'
            )

        # JS that rewrites every cc-control link to carry a return_to URL
        # pointing at this very page — works regardless of file:// vs portal
        # vs http:// origin, as long as the browser can reach localhost.
        return_to_js = (
            "<script>"
            "  (function() {"
            "    var here = encodeURIComponent(location.href);"
            "    document.querySelectorAll('a.cc-control').forEach(function(a) {"
            "      var sep = a.href.indexOf('?') >= 0 ? '&' : '?';"
            "      a.href = a.href + sep + 'return_to=' + here;"
            "    });"
            "  })();"
            "</script>"
        )

        body = f"""
        {pause_banner}

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
            <div class="value">{f'{sonnet_pct}%' if sonnet_pct is not None else '—'}</div>
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

        <div class="chart-box strategies">
          <div class="chart-title">Projection by strategy — alerting strategy is highlighted</div>
          <table class="strategies-table">
            <thead><tr><th>Strategy</th><th>Projected %</th><th></th></tr></thead>
            <tbody>{strategy_table}</tbody>
          </table>
          <div class="hint">"use for alerts" links require the tray (control server on <code>{CONTROL_BASE}</code>). CLI: <code>usage-monitor-cli strategy &lt;name&gt;</code>.</div>
        </div>

        <div class="chart-box">
          <div class="chart-title">Active hours — drives the <code>active_hours</code> and <code>dow_curve</code> strategies</div>
          <div class="ah-summary">{escape(ah_summary)}
            <span class="ah-actions">
              <a class="badge cc-control {'active' if ah_mode == 'auto' else ''}" href="{CONTROL_BASE}/set_active_hours_mode?mode=auto">auto</a>
              <a class="badge cc-control {'active' if ah_mode == 'manual' else ''}" href="{CONTROL_BASE}/set_active_hours_mode?mode=manual">manual</a>
            </span>
          </div>
          {heatmap_svg}
          <div class="hint">CLI: <code>usage-monitor-cli active-hours [show|auto|manual|set --start H --end H]</code>.</div>
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
  .strategies {{ margin-bottom: 16px; }}
  .strategies-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .strategies-table th, .strategies-table td {{ padding: 8px 10px; border-bottom: 1px solid #262a33; text-align: left; }}
  .strategies-table th {{ font-size: 0.7rem; color: #888; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }}
  .strategies-table tr.active {{ background: #1f2730; }}
  .strategies-table td.pct {{ font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; width: 90px; }}
  .strategies-table td.pct.safe {{ color: #2ecc71; }}
  .strategies-table td.pct.warn {{ color: #f1c40f; }}
  .strategies-table td.pct.alert {{ color: #e74c3c; }}
  .strategies-table td.action {{ width: 110px; text-align: right; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.7rem; background: #262a33; color: #aaa; text-decoration: none; }}
  .badge.active {{ background: #2a5a3a; color: #b8e8c0; }}
  .badge:hover {{ background: #3a4a5a; color: #fff; }}
  .hint {{ font-size: 0.72rem; color: #777; margin-top: 8px; }}
  .hint code {{ background: #0f1115; padding: 1px 5px; border-radius: 3px; color: #b8d4ff; }}
  .pause-banner {{
    background: #1f2730; border-left: 3px solid #f1c40f; color: #f1c40f;
    padding: 10px 14px; border-radius: 4px; font-size: 0.85rem; margin-bottom: 14px;
  }}
  .ah-summary {{ font-size: 0.85rem; color: #ddd; margin: 4px 0 10px 0; display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .ah-actions {{ display: inline-flex; gap: 6px; }}
  .color-dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }}
  .empty {{ text-align: center; padding: 60px; color: #666; }}
  footer {{ font-size: 0.7rem; color: #555; margin-top: 20px; text-align: center; }}
</style>
</head>
<body>
<h1>Claude Code Usage Dashboard</h1>
<div class="ts">Generated {escape(now.strftime('%Y-%m-%d %H:%M:%S %Z') or now.strftime('%Y-%m-%d %H:%M:%S'))}</div>
{body}
<footer>Updated every 15 min by claude-usage-monitor.timer · static snapshot · reopen to refresh</footer>
{return_to_js if readings else ''}
</body>
</html>
"""
    return html
