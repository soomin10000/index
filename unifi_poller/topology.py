"""
Network topology visualiser.

Draws the full network tree: gateway → switches/APs → clients.
Flagged clients (weak signal + high retries) are highlighted in red.
Saves to topology.png.

Run with: UNIFI_API_KEY=... python3 topology.py
"""

import json
import os, sys
from pathlib import Path
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).parent))
from unifi_client import UnifiClient
from checks import check_weak_clients

# ── Fetch data ────────────────────────────────────────────────────────────────

api_key = os.environ.get("UNIFI_API_KEY")
if not api_key:
    print("Set UNIFI_API_KEY", file=sys.stderr); sys.exit(1)

client = UnifiClient("https://192.168.1.1", api_key)
devices  = client.get_devices()
stations = client.get_clients()
wlans    = client._get("s/default/rest/wlanconf")

weak_flags     = check_weak_clients(client)
weak_hostnames = {f["hostname"] for f in weak_flags}

# ── Build lookup tables ───────────────────────────────────────────────────────

SSID_COLORS = {}
_palette = ["#4e9af1", "#f1a34e", "#6dbf67", "#c97bd1", "#e05c5c", "#4ecdc4"]
for i, w in enumerate(wlans):
    SSID_COLORS[w["name"]] = _palette[i % len(_palette)]

dev_by_mac = {d["mac"]: d for d in devices}

# Identify gateway
gateway = next((d for d in devices if d["type"] == "udm"), None)
gw_mac  = gateway["mac"] if gateway else None

# Build uplink tree: parent_mac → [child_mac]
children = defaultdict(list)
for d in devices:
    parent = d.get("uplink", {}).get("uplink_mac")
    if parent:
        children[parent].append(d["mac"])

# Map AP mac → wireless clients
ap_clients = defaultdict(list)
sw_clients = defaultdict(list)  # switch mac → wired clients

for sta in stations:
    if sta.get("is_wired"):
        sw_mac = sta.get("sw_mac")
        if sw_mac:
            sw_clients[sw_mac].append(sta)
    else:
        ap_mac = sta.get("ap_mac")
        if ap_mac:
            ap_clients[ap_mac].append(sta)

# ── Layout: recursive tree ────────────────────────────────────────────────────

NODE_Y_GAP   = 3.5
CLIENT_Y_GAP = 2.8
positions    = {}
labels       = {}
node_colors  = {}
node_sizes   = {}
edges        = []
edge_styles  = []

BG      = "#020a18"
COLORS = {
    "udm":    "#ffffff",
    "usw":    "#00aaff",
    "uap":    "#00ffcc",
    "client": "#1a4a6e",
    "flagged":"#ff3030",
}
GLOW = {
    "udm":    "#aaddff",
    "usw":    "#0055aa",
    "uap":    "#00aa88",
    "client": "#0a2a40",
    "flagged":"#aa0000",
}
SIZES = {"udm": 220, "usw": 160, "uap": 130, "client": 40}


def node_label(d):
    return d.get("name") or d.get("hostname") or d.get("mac", "?")[:11]


def client_label(sta):
    name = sta.get("hostname") or sta.get("mac", "?")
    essid = sta.get("essid", "")
    return f"{name}\n{essid}" if essid else name


def add_clients(parent_mac, x_center, y, spacing=1.0):
    """Add wireless clients of an AP, or wired clients of a switch."""
    clients_here = ap_clients.get(parent_mac, []) + sw_clients.get(parent_mac, [])
    if not clients_here:
        return y
    n = len(clients_here)
    spacing = max(1.4, spacing)
    xs = [x_center + (i - (n-1)/2) * spacing for i in range(n)]
    cy = y - CLIENT_Y_GAP
    for idx, (sta, cx) in enumerate(zip(clients_here, xs)):
        nid = sta["mac"]
        # Stagger every other label vertically to reduce overlap
        stagger = -0.35 if idx % 2 else 0
        positions[nid] = (cx, cy + stagger)
        labels[nid]    = client_label(sta)
        hn = sta.get("hostname", "")
        flagged = hn in weak_hostnames or sta["mac"] in weak_hostnames
        node_colors[nid] = COLORS["flagged"] if flagged else (
            SSID_COLORS.get(sta.get("essid"), COLORS["client"])
        )
        node_sizes[nid]  = 80 if flagged else 40
        edges.append((parent_mac, nid))
        edge_styles.append("dashed" if not sta.get("is_wired") else "solid")
    return cy


def leaf_count(mac):
    """Total clients in this subtree (device + all descendant devices)."""
    direct = len(ap_clients.get(mac, []) + sw_clients.get(mac, []))
    return direct + sum(leaf_count(c) for c in children.get(mac, []))


def layout_subtree(mac, x, y, x_width):
    """Recursively position a device, allocating width proportional to leaf count."""
    dev = dev_by_mac.get(mac)
    if not dev:
        return

    positions[mac]   = (x, y)
    labels[mac]      = node_label(dev)
    node_colors[mac] = COLORS.get(dev["type"], COLORS["uap"])
    node_sizes[mac]  = SIZES.get(dev["type"], 600)

    my_clients = ap_clients.get(mac, []) + sw_clients.get(mac, [])
    n_clients  = len(my_clients)
    client_spacing = max(1.6, x_width / max(n_clients, 1) * 0.75)
    add_clients(mac, x, y, spacing=client_spacing)

    child_macs = children.get(mac, [])
    if not child_macs:
        return

    # Allocate width proportional to each child's leaf count
    weights = [max(leaf_count(c), 1) for c in child_macs]
    total   = sum(weights)
    cx_left = x - x_width / 2
    cy      = y - NODE_Y_GAP
    for cmac, w in zip(child_macs, weights):
        sub_w  = x_width * w / total
        cx     = cx_left + sub_w / 2
        cx_left += sub_w
        edges.append((mac, cmac))
        edge_styles.append("solid")
        layout_subtree(cmac, cx, cy, sub_w * 0.9)


