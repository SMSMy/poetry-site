#!/usr/bin/env python3
"""Windows launcher + supervisor for the batch-4 remote D1 import.

WHAT THIS IS
------------
The import is `poetry-static/.d1-final/batch4_runner.py`, written for Linux and
driven by `poetry-static/.d1-final/batch4_supervisor.sh` (bash). This launcher
runs that SAME, unmodified runner on Windows by fixing the three
platform-bound pieces around it:

  1. `fcntl.flock` - `import fcntl` raises ModuleNotFoundError on Windows and
     the runner dies before it touches the database. A tiny `fcntl` shim
     (`win_shim/fcntl.py`, msvcrt-based) is injected through PYTHONPATH for the
     child process only.
  2. `/tmp/opencode/...` - the runner's default log/debug paths are POSIX. The
     log is placed next to the data tree instead.
  3. `npx` - on Windows `npx` is a `.cmd` shim, and CreateProcess cannot
     execute it. The real executable (`node <wrangler>/bin/wrangler.js`) is
     passed through the environment variable BATCH4_WRANGLER_CMD (JSON list),
     which also survives paths that contain spaces.

The runner's own logic is never rewritten. `batch4_state.json` stays the single
source of truth, so a run started on Linux can be resumed on Windows and back.

TWO OPTIONAL, REVERSIBLE PATCHES
--------------------------------
`batch4_runner.py` and `import_huge_rows.py` both shell out to `npx wrangler`.
To let the environment variable win over the hardcoded default, two tiny
patches are needed inside the data tree (not in this repository):

  * `batch4_runner.py`      -> `self.wrangler = _wrangler_argv(args.wrangler)`
  * `import_huge_rows.py`   -> `sh(_wrangler_argv(["npx", "wrangler"]) + [...])`

They are applied ONLY with `--apply-patches`, each one is idempotent, and each
one keeps a `<file>.orig` backup so it can be reverted by copying it back.
They are NOT needed under WSL (fcntl and npx are native there).

USAGE
-----
    # 0) what is missing on this machine?
    python scripts/batch4_windows/batch4_windows.py --data-root C:\\poetry-static --check

    # 1) one-time: make the data tree Windows-ready (reversible)
    python scripts/batch4_windows/batch4_windows.py --data-root C:\\poetry-static --apply-patches

    # 2) run the import, supervised (same stop rules as the bash supervisor)
    python scripts/batch4_windows/batch4_windows.py --data-root C:\\poetry-static --supervise

    # 3) plan only, no writes
    python scripts/batch4_windows/batch4_windows.py --data-root C:\\poetry-static --dry-run

    # 4) where did we stop?
    python scripts/batch4_windows/batch4_windows.py --data-root C:\\poetry-static --status

`--data-root` is the `poetry-static` folder (the one holding `.d1-final/` and
`.cache/`). When omitted, a sibling `poetry-static` next to this repository is
used. `--normalize-dir` points at `poetry-site/scripts` (it must hold
`normalize_ar.py`); it defaults to `<data-root>/../poetry-site/scripts`.

Stop rules (identical to `batch4_supervisor.sh`): the loop ends when the runner
exits non-zero, or when the stage becomes `awaiting_cutover` / `failed`.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                 # poetry-site
SHIM_DIR = HERE / "win_shim"

DEFAULT_DB = "poetry-norm-contentless"
DEFAULT_MAX_WRITES = 85_000
DEFAULT_POLL_SECONDS = 600
SUPERVISOR_SLEEP = 300
STOP_STAGES = {"awaiting_cutover", "failed"}

# Daily row-write cap used by the runner.
#
# The runner ships with 100,000 rows/day, which is the WORKERS FREE plan limit.
# This Cloudflare account is on WORKERS PAID: D1 writes kept succeeding well past
# 100k rows/24h (137,653 observed, zero errors), and Cloudflare enforces the free
# daily stop since 2026-09-01, so a free account would be failing by now.
# Paid has no daily stop - only monthly included writes (50M) then overage -
# so the 100k cap only throttled the import to a trickle.
#
# 1,000,000 rows/day keeps a runaway guard while letting the import finish.
# On a Workers FREE account use --d1-quota 100000 instead.
DEFAULT_D1_QUOTA = 1_000_000
DEFAULT_D1_QUOTA_MARGIN = 5_000

# --------------------------------------------------------------------- helpers


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def find_data_root(explicit: str | None) -> Path:
    """Locate the `poetry-static` tree (the one with .d1-final/ and .cache/)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates = [
        REPO_ROOT.parent / "poetry-static",
        REPO_ROOT / "poetry-static",
        REPO_ROOT.parent / "poetry-static" / "poetry-static",
    ]
    for candidate in candidates:
        if (candidate / ".d1-final").is_dir():
            return candidate.resolve()
    # last resort: walk up looking for a sibling poetry-static
    for parent in [REPO_ROOT, *REPO_ROOT.parents]:
        candidate = parent / "poetry-static"
        if (candidate / ".d1-final").is_dir():
            return candidate.resolve()
    raise SystemExit("could not find the poetry-static data tree; pass --data-root")


