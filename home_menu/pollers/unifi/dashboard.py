"""
Dashboard — reads from unifi_poller.db and plots network history.

Called by poller: dashboard.render()
Run standalone:   python3 dashboard.py
Saves to: dashboard.png

Styled to match the "Signal Room" web console (/unifi): ink-slate surface,
phosphor-cyan hero trace, reserved status colours, clean thin marks (no glow).
"""

import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import numpy as np

# ── Theme (matches unifi.html console) ─────────────────────────────────────────

BG     = "#0a0e13"   # page surface
PANEL  = "#111820"   # axes surface
GRID   = "#1b2531"   # hairline grid
SPINE  = "#26333f"   # baseline
TICK   = "#8a97a4"   # tick labels
MUTE   = "#586773"   # axis labels / captions
INK    = "#e6edf3"   # titles
ACCENT = "#35d0d6"   # hero trace (matches web download sparkline)
FONT   = "DejaVu Sans Mono"

# status palette — reserved, only for thresholds / zones (never a series)
GOOD, WARN, CRIT = "#3fb950", "#d6a419", "#f85149"

# categorical series order — the skill's CVD-validated dark ordering
PALETTE = [
    "#3987e5",  # blue
    "#199e70",  # aqua
    "#c98500",  # yellow
    "#9085e9",  # violet
    "#d55181",  # magenta
    "#d95926",  # orange
    "#57b6c9",  # slate-cyan
    "#8aa0b0",  # grey-blue
]

plt.rcParams.update({
    "figure.facecolor":      BG,
    "axes.facecolor":        PANEL,
    "axes.edgecolor":        SPINE,
    "axes.labelcolor":       MUTE,
    "axes.titlecolor":       INK,
    "xtick.color":           TICK,
    "ytick.color":           TICK,
    "text.color":            MUTE,
    "grid.color":            GRID,
    "grid.linewidth":        0.8,
    "grid.linestyle":        "-",
    "legend.framealpha":     0,
    "legend.labelcolor":     TICK,
    "font.family":           FONT,
    "font.size":             11,
    "axes.titlesize":        12.5,
    "axes.labelsize":        10,
    "xtick.labelsize":       9.5,
    "ytick.labelsize":       9.5,
    "lines.solid_capstyle":  "round",
    "lines.solid_joinstyle": "round",
})

_DB  = Path.home() / "unifi_poller.db"
_OUT = Path(__file__).resolve().parents[2] / "data" / "unifi" / "dashboard.png"

# How much history the time-series panels draw. radio_util_log gets a row per
# radio per poll and is kept for 90 days (prune_radio_util), which is ~90k rows —
# pulling and plotting all of it every render is the poller's main memory churn,
# and a 90-day dense line is unreadable anyway. The "by hour of day" averages
# stay stable on a few weeks of samples.
_WINDOW_DAYS = 21

# ── Marks (clean, no glow) ─────────────────────────────────────────────────────

def _line(ax, x, y, color, lw=1.8, fill=False):
    # drop gaps so min()/plotting never chokes on a NULL logged sample
    pts = [(xi, yi) for xi, yi in zip(x, y) if yi is not None]
    if not pts:
        return
    x = [p[0] for p in pts]
    y = [p[1] for p in pts]
    if fill:
        ax.fill_between(x, y, _floor(ax, y), color=color, alpha=0.09, zorder=1,
                        linewidth=0)
    ax.plot(x, y, color=color, linewidth=lw, alpha=0.95, zorder=4)
    # single end marker to anchor the latest reading
    ax.scatter([x[-1]], [y[-1]], color=color, s=22, zorder=5, linewidths=0)

def _floor(ax, y):
    lo = min(y)
    hi = max(y)
    return lo - (hi - lo) * 0.08 - 1e-9

def _threshold(ax, value, label, invert=False):
    ax.axhline(value, color=CRIT, linewidth=1.0, linestyle=(0, (5, 4)), alpha=0.6, zorder=2)
    ax.text(0.006, value, f" {label}", transform=ax.get_yaxis_transform(),
            color=CRIT, fontsize=8.5, va="top" if invert else "bottom", alpha=0.85)

def _style_ax(ax, title, ylabel):
    ax.set_title(title, loc="left", pad=10, fontsize=12.5, fontweight="bold")
    ax.set_ylabel(ylabel, labelpad=8)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(SPINE)
    ax.tick_params(length=0)
    ax.margins(x=0.01)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y")
    ax.grid(False, axis="x")

