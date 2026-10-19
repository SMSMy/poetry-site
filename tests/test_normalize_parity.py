#!/usr/bin/env python3
"""Parity tests: the Python index side and the JS UI side must normalize identically.

Text comparison (tests/test_normalize_unit.py) catches drift inside `search.js`, but the
real risk is behavioural: two implementations that look different but must agree on
every token that actually occurs in the corpus. This test therefore:

  1. pulls every token of the 125 recall-miss pages (see
     tests/fixtures/recall_miss_125.json) plus the poet names and a sample of poem
     titles straight out of the built pages,
  2. normalizes them with `scripts/normalize_ar.py`,
  3. normalizes the same tokens with the JavaScript block that is actually shipped in
     `static/js/search.js` (executed by node),
  4. fails on the first difference.

Requires `node` on PATH and the built tree (default <repo>/../poetry-static, override
with POETRY_STATIC_ROOT or --data). Both are skipped with a clear message when missing.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from normalize_ar import normalize_search  # noqa: E402

DATA = Path(os.environ.get("POETRY_STATIC_ROOT", REPO.parent / "poetry-static"))
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "recall_miss_125.json").read_text(encoding="utf-8"))

ANCHOR = re.compile(r"<a\b[^>]*href=/(?:[^/]+/)?poem/\d+\.html[^>]*>(.*?)</a>", re.S)
TAGS = re.compile(r"<[^>]*>")
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

NODE_RUNNER = r"""
import fs from "fs";
const [,, jsFile, tokenFile] = process.argv;
const source = fs.readFileSync(jsFile, "utf8");
const block = source.split("// <<normalize-ar>>")[1].split("// <</normalize-ar>>")[0];
const normalize = new Function(block + "\nreturn normalizeArabicQuery;")();
const tokens = JSON.parse(fs.readFileSync(tokenFile, "utf8"));
process.stdout.write(JSON.stringify(tokens.map(token => normalize(token))));
"""


def page_tokens(html_text: str) -> list[str]:
    """Raw (still diacritized) tokens of one poet page: names + poem titles."""
    visible = []
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html_text, re.S)
    if h1:
        visible.append(TAGS.sub(" ", h1.group(1)))
    for match in ANCHOR.finditer(html_text):
        visible.append(TAGS.sub(" ", match.group(1)))
    return [token for chunk in visible for token in TOKEN.findall(chunk)]


def corpus_tokens(limit: int = 120) -> list[str]:
    poet_dir = DATA / "public-final" / "poet"
    if not poet_dir.exists():
        raise unittest.SkipTest(f"no built tree at {poet_dir}")
    ids = list(FIXTURE["page_ids"]) + list(range(1, limit + 1))
    tokens: list[str] = []
    for page_id in dict.fromkeys(ids):
        page = poet_dir / f"{page_id}.html"
        if page.exists():
            tokens.extend(page_tokens(page.read_text(encoding="utf-8")))
    return tokens


def node_normalize(tokens: list[str]) -> list[str]:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.mjs"
        runner.write_text(NODE_RUNNER, encoding="utf-8")
        token_file = Path(tmp) / "tokens.json"
        token_file.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run([node, str(runner), str(REPO / "static" / "js" / "search.js"), str(token_file)],
                              capture_output=True, check=True)
    return json.loads(proc.stdout.decode("utf-8"))


class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tokens = corpus_tokens()
        if not cls.tokens:
            raise unittest.SkipTest("no tokens collected")

    def test_python_and_js_agree_on_every_corpus_token(self) -> None:
        expected = [normalize_search(token) for token in self.tokens]
        actual = node_normalize(self.tokens)
        mismatches = [(token, want, got) for token, want, got in zip(self.tokens, expected, actual) if want != got]
        self.assertEqual(mismatches[:10], [], f"{len(mismatches)} token(s) normalize differently in JS")

    def test_query_of_the_125_miss_pages_normalizes_to_a_searchable_token(self) -> None:
        query = FIXTURE["query"]
        self.assertEqual(normalize_search(query), FIXTURE["query_normalized"])
        self.assertEqual(node_normalize([query]), [FIXTURE["query_normalized"]])


if __name__ == "__main__":
    unittest.main()
