#!/usr/bin/env python3
"""Emit static title chunks for D1 search results (sidecar files, zero D1 reads).

Chunks map id-range -> <title> of public-final/poem pages. Default target is
public-final/search-title/ — for pure measurement send --out-dir to a temp path.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from normalize_catalog_v2 import normalize  # same normalization as the site  # noqa: E402

DIACRITICS = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=ROOT / "public-final")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "public-final" / "search-title")
    parser.add_argument("--chunk", type=int, default=2000)
    parser.add_argument("--mode", choices=["raw", "norm"], default="raw")
    args = parser.parse_args()

    started = time.perf_counter()
    entries = []  # (id, title)
    for page in sorted((args.site / "poem").glob("*.html"), key=lambda p: int(p.stem)):
        pid = int(page.stem)
        text = page.read_text(encoding="utf-8")
        start = text.find("<title>")
        end = text.find("</title>", start)
        title = text[start + 7:end] if start != -1 and end != -1 else ""
        if args.mode == "norm":
            title = normalize(title)
        entries.append((pid, title))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"chunk": args.chunk, "mode": args.mode, "count": len(entries),
                "min_id": entries[0][0], "max_id": entries[-1][0]}
    files = []
    for i in range(0, len(entries), args.chunk):
        chunk = entries[i:i + args.chunk]
        first = chunk[0][0]
        out = args.out_dir / f"{first // args.chunk}.json"
        payload = {"start": first, "titles": [t for _, t in chunk]}
        out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        files.append({"file": out.name, "bytes": out.stat().st_size, "ids": first})
    manifest["files"] = files
    manifest["total_bytes"] = sum(f["bytes"] for f in files)
    manifest["avg_bytes_per_file"] = round(manifest["total_bytes"] / len(files), 1)
    manifest["seconds"] = round(time.perf_counter() - started, 1)
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False)[:600])
    print(f"TOTAL {manifest['total_bytes']} bytes in {len(files)} files "
          f"({manifest['seconds']}s, mode={args.mode})")


if __name__ == "__main__":
    main()
