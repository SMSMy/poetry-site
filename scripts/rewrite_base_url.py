#!/usr/bin/env python3
"""Rewrite root-absolute URLs inside a built site tree for the project Pages subpath.

Needed only when reusing a previously generated tree whose links were emitted
for another baseURL. Fresh builds should instead keep hugo.toml:
    baseURL = "https://smsmy.github.io/poetry-site/"

Usage:
  python scripts/rewrite_base_url.py --site public-trial --from https://poetry-demo.invalid/ \
      --to https://smsmy.github.io/poetry-site/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ATTRIBUTE_ATTRS = ("href", "src", "poster", "action", "formaction")
# attribute = /path | attribute="/path" — never touches //host or http URLs
URL_ATTR = re.compile(
    r"\b(?P<attr>" + "|".join(ATTRIBUTE_ATTRS) + r")\s*=\s*(\"?)/(?!/)([^\"\\s>]*)",
    re.IGNORECASE,
)
LOC = re.compile(r"(<loc>)([^<]+)(</loc>)")


def rewrite_relative(text: str, prefix: str) -> str:
    def attr_replace(match: re.Match) -> str:
        # named group shifts numeric indices: (attr)->1, (quote)->2, (path)->3
        return (f"{match.group('attr')}={match.group(2) or ''}{prefix}"
                f"/{match.group(3)}")

    return URL_ATTR.sub(attr_replace, text)


def rewrite_loc(match: re.Match, from_host: str, to_base: str) -> str:
    url = match.group(2)
    p = re.match(rf"{re.escape(from_host)}(/.*)?$", url)
    if p is None:
        return match.group(0)  # unknown host — leave the entry alone
    path = (p.group(1) or "/").rstrip("/")
    tail = "/" if url.endswith("/") and path != "/" else ""
    return f"{match.group(1)}{to_base}{path}{tail}{match.group(3)}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--prefix", default="/poetry-site",
                        help="prefix inserted into root-absolute attribute URLs")
    parser.add_argument("--from", dest="from_host", default="https://poetry-demo.invalid/")
    parser.add_argument("--to", dest="to_base", default="https://smsmy.github.io/poetry-site",
                        help="replacement base for sitemap <loc> entries")
    args = parser.parse_args()
    started = time.perf_counter()

    pages = 0
    sitemaps = 0
    for page in args.site.rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        if f'="/{args.prefix.strip("/")}/' in text:
            continue  # already carries the prefix
        new = rewrite_relative(text, args.prefix)
        if new != text:
            page.write_text(new, encoding="utf-8", newline="")
        pages += 1
    for sm_file in list(args.site.glob("sitemap*.xml")):
        text = sm_file.read_text(encoding="utf-8")
        if "/" + args.prefix.strip("/") + "/" in text:
            continue
        new = rewrite_sitemap_from = LOC.sub(
            lambda m: rewrite_loc(m, args.from_host, args.to_base), text)
        if new != text:
            sm_file.write_text(new, encoding="utf-8", newline="")
        sitemaps += 1
    print(json.dumps({"site": str(args.site), "html_files_seen": pages,
                      "sitemap_files": sitemaps,
                      "seconds": round(time.perf_counter() - started, 1)}))


if __name__ == "__main__":
    main()
