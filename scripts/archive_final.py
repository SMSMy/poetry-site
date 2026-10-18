#!/usr/bin/env python3
"""Create the final site archive (tar.gz, compresslevel=1) from public-final and report sizes."""
from __future__ import annotations

import json
import sys
import tarfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "public-final"
OUT = ROOT / "public-final.tar.gz"


def main() -> None:
    started = time.perf_counter()
    with tarfile.open(OUT, "w:gz", compresslevel=1) as tar:
        tar.add(SRC, arcname="public-final")
    elapsed = time.perf_counter() - started
    raw = sum(p.stat().st_size for p in SRC.rglob("*") if p.is_file())
    files = sum(1 for p in SRC.rglob("*") if p.is_file())
    report = {
        "tar_gz_bytes": OUT.stat().st_size,
        "raw_bytes": raw,
        "files": files,
        "seconds": round(elapsed, 2),
        "ratio": round(OUT.stat().st_size / raw, 4),
    }
    (ROOT / "archive_final_valid_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
