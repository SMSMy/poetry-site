#!/usr/bin/env python3
"""Emit static title chunks for D1 search results (sidecar files, zero D1 reads).

Chunks map fixed id-ranges -> <title> of public-final/poem pages:
chunk index = (id - 1) // chunk, explicit "start" = index * chunk + 1, and the
`titles` array always has exactly `chunk` slots so that `id - start` is the
literal offset. Ids that do not exist in the tree (e.g. the corpus gap
32334-32338) are stored as `null`. Default target is public-final/search-title/ —
for pure measurement send --out-dir to a temp path.
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
from normalize_ar import normalize_search as normalize  # canonical site normalization  # noqa: E402

DIACRITICS = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=ROOT / "public-final")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "public-final" / "search-title")
    parser.add_argument("--chunk", type=int, default=2000)
    parser.add_argument("--mode", choices=["raw", "norm"], default="raw")
    args = parser.parse_args()

    started = time.perf_counter()
    entries: dict[int, str] = {}  # id -> title
    for page in sorted((args.site / "poem").glob("*.html"), key=lambda p: int(p.stem)):
        pid = int(page.stem)
        text = page.read_text(encoding="utf-8")
        start = text.find("<title>")
        end = text.find("</title>", start)
        title = text[start + 7:end] if start != -1 and end != -1 else ""
        if args.mode == "norm":
            title = normalize(title)
        entries[pid] = title

    if not entries:
        raise SystemExit(f"no poem pages under {args.site / 'poem'}")

    min_id = min(entries)
    max_id = max(entries)
    missing = [pid for pid in range(min_id, max_id + 1) if pid not in entries]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    nulls = 0
    for chunk_index in range((max_id - 1) // args.chunk + 1):
        start = chunk_index * args.chunk + 1
        titles = [entries.get(start + offset) for offset in range(args.chunk)]
        nulls += sum(1 for title in titles if title is None)
        out = args.out_dir / f"{chunk_index}.json"
        payload = {"start": start, "titles": titles}
        out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        files.append({"file": out.name, "bytes": out.stat().st_size, "ids": start})

    manifest = {"chunk": args.chunk, "mode": args.mode, "count": len(entries),
                "min_id": min_id, "max_id": max_id,
                "missing": missing, "nulls": nulls, "files": files,
                "total_bytes": sum(f["bytes"] for f in files)}
    manifest["avg_bytes_per_file"] = round(manifest["total_bytes"] / len(files), 1)
    manifest["seconds"] = round(time.perf_counter() - started, 1)
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False)[:600])
    print(f"TOTAL {manifest['total_bytes']} bytes in {len(files)} files "
          f"({manifest['seconds']}s, mode={args.mode}, nulls={nulls})")


if __name__ == "__main__":
    main()
