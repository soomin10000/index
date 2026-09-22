# Kismet remote capture on jeff

The Alfa AWUS036ACH (monitor-mode WiFi adapter) moved from pi4 to jeff
(2026-09-21), consolidating both of jeff's wireless-capture roles (RTL-SDR
+ WiFi) onto one dedicated capture host, while Kismet itself and the
home_menu dashboard stay on steve. This supersedes `pollers/kismet_pi4/`
— same pattern, different host. See that directory's README for the full
background on why remote capture (vs moving Kismet itself, vs opening a
port on the LAN) was the chosen approach.

Confirmed before building this:
- jeff and pi4 run the same Debian 13 trixie / Raspberry Pi kernel family,
  and the Alfa bound cleanly to the same in-tree `rtw88_8812au` driver on
  jeff as it did on pi4 (`wlan1`, separate USB controller from the
  RTL-SDR's `0bda:2838` on a different bus — no USB contention).
- Power: jeff's PSU was swapped for the proper one earlier; `throttled=0x0`
  (clean, no history flags either) both before and after adding the Alfa.
- No interaction with jeff's existing SDR mode-switcher (`_sdr_switch` in
  `server.py`, [[project_jeff_sdr_modes]]) — that only manages the RTL-SDR
  dongle's readsb/rtl_433/acarsdec/openwebrx modes. The Alfa capture agent
  is an entirely separate USB device and systemd service.

Already done (no sudo needed, done from steve):
- Dedicated keypair generated on jeff: `~/.ssh/id_ed25519_kismet_tunnel`.
- Its pubkey added to steve's `~/.ssh/authorized_keys`, restricted to
  `permitopen="127.0.0.1:2501"` with `command="/bin/false"` — can only
  open that one tunneled port, nothing else.
- Manually verified the tunnel connects and steve's Kismet port 2501 is
  reachable from jeff through it (got a 401 without creds, as expected).

Still needed on jeff (needs an interactive sudo password — jeff has no
passwordless sudo):

```
# 1. Add Kismet's apt repo (trixie/arm64) and install just the capture helper
wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key | \
  gpg --dearmor | sudo tee /usr/share/keyrings/kismet-archive-keyring.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/kismet-archive-keyring.gpg] https://www.kismetwireless.net/repos/apt/git/trixie trixie main' | \
  sudo tee /etc/apt/sources.list.d/kismet.list
sudo apt update
sudo apt install -y kismet-capture-linux-wifi

# 2. Create the credentials file the capture service reads (root-only, NOT in git)
ssh steve "grep -E '^KISMET_(USER|PASS)=' ~/.config/home-menu.env" | sudo tee /etc/kismet-capture.env >/dev/null
sudo chmod 600 /etc/kismet-capture.env

# 3. Install the two service files from this directory
sudo cp jeff-kismet-tunnel.service jeff-kismet-capture.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jeff-kismet-tunnel.service
sudo systemctl enable --now jeff-kismet-capture.service

# 4. Verify
sudo systemctl status jeff-kismet-tunnel.service jeff-kismet-capture.service
journalctl -u jeff-kismet-capture.service -n 30 --no-pager
```

**Cutover — once `jeff_alfa` shows up and running in Kismet's datasource
list**, retire the pi4 side so there isn't a stale unused key/service
lying around:
```
# on pi4:
sudo systemctl disable --now pi4-kismet-tunnel.service pi4-kismet-capture.service
sudo rm /etc/systemd/system/pi4-kismet-tunnel.service /etc/systemd/system/pi4-kismet-capture.service
sudo rm /etc/kismet-capture.env
sudo systemctl daemon-reload
```
Then tell me so I can remove pi4's key from steve's `authorized_keys`,
delete `pollers/kismet_pi4/` from the repo, and update the index page's
Wireless card footer to say jeff instead of pi4.

Verify on steve once both jeff services are up:
`curl -s -u "$KISMET_USER:$KISMET_PASS" localhost:2501/datasource/all_sources.json`
should show `jeff_alfa` running, and `/kismet` should keep showing live
devices with zero interruption (Kismet just gains a second remote source
briefly during cutover, no config change needed on steve either way).

## Weekly restart (leak mitigation, added 2026-09-22)

`kismet_cap_linux_wifi` leaks ~200MB/day (see
[[project_kismet_memory_leak]]) — steve already restarts `kismet.service`
weekly for this, but that only covers the **server**. Since capture moved
here, the leaking process is `jeff-kismet-capture.service` on jeff itself,
which had no restart schedule (`Restart=always` on crash only). This adds
a matching weekly restart on jeff, offset 30 min after steve's so both
sides refresh in the same maintenance window without racing each other.

```
sudo cp jeff-kismet-restart.service jeff-kismet-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jeff-kismet-restart.timer
systemctl list-timers jeff-kismet-restart.timer
```
