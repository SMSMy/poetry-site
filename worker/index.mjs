// Production search worker — READ-ONLY over the D1 FTS5 index (contentless).
// Differences vs the research/dev worker in poetry-static:
//   * /meas removed (measurement endpoint is a research-only affordance).
//   * CORS restricted to the Pages origin below.
//   * Hard query length cap.
//   * No write/import endpoints at all — imports run OUTSIDE this worker
//     (a temporary import worker gets deployed for the import window,
//     then permanently deleted; see README).
const ALLOWED_ORIGIN = "https://smsmy.github.io";
const MAX_QUERY_CHARS = 80;
const MAX_TERMS = 8;
const MAX_TERM_LENGTH = 24;
const FTS_PUNCTUATION = /[\u0621-\u063A\u0641-\u064A]/; // split on everything else
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
  const cleaned = normalizeArabic(String(raw ?? "")).slice(0, MAX_QUERY_CHARS);
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

function matchExpr(tokens) {
  return tokens.map((t) => `"${t}"`).join(" "); // AND of quoted terms; no FTS operators
}

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (origin && origin !== ALLOWED_ORIGIN) return null;
  return origin
    ? { "Access-Control-Allow-Origin": origin, Vary: "Origin" }
    : {};
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

      const raw = url.searchParams.get("q") || "";
      const tokens = buildTokens(raw);
      if (!tokens.length) {
        return Response.json({ error: "empty_query", query: raw }, {
          status: 400, headers,
        });
      }
      const cacheKey = new Request(
        "https://poetry.internal/search?terms=" + encodeURIComponent(tokens.join(" ")),
        request,
      );
      const cached = await caches.default.match(cacheKey);
      if (cached) {
        const hit = new Response(cached.body, cached);
        hit.headers.set("x-cache", "hit");
        hit.headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
        hit.headers.set("Vary", "Origin");
        return hit;
      }
      const match = matchExpr(tokens);
      // "has more" cursor: fetch 101, show 100 — no second COUNT(*) query.
      const limited = await env.DB.prepare(
        `SELECT rowid AS id FROM poem_fts WHERE poem_fts MATCH ?1 LIMIT 101`,
      ).bind(match).all();
      const returned = limited.results.length;
      const shown = limited.results.slice(0, 100);
      const payload = {
        query: raw,
        tokens,
        ids: shown.map((row) => row.id),
        shown: shown.length,
        has_more: returned > 100,
        duration_ms: limited.meta?.duration ?? null,
      };
      const response = Response.json(payload, { headers });
      const cachedCopy = response.clone();
      cachedCopy.headers.set("Cache-Control", "public, max-age=3600, s-maxage=3600");
      ctx.waitUntil(caches.default.put(cacheKey, cachedCopy));
      return response;
    }

    return Response.json({ error: "not_found" }, { status: 404 });
  },
};
