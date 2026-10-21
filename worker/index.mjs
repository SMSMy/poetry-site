// Production search worker — READ-ONLY over the D1 FTS5 index (contentless).
// Differences vs the research/dev worker in poetry-static:
//   * /meas removed (measurement endpoint is a research-only affordance).
//   * CORS restricted to the Pages origin below.
//   * Hard query length cap (rejected before tokenization, never echoed back).
//   * No write/import endpoints at all — imports run OUTSIDE this worker
//     (a temporary import worker gets deployed for the import window,
//     then permanently deleted; see README).
//
// Batch 4 note: poem_fts is rebuilt as detail='full' + content='', so phrase
// queries work and bm25 rank is meaningful; both result layers now use
// ORDER BY rank, rowid (rowid keeps paging stable on equal scores). poem_meta
// carries first_verse plus the era/meter/rhyme/poet filter columns, and /facets
// feeds the UI filter lists. Both features are capability-detected, so this
// worker is also safe to deploy before the rebuild: the phrase layer is skipped
// on the old detail='column' index and metadata features degrade to "ignored"
// until poem_meta exists.
const ALLOWED_ORIGIN = "https://smsmy.github.io";
const MAX_QUERY_CHARS = 80;
const MAX_TERMS = 8;
const MAX_TERM_LENGTH = 24;
const PAGE_SIZE = 100;
const MAX_OFFSET = 500;
const MAX_FILTER_VALUE_CHARS = 64;
const CACHE_VERSION = 4;
const CACHE_TTL_SECONDS = 3600;
const FTS_SPLIT = new RegExp("[^\\u0621-\\u063A\\u0641-\\u064A]", "g");

// /facets statements: one GROUP BY per column, read-only. `rhyme` in the API
// maps to the rhyme_letter column; counts come from the imported corpus.
const FACET_QUERIES = {
  era: "SELECT era AS value, COUNT(*) AS count FROM poem_meta WHERE era IS NOT NULL AND era <> '' GROUP BY era ORDER BY count DESC, value ASC",
  meter: "SELECT meter AS value, COUNT(*) AS count FROM poem_meta WHERE meter IS NOT NULL AND meter <> '' GROUP BY meter ORDER BY count DESC, value ASC",
  rhyme: "SELECT rhyme_letter AS value, COUNT(*) AS count FROM poem_meta WHERE rhyme_letter IS NOT NULL AND rhyme_letter <> '' GROUP BY rhyme_letter ORDER BY value ASC",
};

// Canonical Arabic search normalization, kept as a verbatim mirror of the JS
// block in scripts/normalize_ar.py (`JS_SOURCE`, also shipped in
// static/js/search.js) between the markers below. tests/test_worker_normalize.py
// extracts this block, compares it against the canonical source and runs it on
// fixed cases, so the worker, the index and the UI cannot drift apart.
// <<normalize-ar-worker>>
const ARABIC_MARKS = /[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]/gu;
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
}
// <</normalize-ar-worker>>

function buildTokens(raw) {
  const cleaned = normalizeArabicQuery(String(raw ?? ""));
  const seen = new Set();
  const tokens = [];
  for (const token of cleaned.split(FTS_SPLIT)) {
    if (!token) continue;
    if (seen.has(token)) continue;
    seen.add(token);
    tokens.push(token.slice(0, MAX_TERM_LENGTH));
    if (tokens.length >= MAX_TERMS) break;
  }
  return tokens;
}

function andExpr(tokens) {
  // Explicit AND between quoted tokens: tokens are normalized Arabic letter
  // runs, so they can never contain a quote or an FTS5 operator.
  return tokens.map((token) => `"${token}"`).join(" AND ");
}

function phraseExpr(tokens) {
  // The whole query as one phrase; the AND layer remains the complete set.
  if (tokens.length < 2) return null;
  return `"${tokens.join(" ")}"`;
}

function parseOffset(value) {
  if (value === null || value === "") return 0;
  if (!/^\d+$/.test(value)) return 0;
  return Math.min(Number(value), MAX_OFFSET);
}