def resolve_paths(data_root: Path, normalize_dir: str | None) -> dict:
    d1 = data_root / ".d1-final"
    paths = {
        "data_root": data_root,
        "d1_final": d1,
        "runner_py": d1 / "batch4_runner.py",
        "state": d1 / "batch4_state.json",
        "batches": d1 / "batch4-batches",
        "builder": d1 / "batch4_build_batches.py",
        "huge": d1 / "import_huge_rows.py",
        "worker_dir": data_root / ".d1-impworker-b4",
        "source": data_root / ".cache" / "reconstruction.sqlite",
        # wrangler must run from the data root: .d1-final/wrangler.toml holds a
        # FAKE database_id, so running it from there would target the wrong DB.
        "wrangler_cwd": data_root,
        "normalize_dir": Path(normalize_dir).resolve() if normalize_dir
        else (data_root.parent / "poetry-site" / "scripts"),
        "log": d1 / "batch4_runner.log",
        "debug": d1 / "batch4_debug",
    }
    return paths


def wrangler_js_candidates() -> list[Path]:
    """Every place a real `wrangler.js` entry point may live on Windows."""
    out: list[Path] = []
    for base in (REPO_ROOT, REPO_ROOT.parent, Path.cwd()):
        out.append(base / "node_modules" / "wrangler" / "bin" / "wrangler.js")
    # global npm root
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        try:
            proc = subprocess.run([npm, "root", "-g"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", check=False, timeout=120)
            root = (proc.stdout or "").strip().splitlines()
            if root:
                out.append(Path(root[-1]) / "wrangler" / "bin" / "wrangler.js")
        except (OSError, subprocess.SubprocessError):
            pass
    # npx cache
    for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "npm-cache" / "_npx",
                 Path(os.environ.get("USERPROFILE", "")) / ".npm" / "_npx",
                 Path(os.environ.get("HOME", "")) / ".npm" / "_npx"):
        if base.is_dir():
            for entry in sorted(base.glob("*/node_modules/wrangler/bin/wrangler.js")):
                out.append(entry)
    return out


def resolve_wrangler(explicit: str | None) -> list[str]:
    """The argv prefix that runs wrangler on this machine.

    `node <wrangler.js>` is preferred: it is a real executable, so
    subprocess can run it without a shell and paths with spaces stay intact.
    """
    if explicit:
        text = explicit.strip()
        if text.startswith("["):
            return [str(item) for item in json.loads(text)]
        return shlex.split(text)
    node = shutil.which("node.exe") or shutil.which("node")
    if node:
        for js in wrangler_js_candidates():
            if js.is_file():
                return [node, str(js)]
    if os.name != "nt":
        return ["npx", "wrangler"]
    cmd = shutil.which("cmd.exe")
    return [cmd or "cmd.exe", "/c", "npx", "wrangler"]


# ---------------------------------------------------------------------- patches

RUNNER_HELPER = '''def _wrangler_argv(default: str | list[str] | None = None) -> list[str]:
    """Windows: BATCH4_WRANGLER_CMD (a JSON list) overrides --wrangler.

    `npx` is a .cmd shim on Windows and CreateProcess cannot execute it, so the
    launcher passes the real executable (node <wrangler>/bin/wrangler.js)
    through the environment instead of the command line.
    """
    raw = os.environ.get("BATCH4_WRANGLER_CMD")
    if raw:
        try:
            argv = json.loads(raw)
        except json.JSONDecodeError:
            argv = None
        if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
            return list(argv)
    if isinstance(default, str):
        return shlex.split(default)
    return list(default or [])


'''

HUGE_HELPER = '''def _wrangler_argv(default: list[str]) -> list[str]:
    """Windows: BATCH4_WRANGLER_CMD (a JSON list) overrides the default `npx wrangler`.

    `npx` is a .cmd shim on Windows and CreateProcess cannot execute it, so the
    launcher passes the real executable (node <wrangler>/bin/wrangler.js)
    through the environment instead.
    """
    raw = os.environ.get("BATCH4_WRANGLER_CMD")
    if raw:
        try:
            argv = json.loads(raw)
        except json.JSONDecodeError:
            argv = None
        if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
            return list(argv)
    return list(default)


'''


def _write_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings (the data tree uses LF; CRLF would be noise)."""
    path.write_bytes(text.encode("utf-8"))


def patch_runner(path: Path) -> tuple[bool, str]:
    """Make batch4_runner.py honour BATCH4_WRANGLER_CMD."""
    text = path.read_text(encoding="utf-8")
    if "_wrangler_argv" in text:
        return False, "already patched"
    original = "        self.wrangler = shlex.split(args.wrangler)\n"
    if original not in text:
        return False, "anchor not found (runner already changed?)"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        _write_lf(backup, text)
    text = text.replace(original, "        self.wrangler = _wrangler_argv(args.wrangler)\n", 1)
    anchor = "def now() -> str:"
    if anchor not in text:
        return False, "helper anchor not found"
    text = text.replace(anchor, RUNNER_HELPER + anchor, 1)
    _write_lf(path, text)
    return True, "patched (backup: %s)" % backup.name


def patch_huge(path: Path) -> tuple[bool, str]:
    """Make import_huge_rows.py honour BATCH4_WRANGLER_CMD."""
    text = path.read_text(encoding="utf-8")
    if "_wrangler_argv" in text:
        return False, "already patched"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        _write_lf(backup, text)
    # 1) os is needed for the environment lookup
    if "\nimport os\n" not in text:
        if "\nimport json\n" not in text:
            return False, "import anchor not found"
        text = text.replace("\nimport json\n", "\nimport json\nimport os\n", 1)
    # 2) the helper itself
    anchor = "def sh(cmd: list[str], cwd: Path) -> str:"
    if anchor not in text:
        return False, "helper anchor not found"
    text = text.replace(anchor, HUGE_HELPER + anchor, 1)
    # 3) the two call sites
    calls = [
        ('sh(["npx", "wrangler", "deploy"], cwd=worker_dir)',
         'sh(_wrangler_argv(["npx", "wrangler"]) + ["deploy"], cwd=worker_dir)'),
        ('sh(["npx", "wrangler", "delete", name, "--force"], cwd=worker_dir)',
         'sh(_wrangler_argv(["npx", "wrangler"]) + ["delete", name, "--force"], cwd=worker_dir)'),
    ]
    for old, new in calls:
        if old not in text:
            return False, "call site not found: %s" % old
        text = text.replace(old, new, 1)
    _write_lf(path, text)
    return True, "patched (backup: %s)" % backup.name


QUOTA_HELPER = '''def _env_int(name: str, default: int) -> int:
    """Read an integer tuning knob from the environment (Windows launcher).

    `BATCH4_D1_QUOTA` overrides the daily row-write cap the runner assumes.
    The shipped default (100,000 rows/day) is the Workers FREE plan limit;
    this account is on Workers Paid, where D1 has no daily stop.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


'''


def patch_runner_quota(path: Path) -> tuple[bool, str]:
    """Let the daily row-write cap come from BATCH4_D1_QUOTA."""
    text = path.read_text(encoding="utf-8")
    if "BATCH4_D1_QUOTA" in text:
        return False, "already patched"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        _write_lf(backup, text)
    import_anchor = "from datetime import datetime, timezone\nfrom pathlib import Path\n"
    if import_anchor not in text:
        return False, "import anchor not found"
    text = text.replace(import_anchor, import_anchor + "\n" + QUOTA_HELPER, 1)
    old_quota = "D1_QUOTA = 100_000\nQUOTA_MARGIN = 5_000\n"
    new_quota = ('D1_QUOTA = _env_int("BATCH4_D1_QUOTA", 100_000)\n'
                 'QUOTA_MARGIN = _env_int("BATCH4_D1_QUOTA_MARGIN", 5_000)\n')
    if old_quota not in text:
        return False, "quota constants not found"
    text = text.replace(old_quota, new_quota, 1)
    _write_lf(path, text)
    return True, "patched (backup: %s)" % backup.name


def apply_patches(paths: dict) -> int:
    print("== applying Windows patches to the data tree ==")
    failed = 0
    for key, func in (("runner_py", patch_runner),
                      ("runner_py", patch_runner_quota),
                      ("huge", patch_huge)):
        target = paths[key]
        if not target.exists():
            print("  MISSING  %s" % target)
            failed += 1
            continue
        try:
            changed, message = func(target)
        except OSError as exc:
            print("  FAILED   %s: %s" % (target, exc))
            failed += 1
            continue
        print("  %-8s %s (%s)" % ("PATCHED" if changed else "SKIPPED", target.name, message))
    if failed:
        print("  -> %d file(s) could not be patched; see messages above." % failed)
    return failed


# ------------------------------------------------------------------- execution


def build_env(paths: dict, wrangler: list[str], args) -> dict:
    env = dict(os.environ)
    # 1) the fcntl shim, for the child process only
    env["PYTHONPATH"] = os.pathsep.join([str(SHIM_DIR), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    # 2) the real wrangler executable, inherited by the grandchild
    env["BATCH4_WRANGLER_CMD"] = json.dumps(wrangler)
    # 3) the private lock file the shim uses
    env["BATCH4_LOCK_PATH"] = str(Path(paths["state"]).with_suffix(".runner.lock.win"))
    # 4) the daily row-write cap (100k = Workers Free; this account is Paid)
    env["BATCH4_D1_QUOTA"] = str(args.d1_quota)
    env["BATCH4_D1_QUOTA_MARGIN"] = str(args.d1_quota_margin)
    # 5) UTF-8 everywhere (the console code page on Windows is not UTF-8)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def build_child_argv(paths: dict, args) -> list[str]:
    argv = [
        sys.executable, "-u", str(paths["runner_py"]),
        "--state", str(paths["state"]),
        "--batches-dir", str(paths["batches"]),
        "--source", str(paths["source"]),
        "--builder", str(paths["builder"]),
        "--huge-script", str(paths["huge"]),
        "--worker-dir", str(paths["worker_dir"]),
        "--normalize-dir", str(paths["normalize_dir"]),
        "--wrangler-cwd", str(paths["wrangler_cwd"]),
        "--log", str(paths["log"]),
        "--db", args.db,
        "--max-writes", str(args.max_writes),
        "--poll-seconds", str(args.poll_seconds),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    return argv


def read_state(paths: dict) -> dict:
    try:
        return json.loads(Path(paths["state"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def supervise(paths: dict, args, wrangler: list[str]) -> int:
    """Same contract as batch4_supervisor.sh: restart on a clean pause, stop on
    failure / awaiting_cutover / failed."""
    env = build_env(paths, wrangler, args)
    argv = build_child_argv(paths, args)
    print("== batch-4 windows supervisor ==")
    print("  runner   : %s" % paths["runner_py"])
    print("  state    : %s" % paths["state"])
    print("  log      : %s" % paths["log"])
    print("  wrangler : %s" % " ".join(wrangler))
    print("  cwd      : %s" % paths["d1_final"])
    while True:
        proc = subprocess.run(argv, cwd=str(paths["d1_final"]), env=env)
        rc = proc.returncode
        state = read_state(paths)
        stage = state.get("stage", "unknown")
        status = state.get("status", "unknown")
        print("[supervisor %s] runner exited rc=%s stage=%s status=%s"
              % (now(), rc, stage, status), flush=True)
        if rc != 0 or stage in STOP_STAGES:
            return rc
        time.sleep(SUPERVISOR_SLEEP)


def run_once(paths: dict, args, wrangler: list[str]) -> int:
    env = build_env(paths, wrangler, args)
    argv = build_child_argv(paths, args)
    proc = subprocess.run(argv, cwd=str(paths["d1_final"]), env=env)
    return proc.returncode


def show_status(paths: dict) -> None:
    state = read_state(paths)
    if not state:
        print("no state file at %s" % paths["state"])
        return
    print("== batch-4 state ==")
    for key in ("db", "stage", "status", "paused", "paused_reason", "ddl_done",
                "writes_run", "writes_measured_total", "quota_probes", "updated_at"):
        if key in state:
            print("  %-22s %s" % (key, state[key]))
    for key in ("fts", "meta", "huge"):
        if key in state and isinstance(state[key], dict):
            print("  %-22s %s" % (key, json.dumps(state[key], ensure_ascii=False)))
    quota = state.get("last_quota")
    if quota:
        print("  %-22s %s" % ("last_quota", json.dumps(quota, ensure_ascii=False)))
    err = state.get("last_error")
    if err:
        print("  %-22s %s" % ("last_error", json.dumps(err, ensure_ascii=False)[:400]))


def run_check(paths: dict, wrangler: list[str], args) -> int:
    print("== environment check ==")
    problems = 0
    required = {
        "runner_py": "batch4_runner.py",
        "state": "batch4_state.json",
        "batches": "batch4-batches/",
        "source": ".cache/reconstruction.sqlite",
        "worker_dir": ".d1-impworker-b4/",
        "normalize_dir": "poetry-site/scripts/",
    }
    for key, label in required.items():
        target = paths[key]
        ok = target.exists()
        print("  %-9s %-32s %s" % ("OK" if ok else "MISSING", label, target))
        if not ok:
            problems += 1
    fts_index = paths["batches"] / "fts" / "index.json"
    if fts_index.exists():
        print("  %-9s %-32s %s" % ("OK", "fts index", fts_index))
    else:
        print("  %-9s %-32s %s" % ("MISSING", "fts index", fts_index))
        problems += 1
    if (paths["normalize_dir"] / "normalize_ar.py").exists():
        print("  %-9s %-32s %s" % ("OK", "normalize_ar.py", paths["normalize_dir"]))
    else:
        print("  %-9s %-32s %s" % ("MISSING", "normalize_ar.py", paths["normalize_dir"]))
        problems += 1
    # wrangler
    node = shutil.which("node.exe") or shutil.which("node")
    print("  %-9s %-32s %s" % ("OK" if node else "MISSING", "node", node or "install Node.js"))
    if not node:
        problems += 1
    print("  %-9s %-32s %s" % ("OK", "wrangler command", " ".join(wrangler)))
    if wrangler[0].lower().endswith("cmd.exe"):
        print("  %-9s %-32s %s"
              % ("WARN", "wrangler fallback",
                 "cmd.exe /c npx wrangler - unsafe if a path contains spaces"))
    # the fake wrangler.toml trap
    fake = paths["d1_final"] / "wrangler.toml"
    if fake.exists():
        print("  %-9s %-32s %s"
              % ("WARN", "fake wrangler.toml",
                 "%s holds a FAKE database_id - never run wrangler from there" % fake))
    # shim
    shim = SHIM_DIR / "fcntl.py"
    print("  %-9s %-32s %s" % ("OK" if shim.exists() else "MISSING", "fcntl shim", shim))
    if not shim.exists():
        problems += 1
    # patches
    for key in ("runner_py", "huge"):
        target = paths[key]
        if target.exists():
            done = "_wrangler_argv" in target.read_text(encoding="utf-8")
            print("  %-9s %-32s %s"
                  % ("OK" if done else "TODO", "%s patch" % target.name,
                     "applied" if done else "run with --apply-patches"))
    # the daily write cap the runner will assume
    runner_text = ""
    try:
        runner_text = paths["runner_py"].read_text(encoding="utf-8")
    except OSError:
        pass
    quota_patched = "BATCH4_D1_QUOTA" in runner_text
    print("  %-9s %-32s %s"
          % ("OK" if quota_patched else "TODO", "quota override patch",
             "applied" if quota_patched else "run with --apply-patches"))
    print("  %-9s %-32s %d rows/day"
          % ("OK", "effective daily quota", args.d1_quota))
    # logs
    try:
        paths["log"].parent.mkdir(parents=True, exist_ok=True)
        print("  %-9s %-32s %s" % ("OK", "log dir", paths["log"].parent))
    except OSError as exc:
        print("  %-9s %-32s %s" % ("FAILED", "log dir", exc))
        problems += 1
    print("== %d problem(s) ==" % problems)
    return problems


# -------------------------------------------------------------------------- cli


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Windows launcher/supervisor for the batch-4 D1 import.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", help="path to the poetry-static tree")
    parser.add_argument("--normalize-dir", help="path to poetry-site/scripts (normalize_ar.py)")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES)
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--d1-quota", type=int, default=DEFAULT_D1_QUOTA,
                        help="daily row-write cap the runner assumes "
                             "(Workers Free = 100000; Workers Paid = no daily stop)")
    parser.add_argument("--d1-quota-margin", type=int, default=DEFAULT_D1_QUOTA_MARGIN,
                        help="rows kept in reserve below the cap")
    parser.add_argument("--wrangler", help="wrangler command (JSON list or a plain command)")
    parser.add_argument("--apply-patches", action="store_true",
                        help="patch the data tree for Windows (reversible, .orig backups)")
    parser.add_argument("--supervise", action="store_true", help="run the supervisor loop")
    parser.add_argument("--run", action="store_true", help="run the runner once")
    parser.add_argument("--dry-run", action="store_true", help="plan only, no writes")
    parser.add_argument("--check", action="store_true", help="check this machine")
    parser.add_argument("--status", action="store_true", help="print the batch-4 state")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data_root = find_data_root(args.data_root)
    paths = resolve_paths(data_root, args.normalize_dir)
    wrangler = resolve_wrangler(args.wrangler)

    if args.status:
        show_status(paths)
        return 0
    if args.apply_patches:
        return 1 if apply_patches(paths) else 0
    if args.check:
        return 1 if run_check(paths, wrangler, args) else 0
    if args.supervise:
        return supervise(paths, args, wrangler)
    if args.run:
        return run_once(paths, args, wrangler)
    # default: --dry-run is the safe no-write default
    args.dry_run = True
    print("== dry run (no writes) ==")
    return run_once(paths, args, wrangler)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
