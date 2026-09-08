# weeny — Pi Zero W OpenCanary honeypot, rebuilt from the Mac

`weeny` is a Raspberry Pi Zero W running an OpenCanary "cheap-NAS" honeypot on
`192.168.1.5` (management on `192.168.1.247`). On 2026-09-06 an
`/etc/ssh/sshd_config.d/10-listen.conf` with `ListenAddress 192.168.1.247` made
sshd fail to bind on cold boot (it starts before NetworkManager assigns the
address) and exit with no fallback — a full lockout, card-pull required.

This directory rebuilds the box **from a card flashed on a Mac**. Raspberry Pi
Imager writes cloud-init files to the FAT boot partition; a one-line `runcmd:`
hook runs `provision.sh` unattended on first boot, which installs everything and
fixes the root cause by design:

* real OpenSSH on **`Port 2222`, wildcard, key-only** — **no `ListenAddress`
  anywhere**, so nothing to race on cold boot
* nft drop of `192.168.1.5:2222` so the bait IP never exposes a real sshd
* OpenCanary keeps the interesting port (`192.168.1.5:22`, fake banner)
* `net.ipv4.ip_nonlocal_bind=1` + a `Restart=always` drop-in on `ssh.service` as
  belt-and-braces
* comitup for headless Wi-Fi re-provisioning, **decoupled** from the honeypot
  (no `web_service:` — that had disabled opencanary's own enablement)
* unattended security upgrades, no auto-reboot

## Files (all copied verbatim to `bootfs:/weeny/`, installed by `provision.sh`)

| Repo file | Installed to | Owner / mode |
|---|---|---|
| `provision.sh` | run in place from `/boot/firmware/weeny/` | — (invoked `bash …`) |
| `user-data.runcmd.yaml` | *merged by hand into `bootfs:/user-data`* | — |
| `secrets.env` (from `secrets.env.example`) | `bootfs:/weeny/secrets.env`, read at stage 12 | 0600, **never committed** |
| `opencanary.conf` | `/etc/opencanaryd/opencanary.conf` | `root:canary` 0640 |
| `opencanary.service` | `/etc/systemd/system/opencanary.service` | root 0644 |
| `opencanary.logrotate` | `/etc/logrotate.d/opencanary` | root 0644 |
| `sshd-10-weeny.conf` | `/etc/ssh/sshd_config.d/10-weeny.conf` | root 0644 |
| `ssh-restart.conf` | `/etc/systemd/system/ssh.service.d/restart.conf` | root 0644 |
| `nftables.conf` | `/etc/nftables.conf` | root 0644 |
| `sysctl-90-weeny.conf` | `/etc/sysctl.d/90-weeny.conf` | root 0644 |
| `50-honeypot-ip` | `/etc/NetworkManager/dispatcher.d/50-honeypot-ip` | root 0755 |
| `comitup.conf` | `/etc/comitup.conf` (rendered + `ap_password`) | root 0600 |
| `comitup-callback` | `/usr/local/bin/comitup-callback` | root 0755 |
| `apt-20auto-upgrades` | `/etc/apt/apt.conf.d/20auto-upgrades` | root 0644 |
| `apt-52weeny-unattended` | `/etc/apt/apt.conf.d/52weeny-unattended-upgrades` | root 0644 |

The honeypot **persona** (`opencanary.conf`) is `weeny-hp`: FTP `NAS FTP service
ready` (no leading `220 ` — OpenCanary prepends it), telnet with `admin/admin` +
`root/root` honeycreds, HTTP `lighttpd/1.4.59` + `nasLogin` skin, SSH
`OpenSSH_7.4p1`, everything else off. Coherent small-NAS, deliberately not
maximal. This was reconstructed verbatim from the original build session, so no
"salvage the old card" step is needed. Before flashing, set a fresh hotspot SSID
in `comitup.conf` (`ap_name:` — the old value wasn't recorded and it's only
cosmetic).

*(Optional, not required: to compare against the real old config, read the old
card's ext4 rootfs on the Mac with `brew install --cask macfuse && brew install
ext4fuse`, mount it read-only, and diff `/etc/opencanaryd/opencanary.conf`.
macOS has no native ext4; steve is a VM with no card reader — neither can mount
it without extra software, which is why salvage was dropped.)*

## Step C — flash on the Mac

1. **Raspberry Pi Imager** → *Choose Device* → **Raspberry Pi Zero** →
   *Choose OS* → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (32-bit)**
   (must read *Debian Trixie*, released 2026-06-18) → *Choose Storage* → the card.
2. Next → **Edit Settings**:
   * *General*: hostname `weeny`; set username `simon` + a password (this is the
     local/console emergency password — SSH itself is key-only); locale
     `Europe/London`, keyboard layout `gb`.
   * *General → Configure wireless LAN*: SSID `couldbe`, its PSK, Wireless LAN
     country `GB`.
   * *Services*: enable **SSH** → **Allow public-key authentication only** →
     paste steve's key:
     ```sh
     ssh steve cat .ssh/id_ed25519_weeny.pub
     ```
   * *Options*: **untick "Eject media when finished"**.
   Save → *Yes* to apply customisation → *Write*.
3. If the card ejected anyway, pull and reinsert so `/Volumes/bootfs` mounts.
   Sanity check:
   ```sh
   ls /Volumes/bootfs/{user-data,network-config,meta-data}
   grep -o 'ds=nocloud;i=[^ ]*' /Volumes/bootfs/cmdline.txt
   ```
4. Copy this folder and the secrets file onto the boot partition. The repo is
   public, so fetch it straight onto the Mac:
   ```sh
   git clone --depth 1 https://github.com/soomin10000/index /tmp/index
   cp -r /tmp/index/home_menu/pollers/weeny /Volumes/bootfs/weeny
   cp /Volumes/bootfs/weeny/secrets.env.example /Volumes/bootfs/weeny/secrets.env
   # edit /Volumes/bootfs/weeny/secrets.env -> set COMITUP_AP_PASSWORD=<hotspot WPA2 key>
   ```
5. **Add the first-boot hook to `user-data`.** Imager already wrote a `runcmd:`
   list (it contains `- [ systemctl, enable, --now, ssh ]`). Our line goes at the
   **end of that same list** — a second top-level `runcmd:` key silently replaces
   Imager's and kills the SSH-enable.

   Try the automatic insert first:
   ```sh
   cd /Volumes/bootfs && cp user-data ~/weeny-user-data.bak
   awk -v item='  - [ bash, /boot/firmware/weeny/provision.sh ]' '
     inr && /^[^[:space:]#-]/ { print item; inr=0 }
     /^runcmd:/ { inr=1; seen=1 }
     { print }
     END { if (inr) print item; if (!seen) { print "runcmd:"; print item } }
   ' ~/weeny-user-data.bak > user-data
   ```
   Then **verify** — exactly one `runcmd:`, our line last in it:
   ```sh
   grep -c '^runcmd:' user-data          # must print 1
   grep -n -A8 '^runcmd:' user-data
   ```
   If it looks wrong: `cp ~/weeny-user-data.bak user-data` and hand-edit — open
   `user-data`, find `runcmd:`, add `  - [ bash, /boot/firmware/weeny/provision.sh ]`
   as the last list item (two-space indent, same as the existing entries).
6. `dot_clean -m /Volumes/bootfs && diskutil eject /Volumes/bootfs`. Insert the
   card in weeny, power on.
7. **Watch it:**
   ```sh
   ssh steve 'ssh-keygen -R 192.168.1.247; ssh-keygen -R weeny; ssh-keygen -R "[192.168.1.247]:2222"'
   # ~2 min: weeny.localdomain answers ping / shows in Pi-hole
   ssh weeny tail -f /boot/firmware/weeny/provision.log      # port 22 until stage 10
   ```
   Timeline: LAN ~2 min → sshd on **:22** ~3 min → provisioning ~25–35 min → an
   ntfy push to `steve_updates` → auto-reboot 60 s later → sshd on **:2222**.
   If the network never appears: pull the card, read
   `/Volumes/bootfs/weeny/provision.log` on the Mac.

### Re-run provisioning later (over SSH)

Idempotent — stages that are already done are skipped. Detach it, because the
comitup stage can bounce wlan0:
```sh
ssh weeny 'sudo systemd-run --unit weeny-provision --collect \
    bash /boot/firmware/weeny/provision.sh --force --no-reboot'
ssh weeny journalctl -fu weeny-provision
```

## Step D — verification (run the whole list yourself, from the Mac)

Probe **from the Mac** — it is not in OpenCanary's `ip.ignorelist` (only steve
`192.168.1.183` is), and this is how the persona was tested the first time. Do
**not** probe from steve; its hits never reach the log and the honeypot looks
dead. Add `Port 2222` to `Host weeny` in the Mac's `~/.ssh/config` first (or use
`ssh -p 2222` explicitly, as below).

**1. Cold boot** (pull power, replug — not `reboot`):
```sh
ssh -p 2222 weeny true && echo "SSH on 2222 OK"
ssh -p 2222 weeny 'sudo -n true' ; echo "expect: a password prompt / failure (no NOPASSWD)"
```

**2. On weeny:**
```sh
ssh -p 2222 weeny '
  echo "== listeners ==";       ss -ltnp | grep -E ":(21|22|23|80|2222) "
  echo "== addresses ==";       ip -4 -br addr show wlan0
  echo "== nonlocal_bind ==";   sysctl net.ipv4.ip_nonlocal_bind
  echo "== nft ==";             nft list table inet weeny
  echo "== units ==";           systemctl is-enabled opencanary comitup ssh nftables
  echo "== ssh.socket ==";      systemctl is-active ssh.socket ; echo "expect: inactive"
  echo "== marker ==";          cat /var/lib/weeny-provisioned
'
```
Expect: sshd on `*:2222` (and `[::]:2222`) only; `twistd` on `192.168.1.5:21/22/23/80`;
nothing on `*:22`; `wlan0` has `192.168.1.247/24` **and** `192.168.1.5/32`;
`ip_nonlocal_bind = 1`.

**3. Probe the persona from the Mac** (needs `nmap` — `brew install nmap`):
```sh
nmap -Pn -p 21,22,23,80,2222 192.168.1.5      # 21/22/23/80 open, 2222 filtered
nmap -Pn -p 22,2222 192.168.1.247             # 2222 open
ssh -p 22 -o StrictHostKeyChecking=no -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no admin@192.168.1.5    # type any junk password, then ^C
curl -s -m 5 http://192.168.1.5/ | head -c 200 ; echo
# then check the log:
ssh -p 2222 weeny 'grep -E "\"logtype\": (3000|4002)" /var/log/opencanary/opencanary.log | tail -3'
```
Expect a `logtype 4002` line whose `src_host` is the Mac's LAN IP from the SSH
attempt, and a `logtype 3000` from the curl. (A phone on the same Wi-Fi browsing
to `http://192.168.1.5/` and trying the fake NAS login is the most realistic
version of this test.)

**4. Dashboard** — from wherever the home-menu poller runs (steve):
```sh
ssh steve 'cd projects/home_menu && python3 pollers/honeypot.py'
```
Expect `attempts>0`; the index **Honeypot** card goes amber (connect) / red
(login) within 10 min; the `honeypot_unreachable` warning clears. (steve's own
SSH to weeny for this poll is ignorelisted, so it won't pollute the count.)

**5. comitup hotspot:**
```sh
ssh -p 2222 weeny 'sudo nmcli con down couldbe'    # or block the SSID at the AP
```
Within ~2 min a WPA2 SSID `weeny-<nnnn>` appears — join it from a phone,
`http://10.41.0.1/` should load (proves comitup-web got `:80`). On weeny:
`/run/comitup-hotspot` exists, `systemctl status opencanary` = inactive
(condition), **no ntfy storm**. Reconnect (`sudo nmcli con up couldbe` or pick
`couldbe` on the phone) → flag gone, opencanary active, `192.168.1.5` back
(dispatcher). `journalctl -t comitup-callback -t honeypot-ip` shows the events.

**6. Idempotency:** run the re-run command from above with `--force --no-reboot`
— every stage reports OK or skipped, `ip -4 addr show wlan0` has no duplicate
`192.168.1.5`, and your SSH session survives.

**7. sshd self-heal:**
```sh
ssh -p 2222 weeny 'sudo systemctl kill -s KILL ssh; sleep 6; systemctl show -p NRestarts --value ssh'
```
Expect a new connection to work within ~5 s and `NRestarts` ≥ 1.

## Step E — golden image ("frozen working state")

After a few quiet days. On weeny:
```sh
ssh -p 2222 weeny '
  sudo systemctl stop opencanary
  sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
  sudo truncate -s0 /var/log/opencanary/opencanary.log
  sudo rm -f /var/log/opencanary/*.gz /var/log/opencanary/*.1 \
             /var/log/comitup.log* /root/.bash_history /home/simon/.bash_history
  sudo apt-get clean
  sudo fstrim -v / || true
  sudo poweroff
'
```
Do **not** run `cloud-init clean` — a re-flash should boot straight to a working
honeypot in ~2 min (same host keys, so steve's `known_hosts` still matches;
`/var/lib/weeny-provisioned` present, so `provision.sh` no-ops). Do not boot the
card again before the dump.

On the Mac:
```sh
diskutil list                                   # find /dev/diskN
diskutil unmountDisk /dev/diskN
sudo dd if=/dev/rdiskN bs=4m status=progress | gzip -1 > ~/weeny-golden-$(date +%Y%m%d).img.gz
gzip -t ~/weeny-golden-*.img.gz && shasum -a 256 ~/weeny-golden-*.img.gz
scp ~/weeny-golden-*.img.gz* steve:/mnt/nas-home/backups/weeny/
```
**NAS only** — the image contains the Wi-Fi PSK, the password hash, the
authorized key and the hotspot password. Re-flash: Imager → *Use custom* → the
`.img.gz` → **"apply OS customisation?" = No**. Don't run this image on a second
Pi while weeny is also running (shared identity).

## steve-side (after weeny is on :2222)

* `~/.ssh/config` `Host weeny`: add `Port 2222`.
* `pollers/honeypot.py` needs no change (uses the `weeny` alias; `*/10` cron
  already exists).
* A `/weeny` host-stats dashboard card (Pi hardware health, like `/bazza`) is a
  follow-up once the box is verified up.
