"""
Dashboard — reads from unifi_poller.db and plots flag history.

Called by poller: dashboard.render()
Run standalone:   python3 dashboard.py
Saves to: dashboard.png
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
import matplotlib.patheffects as pe
import numpy as np

# ── Theme ─────────────────────────────────────────────────────────────────────

BG      = "#020a18"
PANEL   = "#040e22"
GRID    = "#0a1e30"
SPINE   = "#0d2640"
LABEL   = "#2a6080"
TITLE   = "#3a90c0"
FONT    = "DejaVu Sans Mono"

PALETTE = [
    "#00e5cc", "#4e9af1", "#f0a030", "#c97bd1",
    "#6dbf67", "#e05c5c", "#4ecdc4", "#f7dc6f",
]

plt.rcParams.update({
    "figure.facecolor":      BG,
    "axes.facecolor":        PANEL,
    "axes.edgecolor":        SPINE,
    "axes.labelcolor":       LABEL,
    "axes.titlecolor":       TITLE,
    "xtick.color":           LABEL,
    "ytick.color":           LABEL,
    "text.color":            LABEL,
    "grid.color":            GRID,
    "grid.linewidth":        0.7,
    "grid.linestyle":        "-",
    "legend.framealpha":     0,
    "legend.labelcolor":     LABEL,
    "font.family":           FONT,
    "font.size":             8,
    "axes.titlesize":        9,
    "axes.labelsize":        8,
    "xtick.labelsize":       7,
    "ytick.labelsize":       7,
    "lines.solid_capstyle":  "round",
    "lines.solid_joinstyle": "round",
})

_DB  = Path.home() / "unifi_poller.db"
_OUT = Path(__file__).parent / "dashboard.png"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _glowline(ax, x, y, color, lw=2):
    ax.plot(x, y, color=color, linewidth=lw * 5,   alpha=0.08, zorder=2, solid_capstyle="round")
    ax.plot(x, y, color=color, linewidth=lw * 2.5, alpha=0.18, zorder=3, solid_capstyle="round")
    ax.plot(x, y, color=color, linewidth=lw,        alpha=0.95, zorder=4, solid_capstyle="round")

def _glowdots(ax, x, y, color):
    ax.scatter(x, y, color=color, s=30, zorder=6, linewidths=0)
    ax.scatter(x, y, color=color, s=80, alpha=0.12, zorder=5, linewidths=0)

def _shade(ax, x, y, color, invert=False):
    baseline = max(y) * 1.05 if invert else min(0, min(y) * 1.05)
    ax.fill_between(x, y, baseline, color=color, alpha=0.07, zorder=1)

def _threshold_line(ax, value, label, invert=False):
    ax.axhline(value, color="#ff3333", linewidth=0.8, linestyle="--", alpha=0.55, zorder=1)
    va = "top" if invert else "bottom"
    ax.text(0.01, value, f"  {label}", transform=ax.get_yaxis_transform(),
            color="#ff5555", fontsize=6.5, va=va, alpha=0.8)

def _style_ax(ax, title, ylabel):
    ax.set_title(f"  {title}", loc="left", pad=8, fontsize=9, fontweight="bold")
    ax.set_ylabel(ylabel, labelpad=6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(SPINE)
    ax.spines["bottom"].set_color(SPINE)
    ax.set_axisbelow(True)
    ax.grid(True)

def _fmt_legend(ax, names, colors):
    handles = [
        plt.Line2D([0], [0], color=c, linewidth=2, label=n,
                   path_effects=[pe.Stroke(linewidth=4, foreground=c, alpha=0.2), pe.Normal()])
        for n, c in zip(names, colors)
    ]
    leg = ax.legend(handles=handles, loc="upper right",
                    handlelength=1.8, handletextpad=0.5,
                    borderpad=0.6, labelspacing=0.4)
    leg.get_frame().set_facecolor(PANEL)
    leg.get_frame().set_edgecolor(SPINE)


# ── Main render ───────────────────────────────────────────────────────────────

def render():
    """Render dashboard PNG from the SQLite log."""
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
        panels += ["speedtest"]

    if not panels:
        return

    fig, axes = plt.subplots(
        len(panels), 1,
        figsize=(15, 4.2 * len(panels)),
        gridspec_kw={"hspace": 0.6},
    )
    if len(panels) == 1:
        axes = [axes]

    xfmt = mdates.DateFormatter("%H:%M\n%d %b")

    for ax, panel in zip(axes, panels):
        _style_ax(ax, {
            "signal":     "SIGNAL STRENGTH",
            "retry":      "TX RETRY RATE",
            "congestion": "CHANNEL UTILISATION",
            "speedtest":  "SPEED TEST",
        }[panel], {
            "signal":     "dBm",
            "retry":      "%",
            "congestion": "%",
            "speedtest":  "Mbps / ms",
        }[panel])
        ax.xaxis.set_major_formatter(xfmt)

        if panel == "signal":
            names  = list(clients.keys())
            colors = PALETTE[:len(names)]
            for name, color in zip(names, colors):
                d = clients[name]
                _glowline(ax, d["ts"], d["signal"], color)
                _shade(ax, d["ts"], d["signal"], color, invert=True)
                _glowdots(ax, d["ts"], d["signal"], color)
            _threshold_line(ax, -70, "−70 dBm weak threshold", invert=True)
            ax.invert_yaxis()
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d dBm"))
            _fmt_legend(ax, names, colors)

        elif panel == "retry":
            names  = list(clients.keys())
            colors = PALETTE[:len(names)]
            for name, color in zip(names, colors):
                d = clients[name]
                _glowline(ax, d["ts"], d["retry_pct"], color)
                _shade(ax, d["ts"], d["retry_pct"], color)
                _glowdots(ax, d["ts"], d["retry_pct"], color)
            _threshold_line(ax, 10, "10% retry threshold")
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
            _fmt_legend(ax, names, colors)

        elif panel == "congestion":
            names  = list(radios.keys())
            colors = [PALETTE[(i + 3) % len(PALETTE)] for i in range(len(names))]

            ax.axhspan(0,   50, color="#00ff88", alpha=0.03, zorder=0)
            ax.axhspan(50,  70, color="#ffaa00", alpha=0.05, zorder=0)
            ax.axhspan(70, 100, color="#ff2222", alpha=0.07, zorder=0)
            ax.axhline(50, color="#ffaa00", linewidth=0.4, alpha=0.3, zorder=1)
            ax.axhline(70, color="#ff3333", linewidth=0.8, linestyle="--", alpha=0.55, zorder=1)
            ax.text(0.01, 70, "  70% congestion threshold", transform=ax.get_yaxis_transform(),
                    color="#ff5555", fontsize=6.5, va="bottom", alpha=0.8)
            ax.text(0.01, 50, "  50%", transform=ax.get_yaxis_transform(),
                    color="#cc8800", fontsize=6, va="bottom", alpha=0.5)

            for name, color in zip(names, colors):
                d = radios[name]
                ts, vals = d["ts"], d["cu_total"]
                ax.plot(ts, vals, color=color, linewidth=0.6, alpha=0.3, zorder=2)
                arr = np.array(vals, dtype=float)
                w   = min(7, len(arr))
                if w > 1:
                    kernel = np.ones(w) / w
                    smooth = np.convolve(arr, kernel, mode="same")
                    smooth[:w//2]  = arr[:w//2]
                    smooth[-w//2:] = arr[-w//2:]
                else:
                    smooth = arr
                _glowline(ax, ts, smooth, color, lw=2)
                _shade(ax, ts, smooth, color)
                _glowdots(ax, ts, smooth, color)

            ax.set_ylim(0, 100)
            ax.yaxis.set_major_locator(mticker.MultipleLocator(25))
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d%%"))
            _fmt_legend(ax, names, colors)

        elif panel == "speedtest":
            _glowline(ax, speed_ts, speed_dl, PALETTE[0], lw=2)
            _shade(ax, speed_ts, speed_dl, PALETTE[0])
            _glowdots(ax, speed_ts, speed_dl, PALETTE[0])
            _glowline(ax, speed_ts, speed_ul, PALETTE[1], lw=2)
            _shade(ax, speed_ts, speed_ul, PALETTE[1])
            _glowdots(ax, speed_ts, speed_ul, PALETTE[1])

            ax2 = ax.twinx()
            ax2.set_facecolor(PANEL)
            ax2.spines["top"].set_visible(False)
            ax2.spines["right"].set_color(SPINE)
            ax2.spines["left"].set_color(SPINE)
            ax2.spines["bottom"].set_color(SPINE)
            _glowline(ax2, speed_ts, speed_ping, PALETTE[2], lw=1.2)
            _glowdots(ax2, speed_ts, speed_ping, PALETTE[2])
            ax2.set_ylabel("ping ms", color=LABEL, fontsize=7, fontfamily=FONT, labelpad=6)
            ax2.tick_params(colors=LABEL, labelsize=7)
            for lbl in ax2.get_yticklabels():
                lbl.set_color(PALETTE[2])
                lbl.set_fontfamily(FONT)
            ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%dms"))
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d Mbps"))
            _fmt_legend(ax, ["Download", "Upload", "Ping"], PALETTE[:3])

    fig.text(0.012, 0.995, "UNIFI NETWORK MONITOR", ha="left", va="top",
             fontsize=11, fontweight="bold", color=TITLE)
    fig.text(0.012, 0.975, f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             ha="left", va="top", fontsize=7, color=LABEL)

    plt.savefig(_OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)


if __name__ == "__main__":
    render()
    print(f"Saved {_OUT}")
