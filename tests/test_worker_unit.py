#!/usr/bin/env python3
"""Worker unit tests with a stubbed `env.DB` (no D1, no network, no site data).

The worker module is imported by node and `fetch()` is called with an in-memory
D1 stub that records every prepared statement instead of executing it. That lets
the tests pin the batch-4 contract without touching the real database:

  * both result layers (phrase and AND) must use `ORDER BY rank, rowid`;
  * era/meter/rhyme/poet filters must add the poem_meta JOIN and bound WHERE
    clauses, ignore invalid values, and degrade gracefully without poem_meta;
  * the temporary ؤ/ئ bridge must be gone (normalized tokens only);
  * /facets must GROUP BY era/meter/rhyme_letter, answer with the documented
    shape, and share the /search rate limit;
  * every captured statement must still parse and run against the real
    batch-4 schema (replayed on a local in-memory SQLite with FTS5).

Requires `node` on PATH (skipped with a clear message otherwise).

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "worker" / "index.mjs"

SCHEMA_FULL = "CREATE VIRTUAL TABLE poem_fts USING fts5(norm, tokenize='unicode61', detail='full', content='')"
SCHEMA_COLUMN = "CREATE VIRTUAL TABLE poem_fts USING fts5(norm, tokenize='unicode61', detail='column', content='')"


def search_url(q: str, **filters: str) -> str:
    params = "".join(f"&{name}={quote(value)}" for name, value in filters.items())
    return f"/search?q={quote(q)}{params}"


# scenario -> (schema, poem_meta present, request URL)
SCENARIOS = {
    "search_full": (SCHEMA_FULL, True,
                    search_url("مؤمن رؤية", era="جاهلي", meter="الطويل", rhyme="ب", poet="42")),
    "search_column": (SCHEMA_COLUMN, True, search_url("قفا نبك")),
    "search_no_meta": (SCHEMA_FULL, False, search_url("حب", era="جاهلي")),
    "search_invalid_filters": (SCHEMA_FULL, True, search_url("حب قلب", era="ا" * 80, poet="abc", meter=" ")),
    "facets": (SCHEMA_FULL, True, "/facets"),
    "facets_limited": (SCHEMA_FULL, True, "/facets"),
    "facets_forbidden": (SCHEMA_FULL, True, "/facets"),
}

NODE_RUNNER = r"""
import { pathToFileURL } from "node:url";

const [,, workerFile, requestUrl, schemaSql, metaPresent, limited, origin] = process.argv;
const worker = (await import(pathToFileURL(workerFile).href)).default;

// Fake D1 rows, chosen from the statement itself. Search statements get two ids
// so paging and the verse map run; poem_meta row 9 has no first_verse and must
// be dropped from the verses map.
function fakeRows(sql) {
  if (/GROUP BY era/.test(sql)) return [{ value: "جاهلي", count: 12 }];
  if (/GROUP BY meter/.test(sql)) return [{ value: "الطويل", count: 7 }];
  if (/GROUP BY rhyme_letter/.test(sql)) return [{ value: "ب", count: 3 }];
  if (/first_verse/.test(sql)) return [{ id: 7, first_verse: "شطر الشاهد" }, { id: 9, first_verse: null }];
  return [{ id: 7 }, { id: 9 }];
}

const captured = [];
const db = {
  prepare(sql) {
    const stmt = {
      sql,
      args: [],
      bind(...args) { stmt.args = args; return stmt; },
      async all() {
        captured.push({ op: "all", sql, args: stmt.args });
        return { results: fakeRows(sql), meta: { duration: 0.5 } };
      },
      async first() {
        captured.push({ op: "first", sql, args: stmt.args });
        if (/name = 'poem_meta'/.test(sql)) return metaPresent === "true" ? { name: "poem_meta" } : null;
        return { sql: schemaSql };
      },
    };
    return stmt;
  },
  async batch(statements) {
    captured.push({ op: "batch", statements: statements.map((s) => ({ sql: s.sql, args: s.args })) });
    return statements.map((s) => ({ results: fakeRows(s.sql), meta: { duration: 0.25 } }));
  },
};

