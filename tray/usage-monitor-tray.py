#!/usr/bin/python3
"""GTK3 AppIndicator tray for claude-usage-monitor.

Requires system Python (not anaconda) for gi typelibs:
  /usr/bin/python3 with gir1.2-ayatanaappindicator3-0.1 installed.

Reads ~/.claude/usage_status.txt every 10s, updates icon + label.
Right-click menu: Open dashboard, Refresh now, Open status file, Quit.
"""
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Make the sibling usage_monitor package importable regardless of how the
# tray is launched (script path, systemd ExecStart, etc.).
_PKG_PARENT = Path(__file__).resolve().parent.parent
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from usage_monitor import pause_state  # noqa: E402
from usage_monitor.config import CONFIG_PATH, load_config  # noqa: E402
from usage_monitor.notify_decision import (  # noqa: E402
    decide_notification,
    pcts_from_status,
)
from usage_monitor.projector import STRATEGIES  # noqa: E402
from usage_monitor.thresholds import (  # noqa: E402
    ALERT_PCT,
    SESSION_ALERT_PCT,
    SESSION_WARN_PCT,
    WARN_PCT,
    classify,
    classify_session,
)

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

STATUS_FILE = Path.home() / ".claude" / "usage_status.txt"
DASHBOARD_FILE = Path.home() / ".claude" / "usage_monitor" / "dashboard.html"
ICON_DIR = Path.home() / ".claude" / "usage_monitor" / "icons"
NOTIFY_STATE_FILE = Path.home() / ".claude" / "usage_monitor" / "notify_state.json"
REFRESH_SECONDS = 10
SERVICE_NAME = "claude-usage-monitor.service"
STALE_AFTER_MINUTES = 10  # warn if last fresh scrape older than this
CONTROL_PORT = int(os.environ.get("CC_USAGE_TRAY_CONTROL_PORT", "38734"))
CONTROL_HOST = "127.0.0.1"

# State keys: 'safe' green, 'warn' amber, 'alert' red
_STATE_SAFE = "safe"
_STATE_WARN = "warn"
_STATE_ALERT = "alert"
_STATE_UNKNOWN = "unknown"

_RANK = {_STATE_SAFE: 0, _STATE_UNKNOWN: 0, _STATE_WARN: 1, _STATE_ALERT: 2}

# RGB 0-1 for circle fill
_COLORS = {
    _STATE_SAFE: (0.24, 0.73, 0.38),   # #3dbb61 green
    _STATE_WARN: (0.95, 0.69, 0.15),   # #f2b127 amber
    _STATE_ALERT: (0.91, 0.26, 0.26),  # #e84242 red
    _STATE_UNKNOWN: (0.55, 0.55, 0.55),  # grey
}


