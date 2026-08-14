#!/usr/bin/env python3
"""Mirror SmokePing's RRD files from noob (the NAS also running Plex) into
data/smokeping_rrd/, so /smokeping can chart real probe data instead of
proxying rrdtool-rendered PNGs from the box's CGI.

`rsync` fails whenever it's the literal first word of the SSH exec payload
(consistently "Permission denied, please try again." even with a correct
key — looks like a DSM-side command filter, not an auth problem: wrapping
the same rsync call in `sh -c` makes it work). Rather than chase that, a
plain tar pipe is simpler and, once the file list skips SmokePing's own
tree walk (see below), just as fast (~2s for the whole ~150MB tree).
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"
OUT_DIR = DATA / "smokeping_rrd"
STATUS_JSON = DATA / "smokeping_rrd.json"
REMOTE_DIR = "/volume1/docker/smokeping-speedtest/data"


def sync() -> int:
    tmp_dir = OUT_DIR.with_name(OUT_DIR.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    # `find | tar -T -` instead of `tar -C dir .` — a plain recursive tar
    # also walks Synology's @eaDir/#recycle metadata trees alongside the
    # data and took 2.5 minutes; listing just the *.rrd paths first is 2s.
    ssh = subprocess.Popen(
        ["ssh", "noob", f"cd {REMOTE_DIR} && find . -iname '*.rrd' | tar -cf - -T -"],
        stdout=subprocess.PIPE,
    )
    tar = subprocess.run(["tar", "-C", str(tmp_dir), "-xf", "-"], stdin=ssh.stdout)
    ssh.stdout.close()
    ssh.wait(timeout=30)
    if ssh.returncode != 0 or tar.returncode != 0:
        raise RuntimeError(f"sync failed: ssh={ssh.returncode} tar={tar.returncode}")

    count = sum(1 for _ in tmp_dir.rglob("*.rrd"))
    if count == 0:
        raise RuntimeError("synced zero .rrd files")

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    tmp_dir.rename(OUT_DIR)
    return count


def main():
    try:
        count = sync()
        status = {"ts": time.time(), "ok": True, "error": "", "rrd_count": count}
    except Exception as e:
        status = {"ts": time.time(), "ok": False, "error": str(e)}
    tmp = STATUS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    tmp.replace(STATUS_JSON)
    print(f"{time.strftime('%F %T')} ok={status['ok']} "
          f"count={status.get('rrd_count', 0)} {status.get('error', '')}")


if __name__ == "__main__":
    main()
