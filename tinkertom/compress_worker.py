"""Isolate third-party compression so a failure or timeout preserves raw output."""
import json
from pathlib import Path
import sys

from .optimization import headroom_compress
from .storage import atomic_write


if __name__ == "__main__":
    path = Path(sys.argv[1])
    text, metrics = headroom_compress(path.read_text())
    atomic_write(path, text)
    print(json.dumps(metrics))
