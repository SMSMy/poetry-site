#!/usr/bin/env python3
"""normalize_catalog v2: complete, idempotent twins for poet pages.

Fixes over v1:
 - strips existing twins first (idempotent),
 - matches <a> tags with ANY attribute order/set around href,
 - normalizes the anchor's TEXT ONLY (inner tags stripped + html-unescaped).
Applies to the target site's poet/*.html (default public-final)."""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
TRANSLATION = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ـ": ""})
STRIP_TWIN = re.compile(r"<span class=search-normalized[^>]*>.*?</span>", re.S)
ANCHOR = re.compile(r"<a\b[^>]*href=/(?:[^/]+/)?poem/\d+\.html[^>]*>(.*?)</a>", re.S)
TAGS = re.compile(r"<[^>]*>")


def normalize(text: str) -> str:
    return DIACRITICS.sub("", text).translate(TRANSLATION)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("public-final"))
    args = parser.parse_args()
    started = time.perf_counter()
    site: Path = args.site
    files = list((site / "poet").glob("*.html"))
    added = 0
    stripped = 0
    anchor_total = 0
    miss_examples: list[str] = []
    for page in files:
        text = page.read_text(encoding="utf-8")
        text, s = STRIP_TWIN.subn("", text)
        stripped += s

        def link_with_normalized(match: re.Match[str]) -> str:
            nonlocal added, anchor_total
            anchor_total += 1
            visible = TAGS.sub("", html.unescape(match.group(1)))
            if not visible:
                return match.group(0)
            if visible != normalize(visible):
                added += 1
                normalized = html.escape(normalize(visible), quote=False)
                return match.group(0) + f'<span class=search-normalized>{normalized}</span>'
            return match.group(0)

        text = ANCHOR.sub(link_with_normalized, text)

        def h1_with_normalized(match: re.Match[str]) -> str:
            nonlocal added
            visible = TAGS.sub("", html.unescape(match.group(2)))
            if not visible or visible == normalize(visible):
                return match.group(0)
            added += 1
            normalized = html.escape(normalize(visible), quote=False)
            return match.group(1) + match.group(2) + \
                f'<span class=search-normalized>{normalized}</span>' + match.group(3)

        text = re.sub(r"(<h1\b[^>]*>)(.*?)(</h1>)", h1_with_normalized, text, count=1, flags=re.S)
        page.write_text(text, encoding="utf-8", newline="")
    report = {
        "site": str(site), "poet_pages": len(files),
        "twins_stripped_from_source": stripped,
        "anchors_examined": anchor_total,
        "twins_added": added,
        "seconds": round(time.perf_counter() - started, 2),
    }
    (Path(args.site).parent / "normalize_catalog_v2_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
