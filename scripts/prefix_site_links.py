#!/usr/bin/env python3
"""Rewrite internal links to the deployment prefix inside the built site tree.

The site is served from https://smsmy.github.io/poetry-site/, so every root-absolute
URL in the built pages (`href=/css/style.css`, `src=/js/search.js`, `href=/`,
`href=/poet/1.html`) must carry the `/poetry-site` prefix. The built pages are minified,
so attributes appear both quoted and unquoted — hence a byte-level regex over the file.

Idempotent: URLs already under the prefix and protocol-relative `//host` URLs are left
alone. `pagefind-catalog/` and `search-title/` are never touched (index internals and
title chunks, not documents).

Usage:
    python3 scripts/prefix_site_links.py --site /path/to/public-final --dry-run --sample 60
    python3 scripts/prefix_site_links.py --site /path/to/public-final
    python3 scripts/prefix_site_links.py --site /path/to/public-final \
        --sitemap-base https://smsmy.github.io/poetry-site/
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
PREFIX = "/poetry-site"
SKIP_DIRS = {"pagefind-catalog", "search-title"}
PLACEHOLDER_BASE = "https://poetry-demo.invalid/"

ATTR = re.compile(rb'(href|src)=(["\']?)(/[^"\'\s>]*)\2')


def fix_url(url: bytes) -> bytes:
    if url.startswith(b"//") or url.startswith(PREFIX.encode() + b"/"):
        return url
    if url == b"/":
        return PREFIX.encode() + b"/"
    return PREFIX.encode() + url


def iter_pages(site: Path):
    for path in sorted(site.rglob("*.html")):
        if path.parts and any(part in SKIP_DIRS for part in path.relative_to(site).parts[:-1]):
            continue
        yield path


def rewrite_page(path: Path, dry_run: bool) -> tuple[int, list[str]]:
    data = path.read_bytes()
    changes: list[tuple[bytes, bytes]] = []

    def repl(match: re.Match[bytes]) -> bytes:
        attr, quote, url = match.group(1), match.group(2), match.group(3)
        new = fix_url(url)
        if new == url:
            return match.group(0)
        changes.append((url, new))
        return attr + b"=" + quote + new + quote

    updated = ATTR.sub(repl, data)
    if changes and not dry_run:
        path.write_bytes(updated)
    return len(changes), [f"{a.decode()} -> {b.decode()}" for a, b in changes[:3]]


def fix_sitemaps(site: Path, base: str, dry_run: bool) -> dict:
    report = {}
    for path in sorted(site.glob("sitemap*.xml")):
        data = path.read_text(encoding="utf-8")
        count = data.count(PLACEHOLDER_BASE)
        if count and not dry_run:
            path.write_text(data.replace(PLACEHOLDER_BASE, base + "/"), encoding="utf-8")
        report[path.name] = count
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default=str(ROOT / "public-final"),
                        help="built site tree (default: <repo>/public-final)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--sample", type=int, default=0,
                        help="process a random sample of N pages instead of all of them")
    parser.add_argument("--sitemap-base", default="",
                        help="replace the placeholder sitemap base with this base URL")
    parser.add_argument("--report", default="", help="write a JSON report to this path")
    args = parser.parse_args()

    site = Path(args.site).expanduser().resolve()
    if not site.is_dir():
        raise SystemExit(f"missing site tree: {site}")

    pages = list(iter_pages(site))
    if args.sample:
        random.seed(20261019)
        pages = random.sample(pages, min(args.sample, len(pages)))

    started = time.perf_counter()
    files_changed = 0
    total = 0
    per_dir: collections.Counter[str] = collections.Counter()
    samples: dict[str, list[str]] = {}
    for page in pages:
        count, examples = rewrite_page(page, args.dry_run)
        if count:
            files_changed += 1
            total += count
            top = page.relative_to(site).parts[0]
            per_dir[top] += count
            samples.setdefault(top, examples)

    report = {
        "site": str(site),
        "dry_run": args.dry_run,
        "pages_scanned": len(pages),
        "files_changed": files_changed,
        "replacements": total,
        "per_top_dir": dict(per_dir.most_common()),
        "samples": samples,
        "prefix": PREFIX,
        "seconds": round(time.perf_counter() - started, 2),
    }
    if args.sitemap_base:
        report["sitemaps"] = fix_sitemaps(site, args.sitemap_base.rstrip("/"), args.dry_run)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
