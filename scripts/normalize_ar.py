#!/usr/bin/env python3
"""Canonical Arabic search normalization — one function for the index and the UI.

This module is the single source of truth. Everything that has to agree on what a
"normalized" Arabic token looks like must call `normalize_search()` (Python side) or
the JS mirror shipped in `static/js/search.js` (browser side):

  * scripts/normalize_catalog_v2.py  — writes the `search-normalized` twins that the
    Pagefind index actually indexes (the index side).
  * build_static.py / D1 import helpers — normalized corpus text for D1 (same rules).
  * static/js/search.js              — normalizes the query typed in the search box.

`JS_SOURCE` below is the exact JS block kept in `static/js/search.js` between the
`// <<normalize-ar>>` markers; `tests/test_normalize_parity.py` fails if the two drift
apart, and re-runs both implementations over real corpus tokens (`--data` root) to
catch behavioural differences that a text comparison could miss.

Rules, in order:
  1. drop combining marks and tatweel:
     U+0610..U+061A, U+064B..U+065F, U+0670, U+06D6..U+06ED, U+0640
  2. unify letter shapes:  أ إ آ ٱ -> ا,  ى -> ي,  ة -> ه,  ؤ -> و,  ئ -> ي
  3. collapse every whitespace run to a single space, then trim

Every step exists because a real query in the QA battery needs it:
  * U+0640 tatweel and the 0653..065F marks used to leak into twins but not into the
    query (or the reverse) and produced silent misses;
  * ؤ -> و / ئ -> ي were missing from the twin generator, so "رؤية" was indexed as
    "رؤيه" while the UI searched for "رويه".
"""
from __future__ import annotations

import re

MARKS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
TRANSLATION = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
})
MULTI_SPACE = re.compile(r"\s+")

JS_SOURCE = r'''const ARABIC_MARKS = /[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]/gu;
function normalizeArabicQuery(value) {
  return String(value ?? "")
    .replace(ARABIC_MARKS, "")
    .replace(/[أإآٱ]/gu, "ا")
    .replace(/ى/gu, "ي")
    .replace(/ة/gu, "ه")
    .replace(/ؤ/gu, "و")
    .replace(/ئ/gu, "ي")
    .replace(/\s+/gu, " ")
    .trim();
}'''


def normalize_search(text: str) -> str:
    """Normalize one query or corpus string. See module docstring for the rules."""
    value = MARKS.sub("", text or "")
    value = value.translate(TRANSLATION)
    return MULTI_SPACE.sub(" ", value).strip()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Normalize strings with the canonical rules.")
    parser.add_argument("text", nargs="+")
    args = parser.parse_args()
    for text in args.text:
        print(normalize_search(text))


if __name__ == "__main__":
    main()
