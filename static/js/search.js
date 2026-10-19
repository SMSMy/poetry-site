const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#search-query");
const resultList = document.querySelector("#search-results");
const status = document.querySelector("#search-status");
const resultsSection = document.querySelector(".results-section");
let pagefind;

// ---- deployment constants ---------------------------------------------------
// Pages project path: https://smsmy.github.io/poetry-site/
const SITE_PREFIX = "/poetry-site";
// D1 search worker (production, deployed 2026-10-19): full-corpus verse search
// over normalised text; titles are filled from the static /search-title/ chunks.
// Empty string disables the D1 path (the Pagefind titles index stays as fallback).
const D1_ENDPOINT = "https://poetry-search.smsmy-poetry.workers.dev";
const TITLE_CHUNK_SIZE = 2000;
const titleChunkCache = new Map();

function siteUrl(value) {
  if (value.startsWith(SITE_PREFIX)) return value;
  return SITE_PREFIX + value;
}

// Canonical Arabic search normalization. The rules (and the canonical JS source)
// live in scripts/normalize_ar.py; tests/test_normalize_parity.py fails if the two
// blocks ever drift, and re-runs both implementations over real corpus tokens.
// <<normalize-ar>>
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
// <</normalize-ar>>

// The cache holds the in-flight promise too: a page of 100 results asks for the same
// chunk 100 times before the first fetch resolves, and without it every call would
// issue its own request. A failed chunk is evicted so a later search can retry.
async function loadTitleChunk(chunkIndex) {
  if (!Number.isFinite(chunkIndex) || chunkIndex < 0) return null;
  if (!titleChunkCache.has(chunkIndex)) {
    const pending = fetch(siteUrl(`/search-title/${chunkIndex}.json`), { priority: "low" })
      .then(response => response.ok ? response.json() : null)
      .catch(() => null);
    titleChunkCache.set(chunkIndex, pending);
    const chunk = await pending;
    if (!chunk) titleChunkCache.delete(chunkIndex);
    return chunk;
  }
  return titleChunkCache.get(chunkIndex);
}

// Titles live in fixed id-ranges: chunkIndex = floor((id-1) / TITLE_CHUNK_SIZE) and
// offset = id - chunk.start. Missing ids (the corpus gap 32334-32338) are stored as
// null, and a null/out-of-range hit falls back to the neighbouring chunks so the same
// code also resolves ids against the older sequentially-sliced chunks.
async function titleFor(id) {
  const chunkIndex = Math.floor((id - 1) / TITLE_CHUNK_SIZE);
  const chunk = await loadTitleChunk(chunkIndex);
  const offset = chunk ? id - chunk.start : -1;
  if (chunk && offset >= 0 && offset < chunk.titles.length && chunk.titles[offset] != null) {
    return chunk.titles[offset] || "";
  }
  for (const alt of [chunkIndex - 1, chunkIndex + 1]) {
    const neighbor = await loadTitleChunk(alt);
    if (!neighbor) continue;
    const altOffset = id - neighbor.start;
    if (altOffset >= 0 && altOffset < neighbor.titles.length && neighbor.titles[altOffset] != null) {
      return neighbor.titles[altOffset] || "";
    }
  }
  return "";
}

function cleanSnippet(html) {
  const parsed = new DOMParser().parseFromString(html || "", "text/html");
  const output = document.createElement("span");
  const copy = (source, target) => {
    for (const node of source.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) target.append(document.createTextNode(node.textContent || ""));
      else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "MARK") {
        const mark = document.createElement("mark");
        copy(node, mark);
        target.append(mark);
      } else if (node.nodeType === Node.ELEMENT_NODE) copy(node, target);
    }
  };
  copy(parsed.body, output);
  return output;
}