def _ensure_icons() -> None:
    """Create 22x22 PNG disks for each state (idempotent)."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    size = 22
    for state, (r, g, b) in _COLORS.items():
        path = ICON_DIR / f"claude-usage-{state}.png"
        if path.exists():
            continue
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surf)
        ctx.set_antialias(cairo.ANTIALIAS_BEST)
        # Filled disk with a subtle darker outline for contrast in both themes
        ctx.arc(size / 2, size / 2, size / 2 - 2, 0, 2 * 3.14159)
        ctx.set_source_rgb(r, g, b)
        ctx.fill_preserve()
        ctx.set_source_rgba(0, 0, 0, 0.35)
        ctx.set_line_width(1.5)
        ctx.stroke()
        surf.write_to_png(str(path))


def _icon_path(state: str) -> str:
    return str(ICON_DIR / f"claude-usage-{state}.png")


def parse_status(text: str) -> dict:
    """Extract week%, session (optional), proj%, verdict from status line."""
    text = text.strip()
    m = re.search(r"week\s+(\d+)%.*?proj\s+(\d+)%", text, re.IGNORECASE)
    if not m:
        return {"raw": text}
    out = {
        "week_pct": int(m.group(1)),
        "proj_pct": int(m.group(2)),
        "alerting": text.startswith("🚨"),
    }
    # Session may be a percent ('session 18%'), 'n/a', token-based
    # ('sess 18.4M tok · $11 ends 04:00'), or percent-with-projection
    # ('sess 8% → proj 20% ends 20:00')
    sess_pct_m = re.search(
        r"sess(?:ion)?\s+(\d+)%(?:\s*→\s*proj\s+(\d+)%)?(?:\s+ends\s+([\d:]+))?",
        text, re.IGNORECASE,
    )
    if sess_pct_m:
        out["session_pct"] = int(sess_pct_m.group(1))
        if sess_pct_m.group(2):
            out["session_proj_pct"] = int(sess_pct_m.group(2))
        if sess_pct_m.group(3):
            out["session_ends"] = sess_pct_m.group(3)
    elif re.search(r"session\s+n/a", text, re.IGNORECASE):
        out["session_pct"] = None
    sess_tok_m = re.search(
        r"sess\s+([\d.]+[MkK])\s+tok(?:\s*·\s*\$([\d.]+))?(?:\s+ends\s+([\d:]+))?",
        text, re.IGNORECASE,
    )
    if sess_tok_m:
        out["session_tokens_str"] = sess_tok_m.group(1)
        out["session_cost_usd"] = sess_tok_m.group(2)
        out.setdefault("session_ends", sess_tok_m.group(3))
    rate_m = re.search(r"\+(\d+\.\d+)%/h", text)
    if rate_m:
        out["rate_per_h"] = float(rate_m.group(1))
    reset_m = re.search(r"by\s+(\S+\s+\d+:\d+)", text)
    if reset_m:
        out["reset_label"] = reset_m.group(1)
    verdict_m = re.search(r"\|\s*(safe|ON TRACK TO HIT LIMIT)\b", text, re.IGNORECASE)
    if verdict_m:
        out["verdict"] = verdict_m.group(1)
    last_m = re.search(r"\blast\s+(\d{1,2}:\d{2})\b", text, re.IGNORECASE)
    if last_m:
        out["last_fresh"] = last_m.group(1)
    return out


def pick_state(s: dict) -> str:
    """Combine weekly + session classifications; worst zone wins.

    Weekly uses CC_USAGE_WARN_PCT/ALERT_PCT, session uses
    CC_USAGE_SESSION_WARN_PCT/ALERT_PCT — the two are independent so a user
    can alert earlier on the 5h block without changing the weekly thresholds.
    """
    if not s or "proj_pct" not in s:
        return _STATE_UNKNOWN
    week_pcts = [s["proj_pct"], s["week_pct"]]
    week_alert = s.get("alerting") or max(week_pcts) >= ALERT_PCT
    week_warn = max(week_pcts) >= WARN_PCT
    sess = s.get("session_pct")
    sess_alert = isinstance(sess, int) and sess >= SESSION_ALERT_PCT
    sess_warn = isinstance(sess, int) and sess >= SESSION_WARN_PCT
    if week_alert or sess_alert:
        return _STATE_ALERT
    if week_warn or sess_warn:
        return _STATE_WARN
    return _STATE_SAFE


def _minutes_since_last_fresh(s: dict, now: datetime | None = None) -> int | None:
    """Return minutes since last fresh scrape, or None if unknown."""
    last_str = s.get("last_fresh")
    if not last_str:
        return None
    try:
        h, m = map(int, last_str.split(":"))
    except ValueError:
        return None
    now = now or datetime.now()
    last_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if last_dt > now:
        last_dt -= timedelta(days=1)
    return int((now - last_dt).total_seconds() / 60)


def _pct_markup(pct, state_hint: str) -> str:
    """Color-coded percent for menu markup. pct may be None."""
    if pct is None:
        return "<span foreground='#888'>n/a</span>"
    colors = {"safe": "#3dbb61", "warn": "#f2b127", "alert": "#e84242"}
    c = colors.get(state_hint, "#ddd")
    return f"<span foreground='{c}'><b>{pct}%</b></span>"


def send_ntfy(title: str, body: str, priority: str = "default",
              replace_id: str | None = None) -> str | None:
    """Publish to ntfy and return the message id from the response.

    If replace_id is given, ntfy >= v2.16 will UPDATE the existing message
    in clients instead of stacking a new notification (X-Sequence-ID).
    Returns None when ntfy is disabled (env unset) or on any error.
    """
    url = os.environ.get("CLAUDE_USAGE_NTFY_URL", "")
    topic = os.environ.get("CLAUDE_USAGE_NTFY_TOPIC", "")
    if not url or not topic:
        return None  # ntfy disabled when either env var unset
    cmd = ["curl", "-s", "--max-time", "5", "-X", "POST",
           f"{url}/{topic}",
           "-H", f"Title: {title}",
           "-H", f"Priority: {priority}"]
    if replace_id:
        cmd.extend(["-H", f"X-Sequence-ID: {replace_id}"])
    cmd.extend(["-d", body])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return json.loads(result.stdout).get("id")
    except (json.JSONDecodeError, AttributeError):
        return None


def is_escalation(prev: str, curr: str) -> bool:
    return _RANK.get(curr, 0) > _RANK.get(prev, 0)


def _load_notify_state() -> tuple[str, dict, str | None]:
    try:
        data = json.loads(NOTIFY_STATE_FILE.read_text())
        state = data.get("state", _STATE_SAFE)
        pcts = data.get("pcts") or {"proj": None, "week": None, "session": None}
        ntfy_id = data.get("ntfy_id")
        return state, pcts, ntfy_id
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _STATE_SAFE, {"proj": None, "week": None, "session": None}, None


def _save_notify_state(state: str, pcts: dict, ntfy_id: str | None) -> None:
    try:
        NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTIFY_STATE_FILE.write_text(json.dumps(
            {"state": state, "pcts": pcts, "ntfy_id": ntfy_id},
            separators=(",", ":"),
        ))
    except OSError:
        pass


class UsageTray:
    def __init__(self):
        _ensure_icons()
        self.indicator = AppIndicator3.Indicator.new(
            "claude-usage-monitor",
            _icon_path(_STATE_UNKNOWN),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)
        self._last_state, self._last_pcts, self._last_ntfy_id = _load_notify_state()
        self._update()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._update)

    def _markup_label(self, text: str, sensitive: bool = False) -> Gtk.MenuItem:
        lbl = Gtk.Label()
        lbl.set_markup(text)
        lbl.set_xalign(0.0)
        item = Gtk.MenuItem()
        item.add(lbl)
        item.set_sensitive(sensitive)
        return item

    def _build_menu(self):
        self.menu_week = self._markup_label("Week (all): loading…")
        self.menu_sonnet = self._markup_label("Sonnet week: —")
        self.menu_session = self._markup_label("Session (5h): —")
        self.menu_proj = self._markup_label("Projected: —")
        self.menu_rate = self._markup_label("Rate: —")
        self.menu_verdict = self._markup_label("—")
        self.menu_freshness = self._markup_label("—")
        self.menu_pause_status = self._markup_label("")
        self.menu_pause_status.hide()

        for mi in (self.menu_week, self.menu_sonnet, self.menu_session,
                   self.menu_proj, self.menu_rate, self.menu_verdict,
                   self.menu_freshness, self.menu_pause_status):
            self.menu.append(mi)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Strategy submenu — switch which projection drives alerts.
        strategy_item = Gtk.MenuItem(label="Alert strategy")
        strategy_submenu = Gtk.Menu()
        self._strategy_items: dict[str, Gtk.RadioMenuItem] = {}
        group = None
        for s in STRATEGIES:
            label = {
                "anchored": "Anchored (rate × remaining)",
                "active_hours": "Active hours window",
                "blend": "Blend with history",
                "dow_curve": "Day-of-week deviation",
            }.get(s, s)
            radio = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            if group is None:
                group = radio
            radio.connect("toggled", self._on_strategy_toggled, s)
            self._strategy_items[s] = radio
            strategy_submenu.append(radio)
        strategy_item.set_submenu(strategy_submenu)
        self.menu.append(strategy_item)

        # Active-hours submenu — auto/manual + current window display.
        active_item = Gtk.MenuItem(label="Active hours")
        active_submenu = Gtk.Menu()
        self.menu_active_summary = self._markup_label("…", sensitive=False)
        active_submenu.append(self.menu_active_summary)
        active_submenu.append(Gtk.SeparatorMenuItem())
        self._active_mode_items: dict[str, Gtk.RadioMenuItem] = {}
        ah_group = None
        for mode in ("auto", "manual"):
            label = "Auto-detect from history" if mode == "auto" else "Manual window"
            radio = Gtk.RadioMenuItem.new_with_label_from_widget(ah_group, label)
            if ah_group is None:
                ah_group = radio
            radio.connect("toggled", self._on_active_mode_toggled, mode)
            self._active_mode_items[mode] = radio
            active_submenu.append(radio)
        active_item.set_submenu(active_submenu)
        self.menu.append(active_item)

        # Pause submenu.
        pause_item = Gtk.MenuItem(label="Pause alerts")
        pause_submenu = Gtk.Menu()
        for label, kind in [
            ("Until session reset (5h)", "session"),
            ("Until weekly reset", "weekly"),
            ("For 1 hour", "1h"),
            ("For 4 hours", "4h"),
        ]:
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", self._on_pause_clicked, kind)
            pause_submenu.append(mi)
        pause_item.set_submenu(pause_submenu)
        self.menu.append(pause_item)

        self.menu_resume = Gtk.MenuItem(label="Resume alerts")
        self.menu_resume.connect("activate", self._on_resume_clicked)
        self.menu.append(self.menu_resume)
        self.menu_resume.hide()

        self.menu.append(Gtk.SeparatorMenuItem())

        open_dashboard = Gtk.MenuItem(label="Open dashboard")
        open_dashboard.connect("activate", self._open_dashboard)
        self.menu.append(open_dashboard)

        refresh_now = Gtk.MenuItem(label="Refresh now")
        refresh_now.connect("activate", self._refresh_now)
        self.menu.append(refresh_now)

        open_status = Gtk.MenuItem(label="Open status file")
        open_status.connect("activate", self._open_status_file)
        self.menu.append(open_status)

        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit tray")
        quit_item.connect("activate", Gtk.main_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        # Honor initial config.
        self._sync_strategy_radio()
        self._sync_active_hours_summary()

    def _set_menu_markup(self, item: Gtk.MenuItem, markup: str) -> None:
        child = item.get_child()
        if isinstance(child, Gtk.Label):
            child.set_markup(markup)

    def _read_status(self) -> str:
        try:
            return STATUS_FILE.read_text().strip()
        except FileNotFoundError:
            return ""

    def _update(self):
        raw = self._read_status()
        s = parse_status(raw) if raw else {}
        state = pick_state(s)

        curr_pcts = pcts_from_status(s)
        # Auto-pause when a hard limit is hit. Also picks up the scraper's pause.
        week_pct = s.get("week_pct")
        sess_pct = s.get("session_pct") if isinstance(s.get("session_pct"), int) else None
        week_reset_dt = self._parse_reset_label(s.get("reset_label", ""), datetime.now()) if s.get("reset_label") else None
        sess_reset_dt = None
        if s.get("session_ends"):
            try:
                h, m = map(int, s["session_ends"].split(":"))
                cand = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if cand <= datetime.now():
                    cand += timedelta(days=1)
                sess_reset_dt = cand
            except ValueError:
                sess_reset_dt = None
        pause_state.auto_pause_for_limit(
            week_pct=week_pct, session_pct=sess_pct,
            week_reset_at=week_reset_dt, session_reset_at=sess_reset_dt,
        )
        active_pause = pause_state.load()
        # Reflect pause in menu (visibility + label).
        if active_pause is not None:
            self._set_menu_markup(
                self.menu_pause_status,
                f"<span foreground='#888'><i>{GLib.markup_escape_text(pause_state.describe(active_pause))}</i></span>",
            )
            self.menu_pause_status.show()
            self.menu_resume.show()
        else:
            self.menu_pause_status.hide()
            self.menu_resume.hide()
        self._sync_strategy_radio()
        self._sync_active_hours_summary()

        notif = decide_notification(
            self._last_state, state, self._last_pcts, curr_pcts
        )
        if notif is not None and active_pause is not None:
            notif = None  # suppressed by pause
        if notif is not None:
            sess = s.get("session_pct")
            sess_part = f" · sess {sess}%" if isinstance(sess, int) else ""
            body = (
                f"wk {s.get('week_pct', '?')}% → proj {s.get('proj_pct', '?')}% "
                f"by {s.get('reset_label', '?')}{sess_part}"
            )
            urgency_map = {"urgent": "critical", "default": "normal", "low": "low"}
            timeout_map = {"urgent": "0", "default": "15000", "low": "8000"}
            subprocess.Popen([
                "notify-send",
                "-u", urgency_map.get(notif.severity, "normal"),
                "-t", timeout_map.get(notif.severity, "15000"),
                notif.title, body,
            ])
            new_id = send_ntfy(
                notif.title, body, notif.severity,
                replace_id=self._last_ntfy_id,
            )
            if new_id:
                self._last_ntfy_id = new_id
        if state != _STATE_UNKNOWN:
            self._last_state = state
            self._last_pcts = curr_pcts
            _save_notify_state(state, curr_pcts, self._last_ntfy_id)

        self.indicator.set_icon_full(_icon_path(state), f"Claude usage: {state}")

        if "week_pct" in s:
            stale_min = _minutes_since_last_fresh(s)
            is_stale = stale_min is not None and stale_min > STALE_AFTER_MINUTES
            # Concise tray label: e.g. "24% → 51%", with ⚠ prefix if stale
            warn_prefix = "\u26a0 " if is_stale else ""
            sess_label = s.get("session_pct")
            sess_part = f" \u00b7 {sess_label}%s" if isinstance(sess_label, int) else ""
            self.indicator.set_label(
                f"{warn_prefix}{s['week_pct']}% → {s['proj_pct']}%{sess_part}",
                "99% → 99% \u00b7 99%s",
            )
            # Override icon to grey "unknown" disk when data is stale
            if is_stale:
                self.indicator.set_icon_full(
                    _icon_path(_STATE_UNKNOWN), "Claude usage: STALE"
                )
            self._set_menu_markup(
                self.menu_week,
                f"<b>Week (all models):</b>  {_pct_markup(s['week_pct'], state)}",
            )
            self._set_menu_markup(
                self.menu_sonnet,
                f"<b>Sonnet week:</b>  —"
                if "sonnet_pct" not in s
                else f"<b>Sonnet week:</b>  <b>{s['sonnet_pct']}%</b>",
            )
            sess_pct = s.get("session_pct")
            if isinstance(sess_pct, int):
                # Color-code session by its own thresholds
                sess_state = classify(sess_pct)
                proj = s.get("session_proj_pct")
                ends = s.get("session_ends")
                proj_part = f"  →  {_pct_markup(proj, sess_state)}" if isinstance(proj, int) else ""
                ends_part = f"  <span foreground='#888'>ends {ends}</span>" if ends else ""
                sess_markup = f"{_pct_markup(sess_pct, sess_state)}{proj_part}{ends_part}"
            elif "session_tokens_str" in s:
                tok = s["session_tokens_str"]
                cost = s.get("session_cost_usd")
                ends = s.get("session_ends")
                cost_str = f" · <b>${cost}</b>" if cost else ""
                ends_str = f"  <span foreground='#888'>ends {ends}</span>" if ends else ""
                sess_markup = f"<b>{tok} tok</b>{cost_str}{ends_str}"
            else:
                sess_markup = _pct_markup(None, 'safe')
            self._set_menu_markup(
                self.menu_session,
                f"<b>Session (5h):</b>  {sess_markup}",
            )
            reset_str = s.get("reset_label", "?")
            self._set_menu_markup(
                self.menu_proj,
                f"<b>Projected at reset:</b>  {_pct_markup(s['proj_pct'], state)}"
                f"  <span foreground='#888'>@ {reset_str}</span>",
            )
            rate = s.get("rate_per_h")
            self._set_menu_markup(
                self.menu_rate,
                f"<b>Rate:</b>  " + (f"+{rate:.2f}%/h" if rate is not None else "—"),
            )
            verdict = s.get("verdict", "")
            vcolor = "#e84242" if state == _STATE_ALERT else ("#f2b127" if state == _STATE_WARN else "#3dbb61")
            vtext = "\u26a0\ufe0f ON TRACK TO HIT LIMIT" if state == _STATE_ALERT else ("\u26a0\ufe0f Approaching limit" if state == _STATE_WARN else "\u2713 Safe")
            self._set_menu_markup(
                self.menu_verdict,
                f"<span foreground='{vcolor}'><b>{vtext}</b></span>",
            )
            # Freshness line — last successful scrape time + age
            last_str = s.get("last_fresh")
            if last_str is not None and stale_min is not None:
                age_str = f"{stale_min}m ago" if stale_min < 60 else f"{stale_min // 60}h {stale_min % 60}m ago"
                if is_stale:
                    fresh_markup = (
                        f"<span foreground='#e84242'><b>\u26a0 Last fresh data:</b> "
                        f"{GLib.markup_escape_text(last_str)} ({age_str})</span>"
                    )
                else:
                    fresh_markup = (
                        f"<span foreground='#888'>Last fresh data: "
                        f"{GLib.markup_escape_text(last_str)} ({age_str})</span>"
                    )
            else:
                fresh_markup = "<span foreground='#888'>Last fresh data: unknown</span>"
            self._set_menu_markup(self.menu_freshness, fresh_markup)
        elif raw:
            self.indicator.set_label("…", "")
            self._set_menu_markup(self.menu_week, f"<i>{GLib.markup_escape_text(raw)}</i>")
        else:
            self.indicator.set_label("no data", "")
            self._set_menu_markup(self.menu_week, "<i>No status file — waiting for daemon</i>")
        return True  # keep the timeout alive

    # ---------- strategy / pause handlers ----------

    def _sync_strategy_radio(self) -> None:
        try:
            cfg = load_config()
        except Exception:
            return
        active = cfg.get("projection_strategy", "anchored")
        radio = self._strategy_items.get(active)
        if radio is not None and not radio.get_active():
            radio.handler_block_by_func(self._on_strategy_toggled)
            radio.set_active(True)
            radio.handler_unblock_by_func(self._on_strategy_toggled)

    def _sync_active_hours_summary(self) -> None:
        try:
            cfg = load_config()
        except Exception:
            return
        ah = cfg["active_hours"]
        mode = ah.get("mode", "manual")
        # Sync mode radios.
        radio = self._active_mode_items.get(mode)
        if radio is not None and not radio.get_active():
            radio.handler_block_by_func(self._on_active_mode_toggled)
            radio.set_active(True)
            radio.handler_unblock_by_func(self._on_active_mode_toggled)
        # Build the summary line.
        if mode == "manual":
            summary = (
                f"Manual: {ah['start']:02d}:00–{ah['end']:02d}:00 "
                f"({'weekdays' if ah.get('weekdays_only') else 'every day'})"
            )
        else:
            try:
                from usage_monitor.projector import auto_active_mask
                readings = []
                rp = Path.home() / ".claude" / "usage_monitor" / "readings.jsonl"
                if rp.exists():
                    for line in rp.read_text().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            readings.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                mask = auto_active_mask(readings, cfg) if readings else None
                if mask is None:
                    summary = "Auto: <waiting for history…>"
                else:
                    summary = f"Auto: {len(mask)} active hours/week"
            except Exception:
                summary = "Auto: (unavailable)"
        self._set_menu_markup(
            self.menu_active_summary,
            f"<span foreground='#888'><i>{GLib.markup_escape_text(summary)}</i></span>",
        )

    def _on_active_mode_toggled(self, item: Gtk.RadioMenuItem, mode: str) -> None:
        if not item.get_active():
            return
        try:
            cfg = load_config()
            cfg["active_hours"]["mode"] = mode
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        except Exception as e:
            subprocess.Popen(["notify-send", "Claude usage monitor", f"Active-hours switch failed: {e}"])
            return
        subprocess.Popen([
            "notify-send", "-t", "3000",
            "Claude usage monitor",
            f"Active-hours mode → {mode}. Refreshing…",
        ])
        subprocess.Popen(["systemctl", "--user", "start", SERVICE_NAME])
        self._sync_active_hours_summary()

    def _on_strategy_toggled(self, item: Gtk.RadioMenuItem, strategy: str) -> None:
        if not item.get_active():
            return
        try:
            cfg = load_config()
            cfg["projection_strategy"] = strategy
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        except Exception as e:
            subprocess.Popen(["notify-send", "Claude usage monitor", f"Strategy switch failed: {e}"])
            return
        subprocess.Popen([
            "notify-send", "-t", "3000",
            "Claude usage monitor",
            f"Alert strategy → {strategy}. Refreshing…",
        ])
        subprocess.Popen(["systemctl", "--user", "start", SERVICE_NAME])

    def _on_pause_clicked(self, _widget, kind: str) -> None:
        now = datetime.now()
        until: datetime | None = None
        reason = pause_state.REASON_MANUAL
        s = parse_status(self._read_status())
        if kind == "session":
            ends = s.get("session_ends")
            if ends:
                try:
                    h, m = map(int, ends.split(":"))
                    until = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if until <= now:
                        until += timedelta(days=1)
                except ValueError:
                    until = now + timedelta(hours=5)
            else:
                until = now + timedelta(hours=5)
        elif kind == "weekly":
            label = s.get("reset_label")  # e.g. "Tue 15:00"
            if label:
                until = self._parse_reset_label(label, now) or (now + timedelta(days=7))
            else:
                until = now + timedelta(days=7)
        elif kind == "1h":
            until = now + timedelta(hours=1)
        elif kind == "4h":
            until = now + timedelta(hours=4)
        if until is None:
            return
        pause_state.save(pause_state.Pause(until, reason, manual=True))
        subprocess.Popen([
            "notify-send", "-t", "3000",
            "Claude usage monitor",
            f"Alerts paused until {until:%a %H:%M}.",
        ])
        self._update()

    def _on_resume_clicked(self, _widget) -> None:
        pause_state.clear()
        subprocess.Popen([
            "notify-send", "-t", "3000",
            "Claude usage monitor",
            "Alerts resumed.",
        ])
        self._update()

    @staticmethod
    def _parse_reset_label(label: str, now: datetime) -> datetime | None:
        # Accepts "Tue 15:00" — find the next matching weekday/time.
        parts = label.strip().split()
        if len(parts) != 2:
            return None
        day_str, time_str = parts
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        try:
            target_wd = weekdays.index(day_str[:3])
            h, m = map(int, time_str.split(":"))
        except (ValueError, IndexError):
            return None
        days_ahead = (target_wd - now.weekday()) % 7
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    def _open_dashboard(self, _widget):
        # Prefer the localhost URL so the dashboard is same-origin with the
        # control endpoints (no Flatpak portal file:// proxying). Fall back
        # to the static file if the control server isn't bound.
        url = f"http://127.0.0.1:{CONTROL_PORT}/dashboard"
        try:
            import socket
            with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=0.5):
                pass
            subprocess.Popen(["xdg-open", url])
            return
        except OSError:
            pass
        if DASHBOARD_FILE.exists():
            subprocess.Popen(["xdg-open", str(DASHBOARD_FILE)])
        else:
            subprocess.Popen([
                "notify-send",
                "Claude usage monitor",
                "Dashboard not yet generated. Click 'Refresh now' first.",
            ])

    def _refresh_now(self, _widget):
        subprocess.Popen(["systemctl", "--user", "start", SERVICE_NAME])
        subprocess.Popen([
            "notify-send", "-t", "3000",
            "Claude usage monitor",
            "Triggered a fresh scrape. Status will update in ~15 seconds.",
        ])

    def _open_status_file(self, _widget):
        subprocess.Popen(["xdg-open", str(STATUS_FILE)])


_DASHBOARD_PATH = Path.home() / ".claude" / "usage_monitor" / "dashboard.html"
_READINGS_PATH = Path.home() / ".claude" / "usage_monitor" / "readings.jsonl"


def _render_dashboard_now() -> None:
    """Refresh status file + dashboard.html in-process from readings.jsonl.

    Bypasses the systemd monitor unit so the control endpoint stays
    responsive even when ccusage hangs. Also bypasses ntfy / notify-send
    side effects (those still flow through the periodic timer-driven
    monitor run).
    """
    readings: list[dict] = []
    if _READINGS_PATH.exists():
        try:
            for line in _READINGS_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    readings.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
    if not readings:
        return
    try:
        from usage_monitor.main import refresh_outputs
        refresh_outputs(readings)
    except Exception as e:
        sys.stderr.write(f"[tray] refresh_outputs failed: {e}\n")


def _trigger_refresh_blocking(max_wait_s: float = 0.0) -> None:
    """Compatibility shim — kept so existing call sites still work."""
    _render_dashboard_now()


class _ControlHandler(BaseHTTPRequestHandler):
    """Localhost-only handler so the static dashboard can switch settings.

    Routes:
      GET /set_strategy?name=<one of STRATEGIES>
      GET /set_active_hours_mode?mode=auto|manual
      GET /pause?kind=session|weekly|<duration>
      GET /resume
    Responses are 200 with a tiny HTML page that calls history.back() so the
    user lands back on the dashboard.
    """

    def log_message(self, format, *args):  # noqa: A002 — silence default access log
        return

    def _ok(self, msg: str = "OK", return_to: str | None = None) -> None:
        # Prefer the explicit return_to passed by the dashboard JS — it
        # captures the actual URL the browser used to load dashboard.html
        # (which may be a Flatpak xdg-document-portal proxy path like
        # file:///run/user/1000/doc/<hash>/dashboard.html, or whatever).
        if return_to:
            target = return_to.replace("\\", "").replace('"', "")
            target_js = f'location.replace("{target}")'
        else:
            target_js = (
                'if (document.referrer) { location.replace(document.referrer); } '
                'else { history.back(); }'
            )
        body = (
            f"<!DOCTYPE html><meta charset='utf-8'>"
            f"<title>{msg}</title>"
            f"<body style='font-family:sans-serif;background:#0f1115;color:#ddd;padding:24px'>"
            f"{msg}. Reloading dashboard…"
            f"<script>{target_js}</script>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _bad(self, msg: str) -> None:
        self.send_response(400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode("utf-8"))

    def do_GET(self):  # noqa: N802 — http.server convention
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_response(403)
            self.end_headers()
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        return_to = (qs.get("return_to") or [None])[0]
        # Serve the dashboard itself so links can be same-origin and the
        # Flatpak/portal file:// quirks go away. Always re-renders fresh.
        if url.path in ("/", "/dashboard", "/dashboard.html"):
            try:
                _render_dashboard_now()
                html = _DASHBOARD_PATH.read_text()
            except Exception as e:
                html = f"<pre>render failed: {e}</pre>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return
        try:
            if url.path == "/set_strategy":
                name = (qs.get("name") or [""])[0]
                if name not in STRATEGIES:
                    return self._bad(f"unknown strategy {name!r}")
                cfg = load_config()
                cfg["projection_strategy"] = name
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
                _trigger_refresh_blocking()
                return self._ok(f"Strategy → {name}", return_to)
            if url.path == "/set_active_hours_mode":
                mode = (qs.get("mode") or [""])[0]
                if mode not in ("auto", "manual"):
                    return self._bad(f"unknown mode {mode!r}")
                cfg = load_config()
                cfg["active_hours"]["mode"] = mode
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
                _trigger_refresh_blocking()
                return self._ok(f"Active hours → {mode}", return_to)
            if url.path == "/pause":
                kind = (qs.get("kind") or ["session"])[0]
                now = datetime.now()
                until = None
                if kind == "session":
                    until = now + timedelta(hours=5)
                elif kind == "weekly":
                    until = now + timedelta(days=7)
                else:
                    m = re.match(r"^(\d+)([smhd])$", kind)
                    if m:
                        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
                        until = now + timedelta(seconds=int(m.group(1)) * units[m.group(2)])
                if until is None:
                    return self._bad(f"bad pause kind {kind!r}")
                pause_state.save(pause_state.Pause(until, pause_state.REASON_MANUAL, manual=True))
                _trigger_refresh_blocking()
                return self._ok(f"Paused until {until:%a %H:%M}", return_to)
            if url.path == "/resume":
                pause_state.clear()
                _trigger_refresh_blocking()
                return self._ok("Resumed", return_to)
        except Exception as e:
            return self._bad(f"server error: {e}")
        self.send_response(404)
        self.end_headers()


def _start_control_server() -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), _ControlHandler)
    except OSError as e:
        sys.stderr.write(f"[tray] control server bind failed: {e}\n")
        return None
    th = threading.Thread(target=srv.serve_forever, daemon=True, name="cc-usage-control")
    th.start()
    return srv


def main():
    _start_control_server()
    try:
        UsageTray()
        Gtk.main()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