def _legend(ax, names, colors):
    # placed in the gap ABOVE the axes so it never overlaps dense traces
    handles = [plt.Line2D([0], [0], color=c, linewidth=2.4, label=n)
               for n, c in zip(names, colors)]
    ax.legend(handles=handles, loc="lower right", bbox_to_anchor=(1.0, 1.005),
              ncol=min(len(names), 4), frameon=False,
              handlelength=1.4, handletextpad=0.5, columnspacing=1.4,
              labelspacing=0.3, fontsize=9)


# ── Main render ─────────────────────────────────────────────────────────────────

def render():
    cutoff = int(time.time()) - _WINDOW_DAYS * 86400
    conn = sqlite3.connect(_DB)
    weak_rows  = conn.execute(
        "SELECT ts, hostname, signal, retry_pct FROM weak_client_log "
        "WHERE ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    # Continuous per-poll samples (every radio, every poll) rather than
    # congestion_log, which only has a row once cu_total crosses the alert
    # threshold — that's fine for dedup-alerting but too sparse to trend on.
    util_rows  = conn.execute(
        "SELECT ts, ap, radio, cu_total FROM radio_util_log "
        "WHERE ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    speed_rows = conn.execute(
        "SELECT ts, ping_ms, download_mbps, upload_mbps FROM speedtest_log "
        "WHERE ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    event_rows = conn.execute(
        "SELECT ts, type, message FROM events_log "
        "WHERE type IN ('device_joined', 'device_left', 'channel_changed') "
        "AND ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    conn.close()

    if not weak_rows and not util_rows and not speed_rows:
        return

    clients = defaultdict(lambda: {"ts": [], "signal": [], "retry_pct": []})
    for ts, hostname, signal, retry_pct in weak_rows:
        clients[hostname]["ts"].append(datetime.fromtimestamp(ts))
        clients[hostname]["signal"].append(signal)
        clients[hostname]["retry_pct"].append(retry_pct)

    radios = defaultdict(lambda: {"ts": [], "cu_total": []})
    hourly = defaultdict(lambda: defaultdict(list))  # radio -> hour(0-23) -> [cu_total]
    for ts, ap, radio, cu_total in util_rows:
        name = f"{ap} · {radio}"
        when = datetime.fromtimestamp(ts)
        radios[name]["ts"].append(when)
        radios[name]["cu_total"].append(cu_total)
        if cu_total is not None:
            hourly[name][when.hour].append(cu_total)

    speed_ts   = [datetime.fromtimestamp(r[0]) for r in speed_rows]
    speed_ping = [r[1] for r in speed_rows]
    speed_dl   = [r[2] for r in speed_rows]
    speed_ul   = [r[3] for r in speed_rows]

    panels = []
    if clients:
        panels += ["signal", "retry"]
    if radios:
        panels += ["congestion", "hourly"]
    if speed_rows:
        panels += ["throughput", "latency"]
    if not panels:
        return

    fig, axes = plt.subplots(
        len(panels), 1,
        figsize=(12, 3.25 * len(panels)),
        gridspec_kw={"hspace": 0.78},
    )
    if len(panels) == 1:
        axes = [axes]

    xfmt = mdates.DateFormatter("%d %b\n%H:%M")

    EVENT_STYLE = {
        "device_joined":   (GOOD, "▲ joined"),
        "device_left":     (MUTE, "▽ left"),
        "channel_changed": (WARN, "◆ channel Δ"),
    }

    TITLES = {
        "signal": "SIGNAL STRENGTH", "retry": "TX RETRY RATE",
        "congestion": "CHANNEL UTILISATION", "hourly": "CONGESTION BY HOUR OF DAY",
        "throughput": "WAN THROUGHPUT", "latency": "WAN LATENCY",
    }
    YLABELS = {
        "signal": "dBm", "retry": "%", "congestion": "%", "hourly": "avg %",
        "throughput": "Mbps", "latency": "ms",
    }

    for ax, panel in zip(axes, panels):
        _style_ax(ax, TITLES[panel], YLABELS[panel])
        if panel != "hourly":
            ax.xaxis.set_major_formatter(xfmt)

        if panel in ("signal", "retry"):
            names  = list(clients.keys())
            colors = PALETTE[:len(names)]
            key = "signal" if panel == "signal" else "retry_pct"
            for name, color in zip(names, colors):
                _line(ax, clients[name]["ts"], clients[name][key], color)
            if panel == "signal":
                _threshold(ax, -70, "−70 dBm weak", invert=True)
                ax.invert_yaxis()
                ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))
            else:
                _threshold(ax, 10, "10% retry")
                ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f"))
            _legend(ax, names, colors)

        elif panel == "congestion":
            names  = list(radios.keys())
            colors = PALETTE[:len(names)]
            ax.axhspan(70, 100, color=CRIT, alpha=0.05, zorder=0)
            ax.axhspan(50,  70, color=WARN, alpha=0.05, zorder=0)
            for name, color in zip(names, colors):
                vals = np.array(radios[name]["cu_total"], dtype=float)
                w = min(7, len(vals))
                if w > 1:
                    smooth = np.convolve(vals, np.ones(w) / w, mode="same")
                    smooth[:w // 2] = vals[:w // 2]
                    smooth[-w // 2:] = vals[-w // 2:]
                else:
                    smooth = vals
                _line(ax, radios[name]["ts"], smooth, color)
            _threshold(ax, 70, "70% congested")
            ax.set_ylim(0, 100)
            ax.yaxis.set_major_locator(mticker.MultipleLocator(25))
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))

            # Device join/leave + channel-change ticks, so a congestion spike
            # can be visually traced back to a likely cause.
            present = [t for t in ("device_joined", "device_left", "channel_changed")
                       if any(r[1] == t for r in event_rows)]
            for ts, ev_type, _msg in event_rows:
                color, _ = EVENT_STYLE[ev_type]
                ax.axvline(datetime.fromtimestamp(ts), color=color, linewidth=0.9,
                           linestyle=(0, (1, 2)), alpha=0.5, zorder=3)
            x = 0.0
            for t in present:
                color, label = EVENT_STYLE[t]
                ax.text(x, 1.16, f"{label}   ", transform=ax.transAxes, color=color,
                        fontsize=8.5, va="bottom", ha="left")
                x += 0.018 * (len(label) + 3)
            _legend(ax, names, colors)

        elif panel == "hourly":
            names  = list(radios.keys())
            colors = PALETTE[:len(names)]
            hours = list(range(24))
            ax.axhspan(70, 100, color=CRIT, alpha=0.05, zorder=0)
            ax.axhspan(50,  70, color=WARN, alpha=0.05, zorder=0)
            for name, color in zip(names, colors):
                avgs = [np.mean(hourly[name][h]) if hourly[name].get(h) else None for h in hours]
                pts = [(h, v) for h, v in zip(hours, avgs) if v is not None]
                if not pts:
                    continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                ax.plot(xs, ys, color=color, linewidth=1.8, alpha=0.95, zorder=4)
                ax.scatter(xs, ys, color=color, s=14, zorder=5, linewidths=0)
            ax.set_xlim(-0.5, 23.5)
            ax.set_ylim(0, 100)
            ax.set_xlabel("hour of day (local)")
            ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
            ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%02d:00"))
            ax.yaxis.set_major_locator(mticker.MultipleLocator(25))
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))
            _legend(ax, names, colors)

        elif panel == "throughput":
            _line(ax, speed_ts, speed_dl, ACCENT, fill=True)
            _line(ax, speed_ts, speed_ul, PALETTE[0], fill=True)
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))
            _legend(ax, ["Download", "Upload"], [ACCENT, PALETTE[0]])

        elif panel == "latency":
            _line(ax, speed_ts, speed_ping, PALETTE[2], fill=True)
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d"))

    # header
    fig.text(0.012, 0.995, "●", ha="left", va="top",
             fontsize=13, fontweight="bold", color=ACCENT)
    fig.text(0.026, 0.995, " SIGNAL ROOM", ha="left", va="top",
             fontsize=13, fontweight="bold", color=INK)
    fig.text(0.012, 0.975,
             f"network history · generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             ha="left", va="top", fontsize=9, color=MUTE)

    plt.savefig(_OUT, dpi=130, bbox_inches="tight", facecolor=BG)
    plt.close(fig)


if __name__ == "__main__":
    render()
    print(f"Saved {_OUT}")
