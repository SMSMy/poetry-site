#!/usr/bin/env python3
"""Compact generated poem pages and split Hugo's sitemap for static hosting."""
from __future__ import annotations

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
ET.register_namespace("", NS)


def compact_page(text: str, base_path: str = "") -> str:
    # These elements duplicate global navigation or metadata that is not used by
    # the title-only Pagefind catalog. Keep the poet link and all poem data.
    replacements = [
        (r"<div class=pagefind-filters.*?</div>", ""),
        (r"<p class=breadcrumb>.*?</p>", ""),
        (r"<header class=site-header>.*?</header>", '<header><a href=/>ديوان الشعر العربي</a></header>'),
        (r"<footer class=site-footer>.*?</footer>", ""),
        (r'<meta name=description content="[^"]*">', ""),
        (r'<main class="page-container demo-main">', ""),
        (r"</main>", ""),
        (r" itemscope(?:=\S+)?", ""),
        (r" itemtype=\S+", ""),
        (r" itemprop=(?:\"[^\"]*\"|[^ >]+)", ""),
        (r" data-pagefind-body", ""),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.S)

    text = text.replace("<span class=s>", "<span>").replace("<span class=a>", "<span>")

    # Page titles already contain the poem title; repeating the site brand on
    # every one of 134k pages costs several MiB without changing the UI.
    text = text.replace(" — ديوان الشعر العربي</title>", "</title>")

    def paired(match: re.Match[str]) -> str:
        left, right = match.group(1), match.group(2)
        return f'<p class=c><span>{left}</span><span>{right}</span></p>'

    text = re.sub(
        r"<div class=poem-card><div class=sadr-part>(.*?)</div><div class=ajz-part>(.*?)</div></div>",
        paired,
        text,
        flags=re.S,
    )
    # Preserve an occasional single hemistich while using the same compact tag.
    text = re.sub(
        r"<div class=poem-card><div class=sadr-part>(.*?)</div></div>",
        r"<p class=c><span>\1</span></p>",
        text,
        flags=re.S,
    )
    if base_path:
        prefix = "/" + base_path.strip("/") + "/"
        text = re.sub(r'\b(href|src)=/(?!' + re.escape(base_path.strip("/") + "/") + r')', lambda m: m.group(1) + "=" + prefix, text)
    return text


def split_sitemap(root: Path, base_url: str, max_urls: int) -> dict[str, int]:
    sitemap = root / "sitemap.xml"
    if not sitemap.exists():
        return {"maps": 0, "urls": 0, "max_urls_per_map": 0}
    tree = ET.parse(sitemap)
    urls = list(tree.getroot())
    chunks = [urls[i : i + max_urls] for i in range(0, len(urls), max_urls)]
    index = ET.Element(f"{{{NS}}}sitemapindex")
    for number, chunk in enumerate(chunks, start=1):
        child_name = f"sitemap-{number}.xml"
        child_root = ET.Element(f"{{{NS}}}urlset")
        child_root.extend(chunk)
        ET.ElementTree(child_root).write(root / child_name, encoding="utf-8", xml_declaration=True)
        item = ET.SubElement(index, f"{{{NS}}}sitemap")
        ET.SubElement(item, f"{{{NS}}}loc").text = base_url.rstrip("/") + "/" + child_name
    ET.ElementTree(index).write(sitemap, encoding="utf-8", xml_declaration=True)
    return {"maps": len(chunks), "urls": len(urls), "max_urls_per_map": max(map(len, chunks), default=0)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("public"))
    parser.add_argument("--base-url", default="https://poetry-demo.invalid/")
    parser.add_argument("--sitemap-limit", type=int, default=45000)
    parser.add_argument("--base-path", default="")
    args = parser.parse_args()
    if not 1 <= args.sitemap_limit <= 50000:
        raise SystemExit("--sitemap-limit must be between 1 and 50000")
    poem_dir = args.site / "poem"
    pages = list(poem_dir.glob("*.html"))
    before = sum(page.stat().st_size for page in pages)
    started = time.perf_counter()
    after = 0
    for page in pages:
        data = page.read_text(encoding="utf-8")
        compacted = compact_page(data, args.base_path)
        encoded = compacted.encode("utf-8")
        page.write_bytes(encoded)
        after += len(encoded)
    sitemap_stats = split_sitemap(args.site, args.base_url, args.sitemap_limit)
    report = {
        "poem_pages": len(pages),
        "poem_bytes_before": before,
        "poem_bytes_after": after,
        "poem_bytes_saved": before - after,
        "sitemap": sitemap_stats,
        "seconds": round(time.perf_counter() - started, 3),
    }
    (args.site.parent / "compact_static_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