layout_subtree(gw_mac, 0, 0, 30)

# ── Draw ──────────────────────────────────────────────────────────────────────

OUT = Path(__file__).parent / "topology.png"

fig, ax = plt.subplots(figsize=(22, 20), facecolor=BG)
ax.set_facecolor(BG)
ax.set_aspect("equal")
ax.axis("off")

# Subtle grid
for gx in range(int(min(p[0] for p in positions.values())) - 2,
                int(max(p[0] for p in positions.values())) + 3, 2):
    ax.axvline(gx, color="#0a1a2e", linewidth=0.4, zorder=0)
for gy in range(int(min(p[1] for p in positions.values())) - 2,
                int(max(p[1] for p in positions.values())) + 3, 2):
    ax.axhline(gy, color="#0a1a2e", linewidth=0.4, zorder=0)

# Edges
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

# Nodes — glow rings then core
for mac, (x, y) in positions.items():
    dev    = dev_by_mac.get(mac)
    dtype  = dev["type"] if dev else "client"
    color  = node_colors.get(mac, COLORS["client"])
    gcolor = GLOW.get(dtype, GLOW["client"])
    if color == COLORS["flagged"]:
        gcolor = GLOW["flagged"]
    size = node_sizes.get(mac, 40)
    ax.scatter(x, y, s=size * 12, c=gcolor, alpha=0.06, zorder=3, linewidths=0)
    ax.scatter(x, y, s=size * 5,  c=gcolor, alpha=0.15, zorder=4, linewidths=0)
    ax.scatter(x, y, s=size,      c=color,  zorder=5,   linewidths=0)
    if dev:
        ax.scatter(x, y, s=size * 2.2, facecolors="none",
                   edgecolors=color, linewidths=0.8, alpha=0.5, zorder=5)

# Labels
for mac, (x, y) in positions.items():
    label = labels.get(mac, mac)
    dev   = dev_by_mac.get(mac)
    color = node_colors.get(mac, COLORS["client"])
    lc    = color if dev else "#1e5a80"
    if color == COLORS["flagged"]:
        lc = "#ff6666"
    ax.annotate(label, (x, y), textcoords="offset points",
                xytext=(0, -18), ha="center",
                fontsize=5.5 if dev else 5,
                fontfamily="monospace",
                color=lc, zorder=6, alpha=0.9)

# Legend
legend_elements = [
    Line2D([0],[0], marker="o", color="none", markerfacecolor=COLORS["udm"],    markersize=8, label="Gateway"),
    Line2D([0],[0], marker="o", color="none", markerfacecolor=COLORS["usw"],    markersize=8, label="Switch"),
    Line2D([0],[0], marker="o", color="none", markerfacecolor=COLORS["uap"],    markersize=8, label="AP"),
    Line2D([0],[0], marker="o", color="none", markerfacecolor=COLORS["flagged"],markersize=8, label="Flagged"),
    Line2D([0],[0], color="#005588", linestyle="-",       linewidth=1, label="Wired"),
    Line2D([0],[0], color="#004466", linestyle=(0,(3,3)), linewidth=1, label="Wireless"),
]
for ssid, color in SSID_COLORS.items():
    legend_elements.append(Line2D([0],[0], marker="o", color="none",
                                  markerfacecolor=color, markersize=7, label=f"SSID: {ssid}"))

leg = ax.legend(handles=legend_elements, loc="upper right", fontsize=7,
                framealpha=0, labelcolor="white", handletextpad=0.5, borderpad=0.8)
for text in leg.get_texts():
    text.set_color("#446688")

ax.set_title("NETWORK TOPOLOGY", fontsize=11, pad=16,
             color="#224466", fontfamily="monospace", fontweight="bold", loc="left")

plt.tight_layout()
plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
print(f"Saved {OUT}")

# ── Export JSON for interactive map ───────────────────────────────────────────

sta_by_mac = {s["mac"]: s for s in stations}

def _node_level(mac, depth=0):
    """Depth from gateway."""
    return depth

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
    dev = dev_by_mac.get(mac)
    sta = sta_by_mac.get(mac)
    color = node_colors.get(mac, COLORS["client"])
    flagged = color == COLORS["flagged"]
    level = _depth(mac) if dev else (_depth(sta.get("ap_mac") or sta.get("sw_mac", ""), 0) + 1 if sta else 99)

    node = {
        "id":      mac,
        "label":   labels.get(mac, mac),
        "type":    dev["type"] if dev else "client",
        "flagged": flagged,
        "mac":     mac,
        "level":   level,
        "color":   color,
        "ssid_color": SSID_COLORS.get(sta.get("essid") if sta else None, color) if sta else color,
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

json_out = Path(__file__).parent / "topology.json"
json_out.write_text(json.dumps({"nodes": graph_nodes, "edges": graph_edges}, indent=2))
print(f"Saved {json_out}")
