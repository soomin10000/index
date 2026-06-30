"""
Network topology visualiser.

Draws the full network tree: gateway → switches/APs → clients.
Saves to topology.png and topology.json.

Called by poller: topology.render(devices, stations, wlans)
Run standalone:   UNIFI_API_KEY=... python3 topology.py
"""

import json
import os
import sys
from pathlib import Path
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

_DIR = Path(__file__).parent

NODE_Y_GAP   = 3.5
CLIENT_Y_GAP = 2.8

BG     = "#020a18"
COLORS = {"udm": "#ffffff", "usw": "#00aaff", "uap": "#00ffcc", "client": "#1a4a6e"}
GLOW   = {"udm": "#aaddff", "usw": "#0055aa", "uap": "#00aa88", "client": "#0a2a40"}
SIZES  = {"udm": 220, "usw": 160, "uap": 130, "client": 40}
PALETTE = ["#4e9af1", "#f1a34e", "#6dbf67", "#c97bd1", "#e05c5c", "#4ecdc4"]


def render(devices, stations, wlans):
    """Render topology PNG and JSON from pre-fetched UniFi data."""

    # ── Lookup tables ─────────────────────────────────────────────────────────

    ssid_colors = {w["name"]: PALETTE[i % len(PALETTE)] for i, w in enumerate(wlans)}
    dev_by_mac  = {d["mac"]: d for d in devices}

    gateway = next((d for d in devices if d["type"] == "udm"), None)
    gw_mac  = gateway["mac"] if gateway else None

    children   = defaultdict(list)
    ap_clients = defaultdict(list)
    sw_clients = defaultdict(list)

    for d in devices:
        parent = d.get("uplink", {}).get("uplink_mac")
        if parent:
            children[parent].append(d["mac"])

    for sta in stations:
        if sta.get("is_wired"):
            sw_mac = sta.get("sw_mac")
            if sw_mac:
                sw_clients[sw_mac].append(sta)
        else:
            ap_mac = sta.get("ap_mac")
            if ap_mac:
                ap_clients[ap_mac].append(sta)

    # ── Layout state ──────────────────────────────────────────────────────────

    positions   = {}
    labels      = {}
    node_colors = {}
    node_sizes  = {}
    edges       = []
    edge_styles = []

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _client_label(sta):
        name  = sta.get("hostname") or sta.get("mac", "?")
        essid = sta.get("essid", "")
        return f"{name}\n{essid}" if essid else name

    def _add_clients(parent_mac, x_center, y, spacing=1.0):
        clients_here = ap_clients.get(parent_mac, []) + sw_clients.get(parent_mac, [])
        if not clients_here:
            return y
        n       = len(clients_here)
        spacing = max(1.4, spacing)
        xs      = [x_center + (i - (n - 1) / 2) * spacing for i in range(n)]
        cy      = y - CLIENT_Y_GAP
        for idx, (sta, cx) in enumerate(zip(clients_here, xs)):
            nid              = sta["mac"]
            stagger          = -0.35 if idx % 2 else 0
            positions[nid]   = (cx, cy + stagger)
            labels[nid]      = _client_label(sta)
            node_colors[nid] = ssid_colors.get(sta.get("essid"), COLORS["client"])
            node_sizes[nid]  = 40
            edges.append((parent_mac, nid))
            edge_styles.append("dashed" if not sta.get("is_wired") else "solid")
        return cy

    def _leaf_count(mac):
        direct = len(ap_clients.get(mac, []) + sw_clients.get(mac, []))
        return direct + sum(_leaf_count(c) for c in children.get(mac, []))

    def _layout_subtree(mac, x, y, x_width):
        dev = dev_by_mac.get(mac)
        if not dev:
            return
        positions[mac]   = (x, y)
        labels[mac]      = dev.get("name") or dev.get("mac", "?")[:11]
        node_colors[mac] = COLORS.get(dev["type"], COLORS["uap"])
        node_sizes[mac]  = SIZES.get(dev["type"], 600)
        n_clients      = len(ap_clients.get(mac, []) + sw_clients.get(mac, []))
        client_spacing = max(1.6, x_width / max(n_clients, 1) * 0.75)
        _add_clients(mac, x, y, spacing=client_spacing)
        child_macs = children.get(mac, [])
        if not child_macs:
            return
        weights = [max(_leaf_count(c), 1) for c in child_macs]
        total   = sum(weights)
        cx_left = x - x_width / 2
        cy      = y - NODE_Y_GAP
        for cmac, w in zip(child_macs, weights):
            sub_w   = x_width * w / total
            cx      = cx_left + sub_w / 2
            cx_left += sub_w
            edges.append((mac, cmac))
            edge_styles.append("solid")
            _layout_subtree(cmac, cx, cy, sub_w * 0.9)

    _layout_subtree(gw_mac, 0, 0, 30)

    # ── Draw ──────────────────────────────────────────────────────────────────

    fig, ax = plt.subplots(figsize=(22, 20), facecolor=BG)
    ax.set_facecolor(BG)
    ax.set_aspect("equal")
    ax.axis("off")

    xs_all = [p[0] for p in positions.values()]
    ys_all = [p[1] for p in positions.values()]
    for gx in range(int(min(xs_all)) - 2, int(max(xs_all)) + 3, 2):
        ax.axvline(gx, color="#0a1a2e", linewidth=0.4, zorder=0)
    for gy in range(int(min(ys_all)) - 2, int(max(ys_all)) + 3, 2):
        ax.axhline(gy, color="#0a1a2e", linewidth=0.4, zorder=0)

    for (src, dst), style in zip(edges, edge_styles):
        if src not in positions or dst not in positions:
            continue
        x1, y1 = positions[src]
        x2, y2 = positions[dst]
        is_wireless = style == "dashed"
        ec = "#004466" if is_wireless else "#005588"
        ax.plot([x1, x2], [y1, y2], color=ec, linewidth=3, alpha=0.15, zorder=1)
        ax.plot([x1, x2], [y1, y2], color=ec, linewidth=1,
                linestyle=(0, (3, 3)) if is_wireless else "-", alpha=0.7, zorder=2)

    for mac, (x, y) in positions.items():
        dev    = dev_by_mac.get(mac)
        dtype  = dev["type"] if dev else "client"
        color  = node_colors.get(mac, COLORS["client"])
        gcolor = GLOW.get(dtype, GLOW["client"])
        size   = node_sizes.get(mac, 40)
        ax.scatter(x, y, s=size * 12, c=gcolor, alpha=0.06, zorder=3, linewidths=0)
        ax.scatter(x, y, s=size * 5,  c=gcolor, alpha=0.15, zorder=4, linewidths=0)
        ax.scatter(x, y, s=size,      c=color,  zorder=5,   linewidths=0)
        if dev:
            ax.scatter(x, y, s=size * 2.2, facecolors="none",
                       edgecolors=color, linewidths=0.8, alpha=0.5, zorder=5)

    for mac, (x, y) in positions.items():
        label = labels.get(mac, mac)
        dev   = dev_by_mac.get(mac)
        color = node_colors.get(mac, COLORS["client"])
        lc    = color if dev else "#1e5a80"
        ax.annotate(label, (x, y), textcoords="offset points",
                    xytext=(0, -18), ha="center",
                    fontsize=5.5 if dev else 5,
                    fontfamily="monospace",
                    color=lc, zorder=6, alpha=0.9)

    legend_elements = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=COLORS["udm"], markersize=8, label="Gateway"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=COLORS["usw"], markersize=8, label="Switch"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=COLORS["uap"], markersize=8, label="AP"),
        Line2D([0], [0], color="#005588", linestyle="-",       linewidth=1, label="Wired"),
        Line2D([0], [0], color="#004466", linestyle=(0, (3, 3)), linewidth=1, label="Wireless"),
    ]
    for ssid, color in ssid_colors.items():
        legend_elements.append(Line2D([0], [0], marker="o", color="none",
                                      markerfacecolor=color, markersize=7, label=f"SSID: {ssid}"))

    leg = ax.legend(handles=legend_elements, loc="upper right", fontsize=7,
                    framealpha=0, labelcolor="white", handletextpad=0.5, borderpad=0.8)
    for text in leg.get_texts():
        text.set_color("#446688")

    ax.set_title("NETWORK TOPOLOGY", fontsize=11, pad=16,
                 color="#224466", fontfamily="monospace", fontweight="bold", loc="left")

    plt.tight_layout()
    plt.savefig(_DIR / "topology.png", dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)

    # ── Export JSON ───────────────────────────────────────────────────────────

    sta_by_mac = {s["mac"]: s for s in stations}

    def _depth(mac, depth=0, visited=None):
        if visited is None:
            visited = set()
        if mac in visited:
            return depth
        visited.add(mac)
        parent = dev_by_mac.get(mac, {}).get("uplink", {}).get("uplink_mac")
        if not parent or parent not in dev_by_mac:
            return depth
        return _depth(parent, depth + 1, visited)

    graph_nodes = []
    graph_edges = []

    for mac in positions:
        dev   = dev_by_mac.get(mac)
        sta   = sta_by_mac.get(mac)
        color = node_colors.get(mac, COLORS["client"])
        level = (_depth(mac) if dev else
                 _depth(sta.get("ap_mac") or sta.get("sw_mac", ""), 0) + 1 if sta else 99)
        node = {
            "id":         mac,
            "label":      labels.get(mac, mac),
            "type":       dev["type"] if dev else "client",
            "flagged":    False,
            "mac":        mac,
            "level":      level,
            "color":      color,
            "ssid_color": ssid_colors.get(sta.get("essid") if sta else None, color) if sta else color,
        }
        if dev:
            node.update({"ip": dev.get("ip"), "model": dev.get("model"), "name": dev.get("name")})
        if sta:
            node.update({
                "hostname":  sta.get("hostname", ""),
                "ssid":      sta.get("essid"),
                "signal":    sta.get("signal"),
                "retry_pct": sta.get("wifi_tx_retries_percentage", 0),
                "is_wired":  sta.get("is_wired", False),
                "ip":        sta.get("ip"),
            })
        graph_nodes.append(node)

    for (src, dst), style in zip(edges, edge_styles):
        graph_edges.append({"from": src, "to": dst, "wired": style == "solid"})

    (_DIR / "topology.json").write_text(
        json.dumps({"nodes": graph_nodes, "edges": graph_edges}, indent=2)
    )


if __name__ == "__main__":
    sys.path.insert(0, str(_DIR))
    from unifi_client import UnifiClient

    api_key = os.environ.get("UNIFI_API_KEY")
    if not api_key:
        print("Set UNIFI_API_KEY", file=sys.stderr)
        sys.exit(1)

    client = UnifiClient("https://192.168.1.1", api_key)
    render(client.get_devices(), client.get_clients(), client.get_wlans())
    print("Saved topology.png and topology.json")
