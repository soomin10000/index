"""
Dashboard — reads from unifi_poller.db and plots network history.

Called by poller: dashboard.render()
Run standalone:   python3 dashboard.py
Saves to: dashboard.png

Styled to match the "Signal Room" web console (/unifi): ink-slate surface,
phosphor-cyan hero trace, reserved status colours, clean thin marks (no glow).
"""

import sqlite3
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
    conn = sqlite3.connect(_DB)
    weak_rows  = conn.execute(
        "SELECT ts, hostname, signal, retry_pct FROM weak_client_log ORDER BY ts"
    ).fetchall()
    cong_rows  = conn.execute(
        "SELECT ts, ap, radio, cu_total FROM congestion_log ORDER BY ts"
    ).fetchall()
    speed_rows = conn.execute(
        "SELECT ts, ping_ms, download_mbps, upload_mbps FROM speedtest_log ORDER BY ts"
    ).fetchall()
    conn.close()

    if not weak_rows and not cong_rows and not speed_rows:
        return

    clients = defaultdict(lambda: {"ts": [], "signal": [], "retry_pct": []})
    for ts, hostname, signal, retry_pct in weak_rows:
        clients[hostname]["ts"].append(datetime.fromtimestamp(ts))
        clients[hostname]["signal"].append(signal)
        clients[hostname]["retry_pct"].append(retry_pct)

    radios = defaultdict(lambda: {"ts": [], "cu_total": []})
    for ts, ap, radio, cu_total in cong_rows:
        radios[f"{ap} · {radio}"]["ts"].append(datetime.fromtimestamp(ts))
        radios[f"{ap} · {radio}"]["cu_total"].append(cu_total)

    speed_ts   = [datetime.fromtimestamp(r[0]) for r in speed_rows]
    speed_ping = [r[1] for r in speed_rows]
    speed_dl   = [r[2] for r in speed_rows]
    speed_ul   = [r[3] for r in speed_rows]

    panels = []
    if clients:
        panels += ["signal", "retry"]
    if radios:
        panels += ["congestion"]
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

    TITLES = {
        "signal": "SIGNAL STRENGTH", "retry": "TX RETRY RATE",
        "congestion": "CHANNEL UTILISATION", "throughput": "WAN THROUGHPUT",
        "latency": "WAN LATENCY",
    }
    YLABELS = {
        "signal": "dBm", "retry": "%", "congestion": "%",
        "throughput": "Mbps", "latency": "ms",
    }

    for ax, panel in zip(axes, panels):
        _style_ax(ax, TITLES[panel], YLABELS[panel])
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
