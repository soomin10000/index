# Site-to-Site VPN (home <-> remote) — Handoff Context for Claude Code

## What this is
A site-to-site VPN linking the home LAN to a second UniFi site ("far end"),
built 2026-08-19. Goal was general cross-site access (reach devices/services
on either LAN from the other), not a shared SSID or L2 extension — those were
considered and ruled out as unnecessary for the actual goal.

## Infrastructure
- **Home**: UCG-Max at 192.168.1.1, LAN 192.168.1.0/24, WAN 212.132.242.38
  (DHCP-assigned, not static — but has a working DDNS hostname `travis.sytes.net`
  already pointed at it). Dual WAN (Internet 1 + Internet 2, weighted load-balance).
  UniFi OS 5.1.19, Network Application 10.5.67.
- **Far end**: original/base UniFi Dream Machine (UDM, non-Pro/non-SE), hostname
  "DreamMachine", LAN 192.168.10.0/24, WAN shows as private 192.168.0.122 locally
  — genuine ISP-side CGNAT (Virgin Media), externally visible as 86.0.11.157 per
  Site Manager. UniFi OS 5.1.26.
- Both consoles on the same Ubiquiti account (simon@swaysland.net), both visible
  in Site Manager (unifi.ui.com).
- No subnet collision (1.0/24 vs 10.0/24 vs the existing WireGuard remote-user-vpn
  network 192.168.2.0/24 on the home side).

## What's actually running: Site Magic (SD-WAN mesh tunnel)
Despite research suggesting the base UDM isn't on Site Magic's officially
supported gateway list (UDM Pro/SE/Pro Max, UCG Max/Ultra/Fiber/Industrial,
UXG Enterprise/Pro, Dream Router 7 — not plain UDM), **it works anyway**.
Configured via Site Manager (unifi.ui.com), which by 2026-08 no longer shows
a "Site Magic" label in the sidebar — it's folded into a general SD-WAN/Fabrics
flow now. Mesh topology (not hub-and-spoke — UCG-Max can't be a hub anyway).

Manual WireGuard and IPsec/OpenVPN alternatives were researched as fallbacks
(in case Site Magic didn't work) but never built — not needed. Worth knowing
if Site Magic ever breaks for real:
- Manual WireGuard site-to-site on stock UniFi has known bidirectional-routing
  issues (WireGuard Client interface NATs, policy routes get shoved into the
  external firewall zone).
- IPsec's native config has no Dynamic DNS support, which would matter here
  since the far end is CGNAT'd (though the home side's DDNS would only matter
  for the far end dialing in — home has the stable side pattern regardless).
- OpenVPN was the agreed fallback protocol if manual config were ever needed
  (home = server using `travis.sytes.net`, far end = client), but again: not
  built, Site Magic just worked.

## Known bug: status/health API is stale for this tunnel type
Both consoles' local API (`stat/health` vpn subsystem, and the `wgsts1000`
entry in `stat/device`'s `network_table`) report the tunnel as **down** —
`site_to_site_num_active: 0`, `up: "false"`, `ip: null`, zero byte counters —
even while it is demonstrably passing real traffic. This is a telemetry bug
in the legacy REST API, not an actual problem with the tunnel. Don't trust
`stat/health`'s `vpn` subsystem for this tunnel type; verify with real traffic
instead (see below). No dedicated SD-WAN status endpoint was found on either
the legacy `s/{site}/...` API or the `v2/api/site/{site}/...` API — tried
`vpn/site-to-site`, `sdwan`, `sdwan/status`, `network/sdwan`, all 404.

## Verification (2026-08-19)
- User-side: `ping 192.168.10.1` from a machine on the home LAN — consistent
  15-40ms round trips, 0% loss.
- API-side: authenticated HTTPS requests from the home network straight
  through to the far console's local API (`https://192.168.10.1/proxy/network/api/...`)
  succeeded with real HTTP 200 responses and real payloads.
- Sustained test: 30 requests over 30s to the far console's `stat/device`
  endpoint — 0 failures, 868,585 bytes total transferred, avg response 119.5ms
  (min 92.3ms, max 343.2ms).
- Not yet verified: a non-gateway device on the far LAN (none exists there
  yet), and a connection test initiated *from* the far side back to home.

## Credentials (in ~/.config/home-menu.env, not in this repo)
- `UNIFI_API_KEY` — home console (192.168.1.1) local API key. Generated at
  **Settings → API Keys** (top-level, NOT under Control Plane → Integrations).
- `UNIFI_FAREND_API_KEY` — far console (192.168.10.1) local API key. Same
  gotcha applies: must be generated at the top-level **Settings → API Keys**,
  not Control Plane → Integrations (that generates a cloud-scoped key that
  gets rejected — HTTP 401 — by both the legacy `s/{site}/...` API and the
  newer `integrations/v1/...` API when called directly against the local IP).
- `UNIFI_SITEMANAGER_API_KEY` — cloud Site Manager API key (`api.ui.com`),
  used to cross-check which console is which (hardware model, WAN IP as seen
  externally) when the local APIs alone were ambiguous or contradictory.

## Conventions in use
- `verify=False` on requests (self-signed certs on local consoles).
- Auth via `X-API-KEY` header, no login/session/CSRF needed for either the
  legacy or the local-scoped Integration-style key — as long as the key was
  generated from the correct menu (see gotcha above).
