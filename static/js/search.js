const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#search-query");
const resultList = document.querySelector("#search-results");
const status = document.querySelector("#search-status");
const resultsSection = document.querySelector(".results-section");
const filterIds = { poet: "#filter-poet", era: "#filter-era", meter: "#filter-meter", rhyme: "#filter-rhyme" };
let pagefind;
let filters = {};

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

async function titleFor(id) {
  const chunkIndex = Math.floor(id / TITLE_CHUNK_SIZE);
  if (!titleChunkCache.has(chunkIndex)) {
    const response = await fetch(siteUrl(`/search-title/${chunkIndex}.json`), { priority: "low" });
    if (!response.ok) return "";
    titleChunkCache.set(chunkIndex, await response.json());
  }
  const chunk = titleChunkCache.get(chunkIndex);
  const offset = id - chunk.start;
  return chunk.titles[offset] || "";
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

function addOptions(select, values, firstLabel) {
  const selectElement = document.querySelector(select);
  if (!selectElement) return;
  if (firstLabel) {
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = firstLabel;
    selectElement.append(defaultOption);
  }
  const sorted = Object.keys(values || {}).sort((a, b) => a.localeCompare(b, "ar"));
  for (const value of sorted) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${value} (${values[value]})`;
    selectElement.append(option);
  }
}

function currentFilters() {
  const selected = {};
  for (const [key, selector] of Object.entries(filterIds)) {
    const value = document.querySelector(selector)?.value;
    if (value && value !== "") selected[key] = value;
  }
  return selected;
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

function emptyFiltersFor(module) {
  if (!module || typeof module.filters !== "function") return {};
  return module.filters().then(values => {
    const selected = {};
    for (const key of Object.keys(values || {})) selected[key] = [];
    return selected;
  }).catch(() => ({}));
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

async function pagefindResults(index, query, filters) {
  if (!query) {
    const loose = await index.search(null, { filters });
    return {
      results: await Promise.all(loose.results.slice(0, MAX_RESULTS).map(r => r.data())),
      total: loose.results.length,
    };
  }
  if (!EXACT_FIRST) {
    const loose = await index.search(query, { filters });
    return {
      results: await Promise.all(loose.results.slice(0, MAX_RESULTS).map(r => r.data())),
      total: loose.results.length,
    };
  }
  const exactQuery = `"${String(query).replace(/"/g, "")}"`;
  const [exact, loose] = await Promise.all([
    index.search(exactQuery, { filters }),
    index.search(query, { filters }),
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

async function runSearch() {
  const rawValue = queryInput.value.trim();
  const query = normalizeArabicQuery(rawValue);
  const activeFilters = currentFilters();
  if (!query && !Object.keys(activeFilters).length) {
    status.textContent = "اكتب كلمة أو اختر مرشحًا للبحث.";
    clearResults();
    return;
  }
  resultsSection?.setAttribute("aria-busy", "true");
  status.textContent = "جارٍ البحث…";
  try {
    if (D1_ENDPOINT && !Object.keys(activeFilters).length) {
      // D1 full-corpus search, titles filled from static chunks. The RAW query is sent:
      // the worker normalises with the same rules the D1 index was built with (ؤ/ئ kept
      // as-is), while normalizeArabicQuery maps ؤ→و and ئ→ي for the Pagefind twins.
      const response = await fetch(`${D1_ENDPOINT}/search?q=${encodeURIComponent(rawValue)}`);
      if (response.ok) {
        clearResults();
        renderD1Results(await response.json());
        return;
      }
      status.textContent = "تعذر الوصول إلى خدمة D1 — أرجعت إلى فهرس العناوين.";
    }
    const index = await resolvePagefindIndex();
    const { results, total } = await pagefindResults(index, query, activeFilters);
    clearResults();
    renderPagefindResults(results, total);
  } catch (error) {
    status.textContent = "تعذر تحميل فهرس البحث. شغّل الموقع عبر خادم محلي بعد بناء Pagefind.";
    console.error(error);
  } finally {
    resultsSection?.setAttribute("aria-busy", "false");
  }
}

if (form) {
  form.addEventListener("submit", event => { event.preventDefault(); runSearch(); });
  for (const selector of Object.values(filterIds)) document.querySelector(selector)?.addEventListener("change", runSearch);
  import(new URL("../pagefind-catalog/pagefind.js", import.meta.url)).then(async module => {
    pagefind = module;
    filters = await pagefind.filters();
    addOptions(filterIds.poet, filters.poet, "كل الشعراء");
    addOptions(filterIds.era, filters.era, "كل العصور");
    addOptions(filterIds.meter, filters.meter, "كل البحور");
    addOptions(filterIds.rhyme, filters.rhyme, "كل القوافي");
  }).catch(error => console.warn("Pagefind index is not built yet", error));
}
