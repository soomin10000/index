#!/bin/bash
# Push pollers/v6health/v6probe.sh to ~/v6mon/ on steve (local) and bazza (scp).
# Re-run after editing the probe. Cron lines are managed by hand — see
# pollers/v6health/README.md.
set -eu
SRC="$(cd "$(dirname "$0")/.." && pwd)/pollers/v6health/v6probe.sh"
BAZZA_KEY="$HOME/.ssh/id_rsa_bazza"

echo "steve:"
mkdir -p "$HOME/v6mon"
install -m 755 "$SRC" "$HOME/v6mon/v6probe.sh"
echo "  installed $HOME/v6mon/v6probe.sh"

echo "bazza:"
ssh -i "$BAZZA_KEY" -o BatchMode=yes simon@192.168.1.246 'mkdir -p ~/v6mon'
scp -q -i "$BAZZA_KEY" "$SRC" simon@192.168.1.246:/home/simon/v6mon/v6probe.sh
ssh -i "$BAZZA_KEY" -o BatchMode=yes simon@192.168.1.246 'chmod 755 ~/v6mon/v6probe.sh && echo "  installed ~/v6mon/v6probe.sh"'
