"""Windows shim for the POSIX `fcntl` module (batch-4 runner lock).

Why this exists
---------------
`poetry-static/.d1-final/batch4_runner.py` guards itself with:

    import fcntl
    fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

`fcntl` does not exist on Windows, so `import fcntl` raises ModuleNotFoundError
and the runner dies before it touches the database. The launcher
(`scripts/batch4_windows/batch4_windows.py`) puts this directory on PYTHONPATH
for the child process only, so `import fcntl` resolves here.

How it works
------------
`flock()` maps onto `msvcrt.locking()` (a byte-range lock) taken on a PRIVATE
lock file - `<state>.runner.lock.win` - not on the runner's own
`<state>.runner.lock`. That distinction matters: the runner truncates its own
`.lock` file to rewrite the PID, and a byte-range lock over a truncated file is
not guaranteed to survive the truncation. The private file is never rewritten,
so the lock is stable.

Windows releases byte-range locks when the owning process exits, so a crashed
or killed runner never leaves a stale lock behind - there is nothing to clean
up by hand.

On POSIX this file simply re-exports the real `fcntl` (loaded from the stdlib
directory by path, because this module may shadow it on sys.path).
"""
from __future__ import annotations

import os
from pathlib import Path

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if os.name != "nt":
    # POSIX: load the genuine stdlib module by path (this file may shadow it).
    import importlib.util
    import sysconfig

    _stdlib = Path(sysconfig.get_paths()["stdlib"]) / "fcntl.py"
    _spec = importlib.util.spec_from_file_location("_real_fcntl", _stdlib)
    if _spec is not None and _spec.loader is not None:
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        flock = _real.flock
        LOCK_SH = _real.LOCK_SH
        LOCK_EX = _real.LOCK_EX
        LOCK_NB = _real.LOCK_NB
        LOCK_UN = _real.LOCK_UN
    else:  # pragma: no cover - stdlib always ships fcntl.py on POSIX
        def flock(fd, operation):  # type: ignore[misc]
            raise OSError("fcntl shim: real fcntl could not be loaded")
else:
    import msvcrt

    _handle = None
    _path = None

    def _private_lock_file() -> Path:
        """`<state>.runner.lock` -> `<state>.runner.lock.win` (same folder)."""
        global _path
        if _path is None:
            raw = os.environ.get("BATCH4_LOCK_PATH")
            if not raw:
                raise OSError("fcntl shim: BATCH4_LOCK_PATH is not set")
            _path = Path(raw)
        return _path

    def _open_handle():
        global _handle
        if _handle is None:
            path = _private_lock_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+b")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            _handle = handle
        return _handle

    def flock(fd, operation):
        """Emulate fcntl.flock(fd, LOCK_EX|LOCK_NB) with msvcrt."""
        handle = _open_handle()
        os.lseek(handle.fileno(), 0, os.SEEK_SET)
        if operation & LOCK_UN:
            mode = msvcrt.LK_UNLCK
        elif operation & LOCK_NB:
            mode = msvcrt.LK_NBLCK
        else:
            mode = msvcrt.LK_LOCK
        # msvcrt.locking raises OSError when the region is already locked,
        # which is exactly what the runner expects for LOCK_NB.
        msvcrt.locking(handle.fileno(), mode, 1)
