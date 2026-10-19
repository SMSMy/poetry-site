// Production search worker — READ-ONLY over the D1 FTS5 index (contentless).
// Differences vs the research/dev worker in poetry-static:
//   * /meas removed (measurement endpoint is a research-only affordance).
//   * CORS restricted to the Pages origin below.
//   * Hard query length cap (rejected before tokenization, never echoed back).
//   * No write/import endpoints at all — imports run OUTSIDE this worker
//     (a temporary import worker gets deployed for the import window,
//     then permanently deleted; see README).
//
// Ranking note (batch 1): poem_fts is currently detail='column' + content='',
// so FTS5 cannot evaluate phrase queries and rank is 0 for every row. The
// phrase layer below is capability-detected and is skipped on this index; it
// becomes active automatically after the batch-4 rebuild with detail='full'.
// Do not add ORDER BY rank until that rebuild is verified.
const ALLOWED_ORIGIN = "https://smsmy.github.io";
const MAX_QUERY_CHARS = 80;
const MAX_TERMS = 8;
const MAX_TERM_LENGTH = 24;
const PAGE_SIZE = 100;
const MAX_OFFSET = 500;
const MAX_PHRASE_VARIANTS = 16;
const CACHE_VERSION = 3;
const CACHE_TTL_SECONDS = 3600;
const FTS_SPLIT = new RegExp("[^\\u0621-\\u063A\\u0641-\\u064A]", "g");

const DIACRITICS = /[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]/g;
const TATWEEL = /\u0640/g;
function normalizeArabic(value) {
  return value
    .normalize("NFC")
    .replace(DIACRITICS, "")
    .replace(TATWEEL, "")
    .replace(/[\u0622\u0623\u0625\u0671]/g, "\u0627")
    .replace(/\u0649/g, "\u064a")
    .replace(/\u0629/g, "\u0647");
}

function buildTokens(raw) {
  const cleaned = normalizeArabic(String(raw ?? ""));
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

// Temporary ؤ/ئ bridge until the batch-4 import unifies the content. The D1
// index keeps ؤ/ئ as written, while the UI normalizes them to و/ي, so search
// each spelling within the same token group: (مؤمن OR مومن). The reverse
// direction (مومن -> مؤمن) is included so both spellings converge before the
// index rebuild; it only applies to tokens without ؤ/ئ, avoiding odd mixes.
function tokenVariants(token) {
  const variants = [token];
  if (/[\u0624\u0626]/.test(token)) {
    const plain = token.replace(/\u0624/g, "\u0648").replace(/\u0626/g, "\u064a");
    if (plain !== token) variants.push(plain);
  } else if (/[\u0648\u064a]/.test(token)) {
    const seated = token.replace(/\u0648/g, "\u0624").replace(/\u064a/g, "\u0626");
    if (seated !== token) variants.push(seated);
  }
  return variants;
}

function buildGroups(tokens) {
  return tokens.map(tokenVariants);
}

function orGroup(variants) {
  if (variants.length === 1) return `"${variants[0]}"`;
  return `(${variants.map((variant) => `"${variant}"`).join(" OR ")})`;
}

function andExpr(groups) {
  // Explicit AND is required after a parenthesized OR group; FTS5 rejects
  // ('a' OR 'b') 'c' with a syntax error. Input never contributes operators.
  return groups.map(orGroup).join(" AND ");
}

function phraseExpr(groups) {
  // A phrase with per-token variants needs the cross-product of spellings.
  // Cap it; beyond the cap the AND layer still returns every match.
  let combinations = [[]];
  for (const variants of groups) {
    if (combinations.length * variants.length > MAX_PHRASE_VARIANTS) return null;
    const next = [];
    for (const combination of combinations) {
      for (const variant of variants) next.push([...combination, variant]);
    }
    combinations = next;
  }
  const phrases = combinations.map((combination) => `"${combination.join(" ")}"`);
  return phrases.length === 1 ? phrases[0] : `(${phrases.join(" OR ")})`;
}

function parseOffset(value) {
  if (value === null || value === "") return 0;
  if (!/^\d+$/.test(value)) return 0;
  return Math.min(Number(value), MAX_OFFSET);
}

async function queryIds(env, match, limit) {
  const result = await env.DB.prepare(
    `SELECT rowid AS id FROM poem_fts WHERE poem_fts MATCH ?1 LIMIT ?2`,
  ).bind(match, limit).all();
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
      const fetchLimit = offset + PAGE_SIZE + 1;
      // Clean, request-independent cache key: the incoming Request must never
      // leak visitor headers into the cache identity. The version suffix
      // invalidates entries cached by older workers (which echoed raw queries).
      const cacheKey = new Request(
        "https://poetry.internal/search?terms=" +
          encodeURIComponent(tokens.join(" ")) +
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
        const groups = buildGroups(tokens);
        let phraseIds = [];
        let phraseDuration = 0;

        // Phrase-first layer (only meaningful with >=2 tokens and detail='full').
        if (tokens.length > 1 && (await supportsPhraseQueries(env))) {
          const phrase = phraseExpr(groups);
          if (phrase) {
            try {
              const phraseResult = await queryIds(env, phrase, fetchLimit);
              phraseIds = phraseResult.ids;
              phraseDuration = phraseResult.duration;
            } catch {
              // Do not fail the request if phrase evaluation is unavailable or
              // too complex; the AND layer below is the complete result set.
              phraseSupport = false;
            }
          }
        }

        const andResult = await queryIds(env, andExpr(groups), fetchLimit);
        const merged = mergeIds(phraseIds, andResult.ids);
        const page = merged.slice(offset, offset + PAGE_SIZE);
        const hasMore = merged.length > offset + PAGE_SIZE;
        const nextCandidate = offset + page.length;
        const nextOffset = hasMore && nextCandidate <= MAX_OFFSET ? nextCandidate : null;

        const payload = {
          tokens,
          offset,
          ids: page,
          shown: page.length,
          has_more: hasMore,
          next_offset: nextOffset,
          duration_ms: Math.round((phraseDuration + andResult.duration) * 1000) / 1000,
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

    return Response.json({ error: "not_found" }, { status: 404 });
  },
};
