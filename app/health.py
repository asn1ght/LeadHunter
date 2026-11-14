"""Healthcheck для Docker: планировщик раз в минуту обновляет data/heartbeat.

`python -m app.health` → код 0, если отметка свежая (бот и его event loop живы), иначе 1.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from app.config import DATA_DIR

HEARTBEAT = DATA_DIR / "heartbeat"
MAX_AGE_S = 180


def beat(path: Path = HEARTBEAT) -> None:
    """Отметиться: бот жив."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def is_healthy(path: Path = HEARTBEAT, max_age_s: int = MAX_AGE_S, now: float | None = None) -> bool:
    try:
        last = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (now if now is not None else time.time()) - last <= max_age_s


def main() -> None:
    sys.exit(0 if is_healthy() else 1)


if __name__ == "__main__":
    main()