// Optional metadata filters. Values come from /facets (the UI echoes them back
// verbatim), so they are matched as-is; a value over the cap or of the wrong
// shape is ignored rather than rejected, and column names are a hardcoded
// whitelist — values only ever travel as bound parameters.
function parseFilters(searchParams) {
  const text = (name) => {
    const value = (searchParams.get(name) || "").trim();
    return value && value.length <= MAX_FILTER_VALUE_CHARS ? value : null;
  };
  const poet = (searchParams.get("poet") || "").trim();
  return {
    era: text("era"),
    meter: text("meter"),
    rhyme: text("rhyme"),
    poet: /^\d{1,9}$/.test(poet) ? Number(poet) : null,
  };
}

// WHERE fragments for the poem_meta JOIN, fixed column order.
function filterWhere(filters) {
  const clauses = [];
  const binds = [];
  if (filters.era !== null) {
    clauses.push("m.era = ?");
    binds.push(filters.era);
  }
  if (filters.meter !== null) {
    clauses.push("m.meter = ?");
    binds.push(filters.meter);
  }
  if (filters.rhyme !== null) {
    clauses.push("m.rhyme_letter = ?");
    binds.push(filters.rhyme);
  }
  if (filters.poet !== null) {
    clauses.push("m.poet_id = ?");
    binds.push(filters.poet);
  }
  return { clauses, binds };
}

// Stand-in for the filter set on a pre-rebuild index (no poem_meta).
const NO_FILTERS = { era: null, meter: null, rhyme: null, poet: null };

// Both layers share the same statement shape: optional poem_meta filters, a
// bound MATCH expression, and ORDER BY rank, rowid. rank (bm25) is only
// meaningful after the batch-4 rebuild; rowid keeps paging stable on ties.
async function queryIds(env, match, limit, filters) {
  const { clauses, binds } = filterWhere(filters);
  const sql =
    "SELECT poem_fts.rowid AS id FROM poem_fts" +
    (clauses.length ? " JOIN poem_meta m ON m.rowid = poem_fts.rowid" : "") +
    " WHERE poem_fts MATCH ?" +
    (clauses.length ? " AND " + clauses.join(" AND ") : "") +
    " ORDER BY rank, rowid LIMIT ?";
  const result = await env.DB.prepare(sql).bind(match, ...binds, limit).all();
  return {
    ids: (result.results || []).map((row) => row.id),
    duration: result.meta?.duration ?? 0,
  };
}

// detail='column' (and 'none') cannot answer phrase/NEAR queries. FTS5's
// default detail is 'full', so only an explicit column/none disables it.
let phraseSupport = null;
async function supportsPhraseQueries(env) {
  if (phraseSupport !== null) return phraseSupport;
  try {
    const row = await env.DB.prepare(
      "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'poem_fts' LIMIT 1",
    ).first();
    const schema = String(row?.sql || "");
    phraseSupport = Boolean(schema) && !/detail\s*=\s*'?(column|none)'?/i.test(schema);
  } catch {
    phraseSupport = false;
  }
  return phraseSupport;
}

// poem_meta arrives in the same batch-4 rebuild. Until it exists, metadata
// features (filters, verse snippets, /facets) are ignored instead of failing
// the request. Cached per isolate like the detail probe above.
let metaSupport = null;
async function supportsPoemMeta(env) {
  if (metaSupport !== null) return metaSupport;
  try {
    const row = await env.DB.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'poem_meta' LIMIT 1",
    ).first();
    metaSupport = Boolean(row && row.name);
  } catch {
    metaSupport = false;
  }
  return metaSupport;
}

function mergeIds(phraseIds, andIds) {
  const seen = new Set();
  const merged = [];
  for (const id of [...phraseIds, ...andIds]) {
    if (seen.has(id)) continue;
    seen.add(id);
    merged.push(id);
  }
  return merged;
}

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (origin && origin !== ALLOWED_ORIGIN) return null;
  return origin
    ? { "Access-Control-Allow-Origin": origin, Vary: "Origin" }
    : {};
}