async function resolvePagefindIndex() {
  if (pagefind) return pagefind;
  let lastError = null;
  const candidates = [
    new URL("../pagefind-catalog/pagefind.js", import.meta.url),
    new URL("./pagefind-catalog/pagefind.js", import.meta.url),
    new URL("/poetry-site/pagefind-catalog/pagefind.js", import.meta.url),
  ];
  for (const candidate of candidates) {
    try {
      const module = await import(candidate);
      if (module && typeof module.search === "function") {
        pagefind = module;
        return pagefind;
      }
      lastError = new Error("pagefind module has no search() in " + candidate.href);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("pagefind index not found");
}

function clearResults() {
  resultList.replaceChildren();
}

function appendResult({ title, url, snippet }) {
  const item = document.createElement("li");
  item.className = "search-result";
  const link = document.createElement("a");
  link.href = url;
  link.textContent = title || "قصيدة";
  if (snippet) {
    const p = document.createElement("p");
    p.className = "result-snippet";
    p.append(snippet);
    item.append(p);
  }
  item.prepend(link);
  resultList.append(item);
}

function renderPagefindResults(results, total) {
  status.textContent = total ? `عدد النتائج: ${total} — المعروض: ${results.length}` : "لا توجد نتائج مطابقة.";
  for (const data of results) {
    appendResult({
      title: data.meta?.title || "قصيدة",
      url: siteUrl(data.url),
      snippet: cleanSnippet(data.excerpt || data.plain_excerpt),
    });
  }
}

// ---- ranking -----------------------------------------------------------------
// Exact matches first. Pagefind's quoted query returns only pages holding the
// normalized token itself, while the loose query also matches prefix/soft variants
// (حب -> حبيب/المحبة, ليل -> الليل/ليالي). Measured on the 2012-poet-page catalog
// (see SEARCH_QA.md): the loose query alone put that noise first — precision@20
// 50%/75%/50% for ليل/شوق/حب — while exact-first reached precision@20 100% on every
// query of the QA battery without losing recall, because the loose matches follow
// after the exact ones. Set to false to fall back to the plain loose search.
const EXACT_FIRST = true;
const MAX_RESULTS = 30;

async function pagefindResults(index, query) {
  if (!query) {
    const loose = await index.search(null);
    return {
      results: await Promise.all(loose.results.slice(0, MAX_RESULTS).map(r => r.data())),
      total: loose.results.length,
    };
  }
  if (!EXACT_FIRST) {
    const loose = await index.search(query);
    return {
      results: await Promise.all(loose.results.slice(0, MAX_RESULTS).map(r => r.data())),
      total: loose.results.length,
    };
  }
  const exactQuery = `"${String(query).replace(/"/g, "")}"`;
  const [exact, loose] = await Promise.all([
    index.search(exactQuery),
    index.search(query),
  ]);
  const [exactData, looseData] = await Promise.all([
    Promise.all(exact.results.slice(0, MAX_RESULTS).map(r => r.data())),
    Promise.all(loose.results.slice(0, MAX_RESULTS * 2).map(r => r.data())),
  ]);
  const seen = new Set();
  const merged = [];
  for (const data of [...exactData, ...looseData]) {
    if (!data || seen.has(data.url)) continue;
    seen.add(data.url);
    merged.push(data);
  }
  return { results: merged.slice(0, MAX_RESULTS), total: loose.results.length };
}

function renderD1Results(payload) {
  status.textContent = payload.shown
    ? (payload.has_more
        ? "نتائج أكثر من 100 — تُعرض الأولى فقط."
        : `عدد النتائج: ${payload.shown}`)
    : "لا توجد نتائج مطابقة.";
  for (const id of payload.ids) {
    appendResult({ title: "", url: siteUrl(`/poem/${id}.html`), snippet: null });
    const last = resultList.lastElementChild;
    const link = last.querySelector("a");
    titleFor(id).then(title => { link.textContent = title || `قصيدة ${id}`; });
  }
}

// Shareable search links: after a successful search the address bar carries ?q=,
// while any other query parameter and the hash are preserved.
function rememberQuery(rawValue) {
  const url = new URL(window.location.href);
  const params = new URLSearchParams(url.search);
  params.delete("q");
  const rest = params.toString();
  const encoded = `q=${encodeURIComponent(rawValue)}`;
  url.search = rest ? `${rest}&${encoded}` : encoded;
  history.replaceState(null, "", url);
}

async function runSearch() {
  const rawValue = queryInput.value.trim();
  const query = normalizeArabicQuery(rawValue);
  if (!query) {
    status.textContent = "اكتب كلمة للبحث في الديوان.";
    clearResults();
    return;
  }
  resultsSection?.setAttribute("aria-busy", "true");
  status.textContent = "جارٍ البحث…";
  try {
    if (D1_ENDPOINT) {
      // D1 full-corpus search, titles filled from static chunks. The RAW query is sent:
      // the worker normalises with the same rules the D1 index was built with (ؤ/ئ kept
      // as-is), while normalizeArabicQuery maps ؤ→و and ئ→ي for the Pagefind twins.
      const response = await fetch(`${D1_ENDPOINT}/search?q=${encodeURIComponent(rawValue)}`);
      if (response.ok) {
        clearResults();
        renderD1Results(await response.json());
        rememberQuery(rawValue);
        return;
      }
      status.textContent = "تعذر الوصول إلى خدمة D1 — أرجعت إلى فهرس العناوين.";
    }
    const index = await resolvePagefindIndex();
    const { results, total } = await pagefindResults(index, query);
    clearResults();
    renderPagefindResults(results, total);
    rememberQuery(rawValue);
  } catch (error) {
    status.textContent = "تعذر تحميل فهرس البحث. شغّل الموقع عبر خادم محلي بعد بناء Pagefind.";
    console.error(error);
  } finally {
    resultsSection?.setAttribute("aria-busy", "false");
  }
}

if (form) {
  form.addEventListener("submit", event => { event.preventDefault(); runSearch(); });
  const initialQuery = new URLSearchParams(window.location.search).get("q");
  if (initialQuery && initialQuery.trim()) {
    queryInput.value = initialQuery;
    runSearch();
  }
}
