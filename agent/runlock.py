"""Only one run at a time: two runs would read the same open tickets and both answer them."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"  # local files; never in git


@contextmanager
def run_lock(directory: Path | str = STATE_DIR) -> Generator[None, None, None]:
    """Hold state/run.lock for one run. The operating system frees it when the process ends, also after a crash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            raise SystemExit(f"Another run (pid {lock.read().strip()}) is in progress.") from None
        lock.truncate(0)
        lock.write(str(os.getpid()))  # so the message above can name the run
        lock.flush()
        yield
