#!/usr/bin/env python3
"""Create the final site archive (tar.gz, compresslevel=1) from public-final and report sizes.

Before packing, the tracked files under `static/` are restored from git HEAD automatically
(never from the dirty working tree) and written where Hugo puts them in the built site
(`static/css/style.css` -> `css/style.css`, `static/js/search.js` -> `js/search.js`), so the
deployed assets always match the commit that builds the release. A legacy `public-final/static/`
copy is removed if present. The deploy workflow re-verifies `css/style.css` and `js/search.js`
against the same files in the repository by SHA-256 after extraction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "public-final"
OUT = ROOT / "public-final.tar.gz"
STATIC_REL = "static"


def _git(args: list[str]) -> bytes:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace").strip() or "git failed")
    return proc.stdout


def head_commit() -> str:
    return _git(["rev-parse", "HEAD"]).decode().strip()


def _clear_legacy_static_dir(site: Path) -> bool:
    """Hugo copies `static/*` to the site root, so `site/static/` must never ship."""
    import shutil

    legacy = site / STATIC_REL
    if legacy.is_dir():
        shutil.rmtree(legacy)
        return True
    return False


def sync_static_from_head(site: Path, ref: str = "HEAD") -> dict:
    """Write `static/` exactly as stored at `ref` into the site root (static/css/x -> css/x)."""
    listed = _git(["ls-tree", "-r", "-z", "--name-only", ref, "--", STATIC_REL]).split(b"\0")
    rels = [p.decode("utf-8") for p in listed if p]
    if not rels:
        raise RuntimeError(f"no tracked files under {STATIC_REL}/ at {ref}")
    removed_legacy = _clear_legacy_static_dir(site)
    digests: dict[str, dict] = {}
    for rel in rels:
        data = _git(["show", f"{ref}:{rel}"])
        target = site / Path(rel).relative_to(STATIC_REL)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                        "site_path": str(target.relative_to(site))}
    return {"ref": ref, "legacy_static_dir_removed": removed_legacy, "files": digests}


def sync_static_from_worktree(site: Path) -> dict:
    """Fallback used only when ROOT is not a git checkout (bare build machine)."""
    src = ROOT / STATIC_REL
    if not src.exists():
        raise RuntimeError(f"{src} does not exist and git is unavailable")
    removed_legacy = _clear_legacy_static_dir(site)
    digests: dict[str, dict] = {}
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        data = path.read_bytes()
        target = site / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[str(path.relative_to(ROOT))] = {"sha256": hashlib.sha256(data).hexdigest(),
                                                "bytes": len(data), "site_path": str(rel)}
    return {"ref": "worktree", "legacy_static_dir_removed": removed_legacy, "files": digests}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-source", choices=["head", "worktree"], default="head",
                        help="where static/ is taken from (default: head)")
    parser.add_argument("--site", default=str(SRC),
                        help="site tree to pack (default: <repo>/public-final; the real tree "
                             "usually lives in the poetry-static data directory)")
    parser.add_argument("--out", default=str(OUT),
                        help="archive to write (default: <repo>/public-final.tar.gz); the "
                             "top-level member is always public-final/")
    args = parser.parse_args()

    site_root = Path(args.site).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    started = time.perf_counter()
    if not site_root.exists():
        raise SystemExit(f"missing {site_root}; build the site tree first")

    if args.static_source == "head":
        try:
            static_info = sync_static_from_head(site_root)
            static_info["commit"] = head_commit()
        except RuntimeError as error:
            print(f"warning: git unavailable ({error}); falling back to the working tree", flush=True)
            static_info = sync_static_from_worktree(site_root)
    else:
        static_info = sync_static_from_worktree(site_root)
    for rel, info in static_info["files"].items():
        print(f"static {info['site_path']} <- {rel} {info['sha256']} {info['bytes']} B ({static_info['ref']})",
              flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz", compresslevel=1) as tar:
        tar.add(site_root, arcname="public-final")
    elapsed = time.perf_counter() - started
    raw = sum(p.stat().st_size for p in site_root.rglob("*") if p.is_file())
    files = sum(1 for p in site_root.rglob("*") if p.is_file())
    report = {
        "tar_gz_bytes": out_path.stat().st_size,
        "tar_gz_sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "tar_gz_path": str(out_path),
        "site_root": str(site_root),
        "raw_bytes": raw,
        "files": files,
        "seconds": round(elapsed, 2),
        "ratio": round(out_path.stat().st_size / raw, 4),
        "static_source": static_info,
    }
    report_path = out_path.parent / "archive_final_valid_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("tar_gz_bytes", "tar_gz_sha256", "raw_bytes", "files", "seconds")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
