#!/usr/bin/env python3
"""Worker-side normalization parity with the canonical rules (no site data needed).

`worker/index.mjs` carries `normalize_search` as a verbatim mirror of
`scripts/normalize_ar.py::JS_SOURCE`, kept between the `// <<normalize-ar-worker>>`
markers. Two independent guards:

  1. text: the marked block must equal `JS_SOURCE` exactly, so a one-sided edit
     (like the batch-4 removal of NFC plus the ؤ/ئ and whitespace rules) cannot
     silently drift from the index/UI normalization;
  2. behaviour: node executes the marked block over fixed cases — ؤ/ئ, hamza
     forms, diacritics, tatweel, whitespace — and every result must equal
     `normalize_search` on the same input.

Requires `node` on PATH (skipped with a clear message otherwise).

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from normalize_ar import JS_SOURCE, normalize_search  # noqa: E402

WORKER = REPO / "worker" / "index.mjs"
OPEN_MARK = "// <<normalize-ar-worker>>"
CLOSE_MARK = "// <</normalize-ar-worker>>"

# Fixed cases, ordered by intent: the ؤ/ئ rules, the alef/hamza forms, diacritics,
# tatweel, whitespace folding, and two guards against over-normalization.
FIXED_CASES = {
    "مؤمن": "مومن",
    "رؤية": "رويه",
    "قائد": "قايد",
    "لَمّا": "لما",
    "قُرْآن": "قران",
    "أَرى": "اري",
    "إبان": "ابان",
    "مـــزيد": "مزيد",
    "  قفا \t نبك \n": "قفا نبك",
    "الفتى": "الفتي",
    "رحمه": "رحمه",
    "poem 123": "poem 123",
    "": "",
}

NODE_RUNNER = r"""
import fs from "fs";
const [,, workerFile, casesFile] = process.argv;
const source = fs.readFileSync(workerFile, "utf8");
if (!source.includes("// <<normalize-ar-worker>>")) {
  console.error("worker is missing the <<normalize-ar-worker>> markers");
  process.exit(2);
}
const block = source.split("// <<normalize-ar-worker>>")[1].split("// <</normalize-ar-worker>>")[0];
const normalize = new Function(block + "\nreturn normalizeArabicQuery;")();
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
process.stdout.write(JSON.stringify(cases.map(value => normalize(value))));
"""


def worker_block() -> str:
    source = WORKER.read_text(encoding="utf-8")
    if OPEN_MARK not in source or CLOSE_MARK not in source:
        raise AssertionError(f"{WORKER} is missing the <<normalize-ar-worker>> markers")
    return source.split(OPEN_MARK, 1)[1].split(CLOSE_MARK, 1)[0]


class WorkerNormalizeTextTests(unittest.TestCase):
    def test_block_is_wrapped_in_markers(self) -> None:
        self.assertTrue(worker_block().strip())

    def test_block_matches_the_canonical_js_source(self) -> None:
        self.assertEqual(worker_block().strip(), JS_SOURCE.strip())


class WorkerNormalizeBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "runner.mjs"
            runner.write_text(NODE_RUNNER, encoding="utf-8")
            cases = Path(tmp) / "cases.json"
            cases.write_text(json.dumps(list(FIXED_CASES), ensure_ascii=False), encoding="utf-8")
            proc = subprocess.run([node, str(runner), str(WORKER), str(cases)],
                                  capture_output=True, check=True)
        cls.actual = json.loads(proc.stdout.decode("utf-8"))

    def test_fixed_cases_are_normalized_as_expected(self) -> None:
        self.assertEqual(self.actual, list(FIXED_CASES.values()))

    def test_matches_normalize_search_on_every_case(self) -> None:
        expected = [normalize_search(case) for case in FIXED_CASES]
        self.assertEqual(self.actual, expected)


if __name__ == "__main__":
    unittest.main()
