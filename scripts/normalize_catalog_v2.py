#!/usr/bin/env python3
"""normalize_catalog v3: complete, idempotent twins for poet pages.

What the twins are: for every poem link (and the poet <h1>) the diacritized Arabic text
is copied once more in fully normalized form inside a hidden
`<span class=search-normalized>` element. Pagefind only sees normalized characters, so
the UI can normalize the query with the very same function and match everything.

Fixes over v2:
 - normalization is imported from `scripts/normalize_ar.py` (one function for the index
   and the UI); v2 missed ؤ -> و and ئ -> ي,
 - every twin is wrapped in spaces. Without the separator Pagefind merges the twin's
   first (and last) word with the neighbouring text, which silently removed 125 of the
   426 ground-truth pages of "أرى" from the index;
 - `--verify` re-reads the written pages and reports twins whose words are still glued,
 - the biography gets a twin as well: the four remaining ground-truth pages of "فتى"/
   "الفتى" occur only in prose (a bio quoting a verse), so titles alone cannot reach them.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_ar import normalize_search  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

STRIP_TWIN = re.compile(r"\s*<span class=search-normalized[^>]*>.*?</span>", re.S)
ANCHOR = re.compile(r"<a\b[^>]*href=/(?:[^/]+/)?poem/\d+\.html[^>]*>(.*?)</a>", re.S)
TAGS = re.compile(r"<[^>]*>")
H1 = re.compile(r"(<h1\b[^>]*>)(.*?)(</h1>)", re.S)
BIO_SECTION = re.compile(r"(<section class=poet-biography>)(.*?)(</section>)", re.S)
PRESERVE_LINES = re.compile(r"<p class=preserve-lines[^>]*>(.*?)</p>", re.S)


def twin(text: str) -> str:
    """The hidden normalized copy, delimited by spaces so Pagefind tokenizes it alone."""
    return f' <span class=search-normalized> {text} </span>'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=Path("public-final"))
    parser.add_argument("--verify", action="store_true",
                        help="re-read the pages and report twins still glued to their neighbours")
    args = parser.parse_args()
    started = time.perf_counter()
    site: Path = args.site
    files = sorted((site / "poet").glob("*.html"))
    added = 0
    stripped = 0
    anchor_total = 0
    name_twins = 0
    bio_twins = 0
    for page in files:
        text = page.read_text(encoding="utf-8")
        text, s = STRIP_TWIN.subn("", text)
        stripped += s

        def link_with_normalized(match: re.Match[str]) -> str:
            nonlocal added, anchor_total
            anchor_total += 1
            visible = TAGS.sub("", html.unescape(match.group(1))).strip()
            if not visible:
                return match.group(0)
            normalized = normalize_search(visible)
            if visible == normalized:
                return match.group(0)
            added += 1
            return match.group(0) + twin(html.escape(normalized, quote=False))

        text = ANCHOR.sub(link_with_normalized, text)

        def h1_with_normalized(match: re.Match[str]) -> str:
            nonlocal added, name_twins
            visible = TAGS.sub("", html.unescape(match.group(2))).strip()
            if not visible:
                return match.group(0)
            normalized = normalize_search(visible)
            if visible == normalized:
                return match.group(0)
            added += 1
            name_twins += 1
            return match.group(1) + match.group(2) + twin(html.escape(normalized, quote=False)) + match.group(3)

        text = H1.sub(h1_with_normalized, text, count=1)

        def bio_with_normalized(match: re.Match[str]) -> str:
            # The biography is prose (and often quotes a whole verse): four pages of the
            # "فتى"/"الفتى" ground truth live only here, so it needs a twin too.
            nonlocal added, bio_twins
            blocks = PRESERVE_LINES.findall(match.group(2)) or [TAGS.sub(" ", match.group(2))]
            visible = " ".join(TAGS.sub(" ", html.unescape(block)) for block in blocks).strip()
            if not visible:
                return match.group(0)
            normalized = normalize_search(visible)
            if not normalized or normalized == visible:
                return match.group(0)
            added += 1
            bio_twins += 1
            return match.group(1) + match.group(2) + twin(html.escape(normalized, quote=False)) + match.group(3)

        text = BIO_SECTION.sub(bio_with_normalized, text, count=1)
        page.write_text(text, encoding="utf-8", newline="")

    report = {
        "site": str(site), "poet_pages": len(files),
        "twins_stripped_from_source": stripped,
        "anchors_examined": anchor_total,
        "twins_added": added,
        "twin_breakdown": {"poem_titles": added - name_twins - bio_twins,
                           "poet_names": name_twins, "biographies": bio_twins},
        "separator": "space before and after every twin",
        "seconds": round(time.perf_counter() - started, 2),
    }
    if args.verify:
        glued = []
        twin_re = re.compile(r"<span class=search-normalized[^>]*>\s*(.*?)\s*</span>", re.S)
        for page in files:
            text = page.read_text(encoding="utf-8")
            for match in twin_re.finditer(text):
                before = text[max(0, match.start() - 1):match.start()]
                after = text[match.end():match.end() + 1]
                if before not in (" ", ">", "") and not before.isspace():
                    glued.append({"page": page.name, "side": "before", "char": before, "twin": match.group(1)[:60]})
                if after not in (" ", "<", "") and not after.isspace():
                    glued.append({"page": page.name, "side": "after", "char": after, "twin": match.group(1)[:60]})
        report["glued_twins"] = len(glued)
        report["glued_sample"] = glued[:10]
    (site.parent / "normalize_catalog_v3_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