// /search and /facets share one key, so both endpoints draw from the same
// 30/minute per-IP budget rather than doubling the allowance.
async function isRateLimited(env, request) {
  const limiter = env.SEARCH_RATE_LIMIT;
  if (!limiter) return false;
  const client = request.headers.get("CF-Connecting-IP") || "unknown";
  try {
    const { success } = await limiter.limit({ key: `search:${client}` });
    return !success;
  } catch {
    return false; // fail open: a limiter outage must not take search down
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      const headers = corsHeaders(request);
      if (headers === null) {
        return new Response(null, { status: 403 });
      }
      return new Response(null, {
        status: 204,
        headers: {
          ...headers,
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    if (url.pathname === "/health") {
      const headers = corsHeaders(request);
      if (headers === null) return new Response(null, { status: 403 });
      return Response.json({ ok: true }, { headers });
    }

    if (url.pathname === "/search") {
      const headers = corsHeaders(request);
      if (headers === null) return new Response(null, { status: 403 });

      if (await isRateLimited(env, request)) {
        return Response.json({ error: "rate_limited" }, {
          status: 429,
          headers: { ...headers, "Retry-After": "60" },
        });
      }

      const raw = url.searchParams.get("q") || "";
      if (raw.length > MAX_QUERY_CHARS) {
        return Response.json({ error: "query_too_long" }, { status: 400, headers });
      }
      const tokens = buildTokens(raw);
      if (!tokens.length) {
        return Response.json({ error: "empty_query" }, { status: 400, headers });
      }

      const offset = parseOffset(url.searchParams.get("offset"));
      const filters = parseFilters(url.searchParams);
      const fetchLimit = offset + PAGE_SIZE + 1;
      // Clean, request-independent cache key: the incoming Request must never
      // leak visitor headers into the cache identity. Filters are part of the
      // identity, and the version suffix invalidates entries cached by older
      // workers (which echoed raw queries or lacked the batch-4 behavior).
      const cacheKey = new Request(
        "https://poetry.internal/search?terms=" +
          encodeURIComponent(tokens.join(" ")) +
          "&era=" + encodeURIComponent(filters.era || "") +
          "&meter=" + encodeURIComponent(filters.meter || "") +
          "&rhyme=" + encodeURIComponent(filters.rhyme || "") +
          "&poet=" + (filters.poet ?? "") +
          "&offset=" + offset +
          "&v=" + CACHE_VERSION,
        { method: "GET" },
      );
      let cached = null;
      try {
        cached = await caches.default.match(cacheKey);
      } catch {
        // Cache is best-effort; continue to D1 on a cache error.
      }
      if (cached) {
        const hit = new Response(cached.body, cached);
        hit.headers.set("x-cache", "hit");
        for (const [key, value] of Object.entries(headers)) hit.headers.set(key, value);
        return hit;
      }

      try {
        const meta = await supportsPoemMeta(env);
        const activeFilters = meta ? filters : NO_FILTERS;
        let phraseIds = [];
        let phraseDuration = 0;

        // Phrase-first layer (only meaningful with >=2 tokens and detail='full').
        if (tokens.length > 1 && (await supportsPhraseQueries(env))) {
          const phrase = phraseExpr(tokens);
          if (phrase) {
            try {
              const phraseResult = await queryIds(env, phrase, fetchLimit, activeFilters);
              phraseIds = phraseResult.ids;
              phraseDuration = phraseResult.duration;
            } catch {
              // Do not fail the request if phrase evaluation is unavailable or
              // too complex; the AND layer below is the complete result set.
              phraseSupport = false;
            }
          }
        }

        const andResult = await queryIds(env, andExpr(tokens), fetchLimit, activeFilters);
        const merged = mergeIds(phraseIds, andResult.ids);
        const page = merged.slice(offset, offset + PAGE_SIZE);
        const hasMore = merged.length > offset + PAGE_SIZE;
        const nextCandidate = offset + page.length;
        const nextOffset = hasMore && nextCandidate <= MAX_OFFSET ? nextCandidate : null;

        // Optional first-verse snippets for this page (poem_meta). Best-effort:
        // the field is omitted when poem_meta is absent (pre-rebuild index) or
        // the lookup fails, and rows without a verse are skipped.
        let verses;
        if (meta && page.length) {
          try {
            const placeholders = page.map(() => "?").join(", ");
            const verseResult = await env.DB.prepare(
              `SELECT rowid AS id, first_verse FROM poem_meta WHERE rowid IN (${placeholders})`,
            ).bind(...page).all();
            const found = {};
            for (const row of verseResult.results || []) {
              if (row.first_verse) found[row.id] = row.first_verse;
            }
            if (Object.keys(found).length) verses = found;
          } catch {
            verses = undefined;
          }
        }

        const payload = {
          tokens,
          offset,
          ids: page,
          shown: page.length,
          has_more: hasMore,
          next_offset: nextOffset,
          duration_ms: Math.round((phraseDuration + andResult.duration) * 1000) / 1000,
          ...(verses ? { verses } : {}),
        };
        const response = Response.json(payload, { headers });
        response.headers.set("x-cache", "miss");
        const cachedCopy = response.clone();
        cachedCopy.headers.set(
          "Cache-Control",
          `public, max-age=${CACHE_TTL_SECONDS}, s-maxage=${CACHE_TTL_SECONDS}`,
        );
        if (ctx?.waitUntil) ctx.waitUntil(caches.default.put(cacheKey, cachedCopy));
        return response;
      } catch {
        return Response.json({ error: "search_failed" }, { status: 500, headers });
      }
    }

    if (url.pathname === "/facets") {
      // Filter value lists for the UI (era / meter / rhyme): one GROUP BY per
      // column, served by a single D1 batch and cached for an hour. Shares the
      // /search rate-limit budget; on a pre-rebuild index it reports empty
      // lists without caching them, so the first post-rebuild call is fresh.
      const headers = corsHeaders(request);
      if (headers === null) return new Response(null, { status: 403 });

      if (await isRateLimited(env, request)) {
        return Response.json({ error: "rate_limited" }, {
          status: 429,
          headers: { ...headers, "Retry-After": "60" },
        });
      }

      const cacheKey = new Request(
        "https://poetry.internal/facets?v=" + CACHE_VERSION,
        { method: "GET" },
      );
      let cached = null;
      try {
        cached = await caches.default.match(cacheKey);
      } catch {
        // Cache is best-effort; continue to D1 on a cache error.
      }
      if (cached) {
        const hit = new Response(cached.body, cached);
        hit.headers.set("x-cache", "hit");
        for (const [key, value] of Object.entries(headers)) hit.headers.set(key, value);
        return hit;
      }

      try {
        if (!(await supportsPoemMeta(env))) {
          return Response.json({ era: [], meter: [], rhyme: [] }, {
            headers: { ...headers, "x-cache": "miss" },
          });
        }
        const [era, meter, rhyme] = await env.DB.batch([
          env.DB.prepare(FACET_QUERIES.era),
          env.DB.prepare(FACET_QUERIES.meter),
          env.DB.prepare(FACET_QUERIES.rhyme),
        ]);
        const collect = (result) =>
          (result.results || []).map((row) => ({ value: row.value, count: row.count }));
        const payload = { era: collect(era), meter: collect(meter), rhyme: collect(rhyme) };
        const response = Response.json(payload, { headers });
        response.headers.set("x-cache", "miss");
        const cachedCopy = response.clone();
        cachedCopy.headers.set(
          "Cache-Control",
          `public, max-age=${CACHE_TTL_SECONDS}, s-maxage=${CACHE_TTL_SECONDS}`,
        );
        if (ctx?.waitUntil) ctx.waitUntil(caches.default.put(cacheKey, cachedCopy));
        return response;
      } catch {
        return Response.json({ error: "facets_failed" }, { status: 500, headers });
      }
    }

    return Response.json({ error: "not_found" }, { status: 404 });
  },
};
