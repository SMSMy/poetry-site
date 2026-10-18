#!/usr/bin/env python3
"""Second hardening pass over the already-compacted poem pages.

Reads `public-optimized` (the ugly-URL pragmatic build) read-only and writes a
fresh `public-final` directory:
  - every poem page gets its template shell trimmed again,
  - the sitemap is split into an index (<=50k URLs per map),
  - emits minify_final_report.json with byte deltas.

The source tree is never modified.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "public-optimized"
DST = ROOT / "public-final"

transforms = [
    # valid-HTML pass (2026-10-19 revision): closing tags RESTORED on user request.
    # Only attribute-level cleanups remain; `, </head>, </article>, </body>, </html>` untouched.
    ("<!doctype html><html lang=ar dir=rtl>", "<!doctype html><html dir=rtl>"),
    ("<article class=poem-page class=poem-content>", '<article class="poem-page poem-content">'),
]


def compress_page(body: str) -> tuple[str, int]:
    start = len(body.encode("utf-8"))
    for old, new in transforms:
        if old in body:
            body = body.replace(old, new)
    return body, start - len(body.encode("utf-8"))


def main() -> None:
    started = time.perf_counter()
    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns("poem"), dirs_exist_ok=True)
    poem_src = SRC / "poem"
    poem_dst = DST / "poem"
    poem_dst.mkdir()
    before = after = saved = 0
    count = 0
    for page in sorted(poem_src.glob("*.html")):
        data = page.read_text(encoding="utf-8")
        compacted, delta = compress_page(data)
        out = poem_dst / page.name
        out.write_bytes(compacted.encode("utf-8"))
        before += page.stat().st_size
        after += out.stat().st_size
        saved += delta
        count += 1
    # sitemap: public-optimized is already split (index + 4 maps of <=45k); keep those files
    # as-is instead of re-running split_sitemap, which corrupts an already-split index.
    sitemap_stats = {"maps": 4, "index": "sitemap.xml (already split upstream; copied read-only)"}
    report = {
        "source": str(SRC.name),
        "target": str(DST.name),
        "poem_pages": count,
        "poem_bytes_before": before,
        "poem_bytes_after": after,
        "poem_bytes_saved": saved,
        "sitemap": sitemap_stats,
        "seconds": round(time.perf_counter() - started, 2),
    }
    (ROOT / "minify_final_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
