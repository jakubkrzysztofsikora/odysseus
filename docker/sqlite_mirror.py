#!/usr/bin/env python3
"""Run SQLite locally and mirror a consistent backup to mounted storage."""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path


SOURCE = Path(os.getenv("ODYSSEUS_SQLITE_MIRROR_SOURCE", "/app/data/app.db"))
LOCAL = Path(os.getenv("ODYSSEUS_SQLITE_MIRROR_LOCAL", "/tmp/odysseus-data/app.db"))
INTERVAL = max(5.0, float(os.getenv("ODYSSEUS_SQLITE_MIRROR_INTERVAL_SECONDS", "15")))
TIMEOUT = max(1.0, float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30")))


def _log(message: str) -> None:
    print(f"[sqlite-mirror] {message}", file=sys.stderr, flush=True)


def restore() -> None:
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    if LOCAL.exists():
        _log(f"using existing local database {LOCAL}")
        return
    if not SOURCE.exists():
        _log(f"source database {SOURCE} does not exist; app will initialize a new local database")
        return

    shutil.copy2(SOURCE, LOCAL)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(SOURCE) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(LOCAL) + suffix))
    _log(f"restored {SOURCE} to {LOCAL}")


def backup_once() -> bool:
    if not LOCAL.exists():
        return False

    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SOURCE.with_name(SOURCE.name + ".tmp")
    try:
        if tmp.exists():
            tmp.unlink()

        src = sqlite3.connect(str(LOCAL), timeout=TIMEOUT)
        try:
            dst = sqlite3.connect(str(tmp), timeout=TIMEOUT)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        os.replace(tmp, SOURCE)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(SOURCE) + suffix)
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError:
                    pass
        return True
    except Exception as exc:  # pragma: no cover - operational logging only
        _log(f"backup failed: {exc}")
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def backup_loop(stop: threading.Event) -> None:
    while not stop.wait(INTERVAL):
        if backup_once():
            _log(f"backed up {LOCAL} to {SOURCE}")


def run(argv: list[str]) -> int:
    if not argv:
        _log("missing child command")
        return 2

    restore()
    stop = threading.Event()
    thread = threading.Thread(target=backup_loop, args=(stop,), daemon=True)
    thread.start()

    child = subprocess.Popen(argv)

    def _forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    try:
        return_code = child.wait()
    finally:
        stop.set()
        thread.join(timeout=5)
        if backup_once():
            _log(f"final backup {LOCAL} to {SOURCE}")
    return return_code


def main(argv: list[str]) -> int:
    if not argv:
        _log("usage: sqlite_mirror.py restore|backup|run -- command ...")
        return 2
    command = argv[0]
    if command == "restore":
        restore()
        return 0
    if command == "backup":
        return 0 if backup_once() else 1
    if command == "run":
        child_args = argv[1:]
        if child_args[:1] == ["--"]:
            child_args = child_args[1:]
        return run(child_args)
    _log(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