const env = {
  DB: db,
  SEARCH_RATE_LIMIT: { async limit() { return { success: limited !== "true" }; } },
};
const headers = origin ? { Origin: origin } : {};
const response = await worker.fetch(new Request("https://poetry.test" + requestUrl, { headers }), env, {});
const text = await response.text();
process.stdout.write(JSON.stringify({
  status: response.status,
  accessControl: response.headers.get("Access-Control-Allow-Origin"),
  xCache: response.headers.get("x-cache"),
  retryAfter: response.headers.get("Retry-After"),
  body: text ? JSON.parse(text) : null,
  captured,
}));
"""


def run_scenario(node: str, name: str) -> dict:
    schema, meta, url = SCENARIOS[name][:3]
    limited = "true" if name == "facets_limited" else "false"
    origin = "https://evil.example" if name == "facets_forbidden" else ""
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "worker_runner.mjs"
        runner.write_text(NODE_RUNNER, encoding="utf-8")
        proc = subprocess.run(
            [node, str(runner), str(WORKER), url, schema, "true" if meta else "false", limited, origin],
            capture_output=True, check=True)
    return json.loads(proc.stdout.decode("utf-8"))


def search_statements(run: dict) -> list[dict]:
    """The MATCH statements of a /search run, in execution order."""
    return [call for call in run["captured"] if call["op"] == "all" and "MATCH" in call["sql"]]


class WorkerUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not on PATH")
        if not WORKER.exists():
            raise unittest.SkipTest(f"{WORKER} does not exist")
        cls.runs = {name: run_scenario(node, name) for name in SCENARIOS}

    # ---- /search -----------------------------------------------------------

    def test_both_layers_order_by_rank_with_stable_rowid(self) -> None:
        statements = search_statements(self.runs["search_full"])
        self.assertEqual(len(statements), 2, "expected the phrase layer and the AND layer")
        for call in statements:
            order_by = call["sql"].split("ORDER BY", 1)[1]
            self.assertIn("rank", order_by)
            self.assertIn("rowid", order_by)

    def test_filters_add_the_join_and_bound_where_clauses(self) -> None:
        for call in search_statements(self.runs["search_full"]):
            self.assertIn("JOIN poem_meta m ON m.rowid = poem_fts.rowid", call["sql"])
            for clause in ("m.era = ?", "m.meter = ?", "m.rhyme_letter = ?", "m.poet_id = ?"):
                self.assertIn(clause, call["sql"])
            self.assertEqual(call["args"][1:5], ["جاهلي", "الطويل", "ب", 42])
            self.assertEqual(call["args"][5], 101)  # offset 0 + PAGE_SIZE + 1

    def test_invalid_filter_values_are_ignored(self) -> None:
        statements = search_statements(self.runs["search_invalid_filters"])
        self.assertEqual(len(statements), 2)
        for call in statements:
            self.assertNotIn("JOIN", call["sql"])
            self.assertEqual(len(call["args"]), 2)  # match + limit only

    def test_metadata_filters_degrade_when_poem_meta_is_missing(self) -> None:
        run = self.runs["search_no_meta"]
        self.assertEqual(run["status"], 200)
        self.assertNotIn("verses", run["body"])
        for call in run["captured"]:
            sqls = [s["sql"] for s in call.get("statements", [])] + ([call["sql"]] if "sql" in call else [])
            for sql in sqls:
                self.assertNotIn("JOIN poem_meta", sql)
                self.assertNotIn("first_verse", sql)

    def test_the_hamza_bridge_is_gone(self) -> None:
        statements = search_statements(self.runs["search_full"])
        self.assertEqual(statements[0]["args"][0], '"مومن رويه"')  # phrase layer
        self.assertEqual(statements[1]["args"][0], '"مومن" AND "رويه"')  # AND layer
        for call in statements:
            self.assertNotIn(" OR ", call["args"][0])
            self.assertNotIn("ؤ", call["args"][0])
            self.assertNotIn("ئ", call["args"][0])

    def test_column_detail_index_still_skips_the_phrase_layer(self) -> None:
        statements = search_statements(self.runs["search_column"])
        self.assertEqual(len(statements), 1, "detail='column' must skip the phrase layer")
        self.assertIn("ORDER BY", statements[0]["sql"])

    def test_search_payload_keeps_its_contract_and_adds_verses(self) -> None:
        run = self.runs["search_full"]
        self.assertEqual(run["status"], 200)
        body = run["body"]
        for field in ("tokens", "offset", "ids", "shown", "has_more", "next_offset", "duration_ms"):
            self.assertIn(field, body)
        self.assertEqual(body["tokens"], ["مومن", "رويه"])
        self.assertEqual(body["ids"], [7, 9])
        self.assertEqual(body["verses"], {"7": "شطر الشاهد"})

    # ---- /facets -----------------------------------------------------------

    def test_facets_group_by_the_three_columns(self) -> None:
        run = self.runs["facets"]
        self.assertEqual(run["status"], 200)
        batches = [call for call in run["captured"] if call["op"] == "batch"]
        self.assertEqual(len(batches), 1)
        sqls = [s["sql"] for s in batches[0]["statements"]]
        self.assertEqual(len(sqls), 3)
        for sql, key in zip(sqls, ("GROUP BY era", "GROUP BY meter", "GROUP BY rhyme_letter")):
            self.assertIn("FROM poem_meta", sql)
            self.assertIn(key, sql)
        self.assertEqual(run["body"], {
            "era": [{"value": "جاهلي", "count": 12}],
            "meter": [{"value": "الطويل", "count": 7}],
            "rhyme": [{"value": "ب", "count": 3}],
        })

    def test_facets_shares_the_search_rate_limit(self) -> None:
        run = self.runs["facets_limited"]
        self.assertEqual(run["status"], 429)
        self.assertEqual(run["body"], {"error": "rate_limited"})
        self.assertEqual(run["retryAfter"], "60")
        self.assertEqual(run["captured"], [], "rate limiting must happen before any D1 work")

    def test_facets_respects_cors(self) -> None:
        run = self.runs["facets_forbidden"]
        self.assertEqual(run["status"], 403)
        self.assertEqual(run["captured"], [])
        self.assertIsNone(run["accessControl"])

    # ---- the captured SQL must really run ----------------------------------

    def test_captured_sql_runs_on_a_local_batch4_schema(self) -> None:
        try:
            con = sqlite3.connect(":memory:")
            con.execute(SCHEMA_FULL)
        except sqlite3.OperationalError as exc:  # pragma: no cover - FTS5 must be compiled in
            raise unittest.SkipTest(f"local SQLite has no FTS5: {exc}")
        con.execute(
            "CREATE TABLE poem_meta(rowid INTEGER PRIMARY KEY, first_verse TEXT, poet_id INTEGER,"
            " era TEXT, meter TEXT, rhyme_letter TEXT, rhyme_label TEXT)"
        )
        checked = 0
        for run in self.runs.values():
            calls = []
            for call in run["captured"]:
                if call["op"] == "batch":
                    calls.extend(call["statements"])
                elif "sql" in call:
                    calls.append(call)
            for call in calls:
                con.execute(call["sql"], call.get("args", []))
                checked += 1
        self.assertGreater(checked, 8)


if __name__ == "__main__":
    unittest.main()
