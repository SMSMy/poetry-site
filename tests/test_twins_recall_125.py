#!/usr/bin/env python3
"""Regression test for the 125 recall-miss pages (see fixtures/recall_miss_125.json).

Root cause, verified with pagefind 1.5.2 on 2026-10-19: the twin span was appended with
no separator, so Pagefind merged the twin's first (and, at the end of a title, last) word
with the neighbouring text. Searching "اري" could not find the pages whose only match
lived in such a twin, and the stemless (en) index lost 125 of the 426 ground-truth pages
of "أرى" (recall 70.7%). Fix: wrap every twin in spaces (scripts/normalize_catalog_v2.py)
and normalize with the shared function (scripts/normalize_ar.py).

This test needs the built tree (<repo>/../poetry-static, override with
POETRY_STATIC_ROOT or by running from any cwd with that variable set): it reads the
regenerated poet pages and asserts both properties for the 125 pages.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from normalize_ar import normalize_search  # noqa: E402

DATA = Path(os.environ.get("POETRY_STATIC_ROOT", REPO.parent / "poetry-static"))
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "recall_miss_125.json").read_text(encoding="utf-8"))
POET_DIR = DATA / "public-final" / "poet"

TWIN = re.compile(r"<span class=search-normalized[^>]*>(.*?)</span>", re.S)
GLUED = re.compile(r"<span class=search-normalized[^>]*>")
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class Recall125Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not POET_DIR.exists():
            raise unittest.SkipTest(f"no built tree at {POET_DIR}")

    def test_every_page_is_still_present(self) -> None:
        missing = [pid for pid in FIXTURE["page_ids"] if not (POET_DIR / f"{pid}.html").exists()]
        self.assertEqual(missing, [], f"{len(missing)} page(s) not found on disk")

    def test_no_twin_is_glued_to_its_neighbours(self) -> None:
        glued: list[tuple[int, str, str]] = []
        for pid in FIXTURE["page_ids"]:
            text = (POET_DIR / f"{pid}.html").read_text(encoding="utf-8")
            for match in GLUED.finditer(text):
                before = text[match.start() - 1:match.start()]
                after = text[match.end():match.end() + 1]
                if before and not before.isspace() and before not in ">":
                    glued.append((pid, "before", before))
                if after and not after.isspace() and after not in "<":
                    glued.append((pid, "after", after))
        self.assertEqual(glued[:10], [], f"{len(glued)} glued twin(s) remain")

    def test_query_token_is_searchable_inside_a_twin(self) -> None:
        query_token = FIXTURE["query_normalized"]
        unsearchable: list[int] = []
        for pid in FIXTURE["page_ids"]:
            text = (POET_DIR / f"{pid}.html").read_text(encoding="utf-8")
            found = False
            for match in TWIN.finditer(text):
                # The twin is space-delimited, so tokenizing its inner text is what
                # Pagefind will index.
                if query_token in TOKEN.findall(match.group(1)):
                    found = True
                    break
            if not found:
                unsearchable.append(pid)
        self.assertEqual(unsearchable[:10], [], f"{len(unsearchable)} page(s) still expose no searchable {query_token!r} token")

    def test_the_token_these_pages_were_missed_on_is_the_normalized_query(self) -> None:
        self.assertEqual(normalize_search(FIXTURE["query"]), FIXTURE["query_normalized"])


if __name__ == "__main__":
    unittest.main()
