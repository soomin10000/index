#!/usr/bin/env python3
"""Review installed apt packages for size and install date, to spot removal candidates.

Only looks at manually-installed packages (apt-mark showmanual) - i.e. things
someone explicitly chose to install, not pulled-in dependencies. Read-only:
it never removes anything, it just prints a sorted report.
"""
import gzip
import re
import subprocess
from pathlib import Path

APT_LOG_DIR = Path("/var/log/apt")


def manual_packages():
    out = subprocess.run(["apt-mark", "showmanual"], capture_output=True, text=True, check=True)
    return sorted(out.stdout.split())


def installed_sizes(packages):
    out = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Installed-Size}\n", *packages],
        capture_output=True, text=True, check=True,
    )
    sizes = {}
    for line in out.stdout.splitlines():
        pkg, kb = line.split("\t")
        sizes[pkg] = int(kb) if kb.isdigit() else 0
    return sizes


def install_dates(packages):
    """Best-effort install date per package, scraped from apt history logs."""
    dates = {pkg: None for pkg in packages}
    pattern = re.compile(r"^Start-Date:\s+(\S+\s+\S+)")
    log_files = sorted(APT_LOG_DIR.glob("history.log*"))
    for path in log_files:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", errors="ignore") as f:
            current_date = None
            for line in f:
                m = pattern.match(line)
                if m:
                    current_date = m.group(1)
                elif line.startswith("Install:") or line.startswith("Commandline:"):
                    for pkg in packages:
                        if re.search(rf"\b{re.escape(pkg)}[:(,]", line):
                            if dates.get(pkg) is None:
                                dates[pkg] = current_date
    return dates


def main():
    packages = manual_packages()
    sizes = installed_sizes(packages)
    dates = install_dates(packages)

    rows = []
    for pkg in packages:
        size_mb = sizes.get(pkg, 0) / 1024
        rows.append((size_mb, pkg, dates.get(pkg) or "unknown"))
    rows.sort(reverse=True)

    total_mb = sum(r[0] for r in rows)
    print(f"{len(rows)} manually-installed packages, {total_mb:.0f} MB total\n")
    print(f"{'SIZE (MB)':>10}  {'PACKAGE':<28} INSTALLED")
    for size_mb, pkg, date in rows:
        print(f"{size_mb:>10.1f}  {pkg:<28} {date}")


if __name__ == "__main__":
    main()
