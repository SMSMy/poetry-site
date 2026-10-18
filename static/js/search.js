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
// D1 search worker (production). Empty string disables the D1 path until
// the worker is deployed; when set, results come from D1 + /search-title/.
const D1_ENDPOINT = ""; // e.g. "https://<worker-subdomain>.workers.dev"
const TITLE_CHUNK_SIZE = 2000;
const titleChunkCache = new Map();

function siteUrl(value) {
  if (value.startsWith(SITE_PREFIX)) return value;
  return SITE_PREFIX + value;
}

function normalizeArabicQuery(value) {
  return value
    .replace(/[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]/gu, "")
    .replace(/ـ/gu, "")
    .replace(/[أإآٱ]/gu, "ا")
    .replace(/ى/gu, "ي")
    .replace(/ة/gu, "ه");
}

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
  const sorted = Object.keys(values || {}).sort((a, b) => a.localeCompare(b, "ar"));
  for (const value of sorted) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${value} (${values[value]})`;
    selectElement.append(option);
  }
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
  for (const result of results) {
    const data = result.data;
    appendResult({
      title: data.meta?.title || "قصيدة",
      url: siteUrl(data.url),
      snippet: cleanSnippet(data.excerpt || data.plain_excerpt),
    });
  }
}

function renderD1Results(payload) {
  status.textContent = payload.shown
    ? `عدد النتائج: ${payload.shown}${payload.has_more ? "+ المزيد" : ""} — المعروض: ${payload.ids.length}`
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
      // D1 full-corpus search (normalized text), titles filled from static chunks
      const response = await fetch(`${D1_ENDPOINT}/search?q=${encodeURIComponent(query)}`);
      if (response.ok) {
        clearResults();
        renderD1Results(await response.json());
        return;
      }
      status.textContent = "تعذر الوصول إلى خدمة D1 — أرجعت إلى فهرس العناوين.";
    }
    pagefind ||= await import(new URL("../pagefind-catalog/pagefind.js", import.meta.url));
    const response = await pagefind.search(query || null, { filters: activeFilters });
    const results = await Promise.all(response.results.slice(0, 30).map(result => result.data()));
    clearResults();
    renderPagefindResults(results, response.results.length);
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
