import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT, _ROOT / "pollers"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
