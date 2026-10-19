#!/usr/bin/env python3
"""Unit tests for the canonical Arabic search normalization (no site data needed).

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from normalize_ar import JS_SOURCE, normalize_search  # noqa: E402


class NormalizeSearchTests(unittest.TestCase):
    def test_strips_diacritics(self) -> None:
        self.assertEqual(normalize_search("لَمّا"), "لما")
        self.assertEqual(normalize_search("أَرى"), "اري")

    def test_unifies_alef_forms(self) -> None:
        for form in ("أبان", "إبان", "آبان", "ٱبان"):
            self.assertEqual(normalize_search(form), "ابان")

    def test_unifies_maqsura_and_taa(self) -> None:
        self.assertEqual(normalize_search("الفتى"), "الفتي")
        self.assertEqual(normalize_search("رحمة"), "رحمه")

    def test_unifies_hamza_on_waw_and_yaa(self) -> None:
        # The v2 twin generator missed these two rules and broke "رؤية"/"قائد".
        self.assertEqual(normalize_search("رؤية"), "رويه")
        self.assertEqual(normalize_search("قائد"), "قايد")

    def test_strips_tatweel_and_high_marks(self) -> None:
        self.assertEqual(normalize_search("مـــزيد"), "مزيد")
        self.assertEqual(normalize_search("قُرْآن"), "قران")

    def test_collapses_whitespace_and_trims(self) -> None:
        self.assertEqual(normalize_search("  لما \n رايت  البز "), "لما رايت البز")
        self.assertEqual(normalize_search(""), "")

    def test_is_idempotent(self) -> None:
        for text in ("لَمّا", "أَرى", "رؤية", "الفتى", "مـــزيد", "  حب "):
            once = normalize_search(text)
            self.assertEqual(normalize_search(once), once)

    def test_does_not_touch_ascii_or_digits(self) -> None:
        self.assertEqual(normalize_search("poem 123"), "poem 123")

    def test_keeps_the_letters_that_only_look_similar(self) -> None:
        # و/ي must not be turned into anything else, and ه must stay ه.
        self.assertEqual(normalize_search("وحي"), "وحي")
        self.assertEqual(normalize_search("عهد"), "عهد")


class JSSourceTests(unittest.TestCase):
    def test_js_block_is_wrapped_in_markers(self) -> None:
        source = (REPO / "static" / "js" / "search.js").read_text(encoding="utf-8")
        self.assertIn("// <<normalize-ar>>", source)
        self.assertIn("// <</normalize-ar>>", source)

    def test_js_block_matches_canonical_source(self) -> None:
        source = (REPO / "static" / "js" / "search.js").read_text(encoding="utf-8")
        block = source.split("// <<normalize-ar>>", 1)[1].split("// <</normalize-ar>>", 1)[0]
        # The marker line carries a trailing comment; compare the code only.
        block = "\n".join(line for line in block.splitlines() if not line.startswith("// <<"))
        self.assertEqual(block.strip(), JS_SOURCE.strip())


if __name__ == "__main__":
    unittest.main()
