#!/usr/bin/python3
"""GTK3 AppIndicator tray for claude-usage-monitor.

Requires system Python (not anaconda) for gi typelibs:
  /usr/bin/python3 with gir1.2-ayatanaappindicator3-0.1 installed.

Reads ~/.claude/usage_status.txt every 10s, updates icon + label.
Right-click menu: Open dashboard, Refresh now, Open status file, Quit.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

STATUS_FILE = Path.home() / ".claude" / "usage_status.txt"
DASHBOARD_FILE = Path.home() / ".claude" / "usage_monitor" / "dashboard.html"
ICON_DIR = Path.home() / ".claude" / "usage_monitor" / "icons"
REFRESH_SECONDS = 10
SERVICE_NAME = "claude-usage-monitor.service"
STALE_AFTER_MINUTES = 10  # warn if last fresh scrape older than this

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
    if not s or "proj_pct" not in s:
        return _STATE_UNKNOWN
    sess = s.get("session_pct")
    pcts = [s["proj_pct"], s["week_pct"]]
    if isinstance(sess, int):
        pcts.append(sess)
    if s.get("alerting") or max(pcts) >= 90:
        return _STATE_ALERT
    if max(pcts) >= 70:
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


def send_ntfy(title: str, body: str, priority: str = "default") -> None:
    url = os.environ.get("CLAUDE_USAGE_NTFY_URL", "")
    topic = os.environ.get("CLAUDE_USAGE_NTFY_TOPIC", "")
    if not url or not topic:
        return  # ntfy disabled when either env var unset
    subprocess.Popen(
        ["curl", "-s", "--max-time", "5", "-X", "POST",
         f"{url}/{topic}",
         "-H", f"Title: {title}",
         "-H", f"Priority: {priority}",
         "-d", body],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def is_escalation(prev: str, curr: str) -> bool:
    return _RANK.get(curr, 0) > _RANK.get(prev, 0)


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
        self._last_state = _STATE_SAFE
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

        for mi in (self.menu_week, self.menu_sonnet, self.menu_session,
                   self.menu_proj, self.menu_rate, self.menu_verdict,
                   self.menu_freshness):
            self.menu.append(mi)

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

        if is_escalation(self._last_state, state):
            sess = s.get("session_pct")
            sess_part = f" · sess {sess}%" if isinstance(sess, int) else ""
            body = (
                f"wk {s.get('week_pct', '?')}% → proj {s.get('proj_pct', '?')}% "
                f"by {s.get('reset_label', '?')}{sess_part}"
            )
            if state == _STATE_ALERT:
                subprocess.Popen(["notify-send", "-u", "critical", "-t", "0",
                                  "Claude usage ALERT \U0001f6a8", body])
                send_ntfy("Claude usage ALERT \U0001f6a8", body, "urgent")
            elif state == _STATE_WARN:
                subprocess.Popen(["notify-send", "-u", "normal", "-t", "15000",
                                  "Claude usage warning \u26a0\ufe0f", body])
                send_ntfy("Claude usage warning \u26a0\ufe0f", body, "default")
        self._last_state = state

        self.indicator.set_icon_full(_icon_path(state), f"Claude usage: {state}")

        if "week_pct" in s:
            stale_min = _minutes_since_last_fresh(s)
            is_stale = stale_min is not None and stale_min > STALE_AFTER_MINUTES
            # Concise tray label: e.g. "24% → 51%", with ⚠ prefix if stale
            warn_prefix = "\u26a0 " if is_stale else ""
            self.indicator.set_label(
                f"{warn_prefix}{s['week_pct']}% → {s['proj_pct']}%", "99% → 99%"
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
                sess_state = (
                    "alert" if sess_pct >= 90 else
                    ("warn" if sess_pct >= 70 else "safe")
                )
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

    def _open_dashboard(self, _widget):
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


def main():
    try:
        UsageTray()
        Gtk.main()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
