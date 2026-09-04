# jeff readsb auto-heal

Source of truth for the files that live **on jeff** (not run from steve). They
keep the ADS-B receiver healthy without a human in the loop: the cheap Nooelec
RTL2838 stick wedges every day or two (readsb stays up but gets zero IQ samples),
and the only reliable fix is `usbreset 0bda:2838` + `systemctl restart readsb`.

| Repo file | Deployed to | Owner / mode |
|---|---|---|
| `readsb-recover` | `/usr/local/bin/readsb-recover` | root `0755` |
| `readsb-watchdog.sh` | `/usr/local/bin/readsb-watchdog` | root `0755` |
| `readsb-watchdog.service` | `/etc/systemd/system/readsb-watchdog.service` | root `0644` |
| `readsb-watchdog.timer` | `/etc/systemd/system/readsb-watchdog.timer` | root `0644` |
| `readsb-watchdog.logrotate` | `/etc/logrotate.d/readsb-watchdog` | root `0644` |

`readsb-recover` is the single recovery code path. It's called two ways:

- **automatically** by `readsb-watchdog` (root, via the timer) after two
  consecutive deaf checks and outside a 15-min cooldown;
- **manually** from the `/jeff` dashboard "Restart readsb" button, which runs
  `ssh jeff sudo -n /usr/local/bin/readsb-recover manual` from steve.

Both log to `/var/log/readsb-watchdog.log` and rewrite
`/var/lib/readsb-watchdog/state.json` (`last_fire`, `last_result`,
`last_trigger`, `fires_24h`) which `pollers/jeff.py` reads back onto the card.
Each run also sends one ntfy push to steve's server (topic `steve_updates`).

## Install / update (run on jeff)

```sh
cd ~/jeff-readsb-autoheal            # staged here by steve (scp from home_menu/pollers/jeff/)
sudo install -m0755 readsb-recover        /usr/local/bin/readsb-recover
sudo install -m0755 readsb-watchdog.sh    /usr/local/bin/readsb-watchdog
sudo install -m0644 readsb-watchdog.service /etc/systemd/system/readsb-watchdog.service
sudo install -m0644 readsb-watchdog.timer   /etc/systemd/system/readsb-watchdog.timer
sudo install -m0644 readsb-watchdog.logrotate /etc/logrotate.d/readsb-watchdog

# one scoped sudo grant for the dashboard button (exact string match, incl. "manual")
echo 'simon ALL=(root) NOPASSWD: /usr/local/bin/readsb-recover manual' \
  | sudo tee /etc/sudoers.d/readsb-recover
sudo chmod 0440 /etc/sudoers.d/readsb-recover
sudo visudo -c

sudo systemctl daemon-reload
sudo systemctl enable --now readsb-watchdog.timer
```

## Verify

```sh
# healthy -> no fire
sudo /usr/local/bin/readsb-watchdog; tail -n2 /var/log/readsb-watchdog.log

# force a wedge -> timer heals it within ~4 min, one ntfy on the phone
sudo systemctl stop readsb
systemctl list-timers readsb-watchdog.timer
# ...wait...
journalctl -u readsb-watchdog.service -n20 --no-pager
cat /var/lib/readsb-watchdog/state.json

# manual path (proves the sudoers line)
ssh jeff sudo -n /usr/local/bin/readsb-recover manual
```

## Notes

- The watchdog runs as **root via systemd**, so it needs no sudoers entry; the
  only new sudo grant is the single line above for the button.
- `usbreset` + USB re-enumerate is as far as software recovery goes — there's no
  addressable hub to power-cycle the port. If `readsb-recover` starts logging
  `still deaf`, the `/jeff` card raises a `readsb_flapping` warn (>=4 heals in
  24h): time to reseat or swap the £30 stick.
- Deaf thresholds live at the top of `readsb-watchdog.sh`
  (`MIN_MSGS_PER_MIN`, `STALE_SEC`, `COOLDOWN`) and mirror `DEAF_MSGS_PER_SEC`
  / `ADSB_STALE_SEC` in `pollers/jeff.py`.
