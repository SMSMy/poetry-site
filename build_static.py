#!/usr/bin/env python3
"""Read-only source audit + deterministic PHP-import simulation + Hugo content export."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_PROJECT = Path(r"D:\مشاريع بائدة\poetry\poetry")
DEFAULT_SOURCE = Path(r"D:\مشاريع بائدة\ديوان\poetry_data\json")
DEFAULT_ROOT = Path(r"D:\مشاريع بائدة\poetry-static")
HARAKAT = re.compile(r"[\u0610-\u061a\u064b-\u0652\u0670\u06d6-\u06ed\u0640]")
PUNCT_SPACE = re.compile(r"[\s\W_]+", flags=re.UNICODE)
NAME_SUFFIX = re.compile(r" \(\d+\)$")
TRAILING_NAME = re.compile(r"[\s_]+$")
MULTI_SPACE = re.compile(r"\s+")
BOUNDARY_ESCAPE = re.compile(r"(?<!\S)(.*?)(?!\S)")
MOJIBAKE = ("\ufffd", "Ã", "Ø", "Ù", "Ð", "Þ")
TRANSLATION = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def text_key(text: str) -> str:
    """Mirror import_diwan.php::text_key for its UTF-8 /u expression."""
    value = HARAKAT.sub("", text)
    value = value.translate(TRANSLATION)
    value = PUNCT_SPACE.sub("", value)
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def normalize_name(name: str) -> str:
    value = HARAKAT.sub("", name.strip()).translate(TRANSLATION)
    value = NAME_SUFFIX.sub("", value)
    value = TRAILING_NAME.sub("", value)
    return MULTI_SPACE.sub(" ", value).strip()


def normalize_search(text: str) -> str:
    value = HARAKAT.sub("", text).replace("ـ", "").translate(TRANSLATION)
    value = value.replace("ٱ", "ا")
    value = MULTI_SPACE.sub(" ", value)
    return value.strip()


def php_int(value):
    """Approximate PHP's (int) cast used by import_diwan.php for scalar CSV/JSON values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            match = re.match(r"^[+-]?\d+", raw)
            return int(match.group(0)) if match else 0


def convert_poem_text(text: str) -> str:
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", text.strip()) if line.strip()]
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "/" in line:
            line = re.sub(r"\s+/\s+", " / ", line)
            result.append(line)
            i += 1
        elif i + 1 < len(lines) and "/" not in lines[i + 1]:
            result.append(f"{line} / {lines[i + 1]}")
            i += 2
        else:
            result.append(line)
            i += 1
    return "\n".join(result) + ("\n" if result else "")


def render_poem_html(poem_text: str) -> str:
    """Pre-render escaped hemistichs so Hugo does not split every poem in templates."""
    cards = []
    for line in poem_text.splitlines():
        line = line.strip()
        if not line:
            continue
        left, separator, right = line.partition("/")
        card = f'<div class="poem-card"><div class="sadr-part">{html.escape(left.strip())}</div>'
        if separator and right.strip():
            card += f'<div class="ajz-part">{html.escape(right.strip())}</div>'
        cards.append(card + "</div>")
    return "".join(cards)


def split_explanation(text: str) -> tuple[str, str, str]:
    pattern = re.compile(r"(?:^|\n)[\t ]*(إضاءة أدبية|معاني المفردات)[\t ]*(?:\n|$)")
    matches = list(pattern.finditer(text))
    if not matches:
        return "", "", text.strip()
    parts = {"إضاءة أدبية": "", "معاني المفردات": ""}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if not parts[match.group(1)]:
            parts[match.group(1)] = content
    return parts["إضاءة أدبية"], parts["معاني المفردات"], ""


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_front_matter(path: Path, params: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["+++", "type = " + toml_string(str(params.pop("type", "")))]
    for key, value in params.items():
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, int):
            encoded = str(value)
        else:
            encoded = toml_string(str(value))
        lines.append(f"{key} = {encoded}")
    lines.extend(["+++", "", body])
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def safe_reset_dir(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_root or resolved_path.name not in {"content", "public", ".cache"}:
        raise RuntimeError(f"Refusing to reset path outside generated project areas: {resolved_path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)
    resolved_path.mkdir(parents=True)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE poets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, era TEXT NOT NULL DEFAULT '',
          info TEXT NOT NULL DEFAULT '', normalized_name TEXT NOT NULL
        );
        CREATE INDEX poets_normalized_name ON poets(normalized_name);
        CREATE TABLE poems (
          id INTEGER PRIMARY KEY AUTOINCREMENT, poet_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
          text TEXT NOT NULL, text_hash TEXT NOT NULL, meter TEXT NOT NULL DEFAULT '',
          rhyme_letter TEXT NOT NULL DEFAULT '', rhyme_label TEXT NOT NULL DEFAULT '',
          verse_count INTEGER, poem_type INTEGER, content_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX poems_poet_hash ON poems(poet_id, text_hash);
        CREATE INDEX poems_poet_id ON poems(poet_id, id);
        CREATE TABLE source_poems (
          source_poet_id TEXT NOT NULL, source_poem_id TEXT NOT NULL, text_hash TEXT NOT NULL,
          explanation TEXT NOT NULL DEFAULT '', PRIMARY KEY (source_poet_id, source_poem_id)
        );
        CREATE TABLE explanations (
          poem_id INTEGER NOT NULL, explanation TEXT NOT NULL,
          PRIMARY KEY (poem_id, explanation)
        );
    """)
    return db


def parse_poem_fields(poem: dict, is_diwan: bool) -> tuple[str, str, str, str, str, int | None, int | None, str]:
    if is_diwan:
        raw_text = str(poem.get("text") or "")
        title = str(poem.get("title") or "")[:500]
        formatted = convert_poem_text(raw_text)
        meter = str(poem.get("meter") or "")[:100]
        rhyme = str(poem.get("rhyme_letter") or "")[:100]
        rhyme_label = str(poem.get("rhyme_label") or "")[:64]
        verse_count = php_int(poem.get("verse_count"))
        poem_type = php_int(poem.get("poem_type"))
        content_hash = str(poem.get("content_hash") or "")
        return title, formatted, meter, rhyme, rhyme_label, verse_count, poem_type, content_hash
    raw_text = str(poem.get("poem") or "")
    first_line = raw_text.strip().splitlines()[0] if raw_text.strip() else ""
    title = first_line.split("//", 1)[0]
    return title, raw_text, str(poem.get("bahr") or ""), str(poem.get("qafiya") or ""), "", None, None, ""


def audit_source(source: Path, base: Path, report: dict) -> tuple[dict, list[str]]:
    index = read_json(source / "_index.json")
    entries = index.get("poets", [])
    poet_entries = [x for x in entries if isinstance(x, dict) and x.get("has_poems") and x.get("file")]
    discovered = list(source.rglob("*.json"))
    malformed: list[str] = []
    empty_files = [str(p.relative_to(source)) for p in discovered if p.stat().st_size == 0]
    duplicate_file_hashes: Counter[str] = Counter()
    source_poet_ids: list[str] = []
    source_poem_ids: Counter[str] = Counter()
    empty_poem_texts = 0
    invalid_poem_records = 0
    replacement_chars = 0
    mojibake_files: set[str] = set()
    hashes_global: Counter[str] = Counter()
    hashes_by_poet: Counter[tuple[str, str]] = Counter()
    source_poem_count = 0
    indexed_ids: set[str] = set()

    for path in discovered:
        if path.name != "_index.json":
            duplicate_file_hashes[hashlib.sha256(path.read_bytes()).hexdigest()] += 1
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") is not None:
            indexed_ids.add(str(entry["id"]))

    for entry in poet_entries:
        file_name = Path(str(entry["file"])).name
        path = source / file_name
        if not path.is_file():
            malformed.append(f"missing file referenced by index: {file_name}")
            continue
        try:
            data = read_json(path)
        except Exception as exc:
            malformed.append(f"invalid JSON {file_name}: {exc}")
            continue
        if not isinstance(data, dict):
            malformed.append(f"non-object JSON {file_name}")
            continue
        if data.get("id") is None:
            malformed.append(f"missing poet id: {file_name}")
        source_poet_ids.append(str(data.get("id", "")))
        for value in (data.get("name", ""), data.get("description", ""), data.get("era", "")):
            if "\ufffd" in str(value):
                replacement_chars += 1
            if any(mark in str(value) for mark in MOJIBAKE[1:]):
                mojibake_files.add(file_name)
        poems = data.get("poems")
        if not isinstance(poems, list):
            malformed.append(f"poems is not an array: {file_name}")
            continue
        for item in poems:
            if not isinstance(item, dict) or item.get("id") is None:
                invalid_poem_records += 1
                continue
            source_poem_count += 1
            source_poem_ids[f"{data.get('id')}:{item.get('id')}"] += 1
            raw = str(item.get("text") or "")
            if not raw.strip():
                empty_poem_texts += 1
                continue
            h = text_key(raw)
            hashes_global[h] += 1
            hashes_by_poet[(str(data.get("id")), h)] += 1
            for key in ("title", "text", "explanation", "name"):
                value = str(item.get(key) or "")
                replacement_chars += value.count("\ufffd")
                if any(mark in value for mark in MOJIBAKE[1:]):
                    mojibake_files.add(file_name)

    duplicates_within_poet = sum(n - 1 for n in hashes_by_poet.values() if n > 1)
    global_duplicate_hashes = sum(n - 1 for n in hashes_global.values() if n > 1)
    referenced = {Path(str(x["file"])).name for x in poet_entries}
    unreferenced = sorted(p.name for p in discovered if p.name != "_index.json" and p.name not in referenced)
    report["source"] = {
        "source_path": str(source),
        "index_poets_total": index.get("poets_total"),
        "index_poems_total": index.get("poems_total"),
        "index_entries": len(entries),
        "index_entries_with_poems_and_file": len(poet_entries),
        "source_json_files_including_index": len(discovered),
        "source_json_bytes": sum(p.stat().st_size for p in discovered),
        "unique_poet_ids_in_index": len(indexed_ids),
        "unique_poet_ids_with_files": len(set(source_poet_ids)),
        "duplicate_poet_ids_with_files": len(source_poet_ids) - len(set(source_poet_ids)),
        "poems_read": source_poem_count,
        "unique_source_poem_keys": len(source_poem_ids),
        "duplicate_source_poem_keys": sum(n - 1 for n in source_poem_ids.values() if n > 1),
        "empty_files": empty_files,
        "empty_poem_texts": empty_poem_texts,
        "invalid_poem_records": invalid_poem_records,
        "duplicate_hashes_within_source_poet": duplicates_within_poet,
        "duplicate_hashes_across_any_source_poet": global_duplicate_hashes,
        "duplicate_json_file_groups": sum(n - 1 for n in duplicate_file_hashes.values() if n > 1),
        "replacement_character_occurrences": replacement_chars,
        "possible_mojibake_files": sorted(mojibake_files),
        "unreferenced_json_files": unreferenced,
        "malformed_records": malformed,
    }
    report["base"] = {
        "json_files": len(list((base / "json_data").glob("*.json"))),
        "json_bytes": sum(p.stat().st_size for p in (base / "json_data").glob("*.json")),
    }
    return index, malformed


def php_word_contains(short: str, long: str) -> bool:
    return re.search(r"(?<!\S)" + re.escape(short) + r"(?!\S)", long) is not None


def generate(args) -> dict:
    started = time.perf_counter()
    root: Path = args.root.resolve()
    source: Path = args.source.resolve()
    base: Path = args.project.resolve()
    if not root.exists():
        raise RuntimeError(f"The output directory must already exist: {root}")
    if source == root or root in source.parents or source in root.parents:
        raise RuntimeError("Output root must not overlap the read-only source tree.")
    if base == root or root in base.parents or base in root.parents:
        raise RuntimeError("Output root must not overlap the original project.")

    report: dict = {"expected_from_handoff": {"poets": 2012, "poems": 134478}}
    index, malformed = audit_source(source, base, report)
    if malformed:
        report["build_blocked_by_malformed_source"] = True

    era_map_path = root / "era_map.json"
    aliases_path = root / "era_aliases.json"
    glob_path = root / "base_glob.json"
    for required in (era_map_path, aliases_path, glob_path):
        if not required.is_file():
            raise RuntimeError(f"Missing PHP-derived read-only input snapshot: {required}")
    era_map = read_json(era_map_path)
    era_aliases = read_json(aliases_path)
    base_files = [Path(p) for p in read_json(glob_path)]

    for dirname in ("content", "public", ".cache"):
        safe_reset_dir(root / dirname, root)
    db_path = root / ".cache" / "reconstruction.sqlite"
    db = init_db(db_path)
    db.execute("PRAGMA cache_size=-200000")
    poem_ids_by_hash: dict[int, dict[str, int]] = defaultdict(dict)
    poem_hashes_by_poet: dict[int, set[str]] = defaultdict(set)
    exact_poet_by_name: dict[str, int] = {}  # import_diwan.php uses normalized names as keys
    base_poet_by_raw_name: dict[str, int] = {}  # import.php looks up the raw file name
    poet_list: list[tuple[int, str]] = []
    base_skipped_empty_info = 0
    base_skipped_unknown_era = 0
    base_poem_empty = 0

    def era_normalize(value: str) -> str:
        value = value.strip()
        return era_aliases.get(value, value)

    # Recreate import.php using PHP glob() order captured without touching the source.
    for file_path in base_files:
        if not file_path.is_file():
            continue
        name = NAME_SUFFIX.sub("", file_path.stem).strip()
        try:
            data = read_json(file_path)
        except Exception:
            continue
        info = str(data.get("info") or "").strip() if isinstance(data, dict) else ""
        if not info:
            base_skipped_empty_info += 1
            continue
        if name not in era_map:
            base_skipped_unknown_era += 1
            continue
        era = era_normalize(str(era_map[name]))
        if name in base_poet_by_raw_name:
            poet_id = base_poet_by_raw_name[name]
            db.execute("UPDATE poets SET era=?,info=?,normalized_name=? WHERE id=?", (era, info, normalize_name(name), poet_id))
            db.execute("DELETE FROM poems WHERE poet_id=?", (poet_id,))
            poem_ids_by_hash[poet_id].clear()
            poem_hashes_by_poet[poet_id].clear()
        else:
            cur = db.execute("INSERT INTO poets(name,era,info,normalized_name) VALUES(?,?,?,?)", (name, era, info, normalize_name(name)))
            poet_id = int(cur.lastrowid)
            base_poet_by_raw_name[name] = poet_id
            poet_list.append((poet_id, normalize_name(name)))
        exact_poet_by_name[normalize_name(name)] = poet_id
        if isinstance(data, dict):
            for key, value in data.items():
                if not str(key).isdigit() or not isinstance(value, dict) or not str(value.get("poem") or "").strip():
                    continue
                title, poem, meter, rhyme, rhyme_label, verses, ptype, _ = parse_poem_fields(value, False)
                if not poem.strip():
                    base_poem_empty += 1
                    continue
                h = text_key(poem)
                cur = db.execute("""INSERT INTO poems(poet_id,title,text,text_hash,meter,rhyme_letter,rhyme_label,verse_count,poem_type)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (poet_id, title, poem, h, meter, rhyme, rhyme_label, verses, ptype))
                poem_id = int(cur.lastrowid)
                poem_ids_by_hash[poet_id].setdefault(h, poem_id)
                poem_hashes_by_poet[poet_id].add(h)

    base_poets = db.execute("SELECT COUNT(*) FROM poets").fetchone()[0]
    base_poems = db.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    report["base_import_simulation"] = {
        "poets": base_poets, "poems": base_poems,
        "skipped_empty_info": base_skipped_empty_info,
        "skipped_missing_era_mapping": base_skipped_unknown_era,
        "empty_poems": base_poem_empty,
        "id_order_source": "PHP glob() listing captured read-only from original import.php order",
    }

    source_to_db_poet: dict[str, int] = {}
    source_names: dict[str, str] = {}
    source_file_count = 0
    source_poem_rows = 0
    source_empty_texts = 0
    source_unmatched_poets = 0
    source_ambiguous_poets = 0
    added_poets = 0
    merged_poets = 0
    added_poems = 0
    duplicate_poems = 0
    duplicate_hashes_cross_author = 0

    for entry in index.get("poets", []):
        if not isinstance(entry, dict) or not entry.get("has_poems") or not entry.get("file"):
            continue
        file_name = Path(str(entry["file"])).name
        path = source / file_name
        if not path.is_file():
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        poems = data.get("poems") if isinstance(data, dict) else None
        if not isinstance(poems, list) or not poems:
            continue
        source_file_count += 1
        name = str(data.get("name") or entry.get("name") or "").strip()
        source_id = str(data.get("id", entry.get("id", "")))
        source_names[source_id] = name
        norm = normalize_name(name)
        prepared = []
        for pm in poems:
            if not isinstance(pm, dict) or pm.get("id") is None:
                continue
            source_poem_rows += 1
            raw = str(pm.get("text") or "")
            if not raw.strip():
                source_empty_texts += 1
                continue
            fields = parse_poem_fields(pm, True)
            h = text_key(raw)
            prepared.append((str(pm.get("id")), raw, h, fields, str(pm.get("explanation") or "")))
            db.execute("INSERT OR REPLACE INTO source_poems(source_poet_id,source_poem_id,text_hash,explanation) VALUES(?,?,?,?)",
                       (source_id, str(pm.get("id")), h, str(pm.get("explanation") or "")))
        if not prepared:
            continue

        target_id = exact_poet_by_name.get(norm)
        exact_match = target_id is not None
        if target_id is None:
            for candidate_id, candidate_norm in poet_list:
                short = norm if len(norm) <= len(candidate_norm) else candidate_norm
                long = candidate_norm if len(norm) <= len(candidate_norm) else norm
                if len(short) < 4 or len(short) > 12:
                    continue
                if php_word_contains(short, long):
                    target_id = candidate_id
                    break

        if target_id is not None:
            overlap = sum(1 for _, _, h, _, _ in prepared if h in poem_hashes_by_poet[target_id])
            if overlap == 0 and not exact_match:
                target_id = None
            else:
                merged_poets += 1

        if target_id is None:
            description = str(data.get("description") or "").strip()
            era = era_normalize(str(data.get("era") or entry.get("era") or ""))
            info = name + "\n*\n" + description
            cur = db.execute("INSERT INTO poets(name,era,info,normalized_name) VALUES(?,?,?,?)", (name, era, info, norm))
            target_id = int(cur.lastrowid)
            # PHP uses the first normalized name mapping, but appends every new row to poetsList.
            exact_poet_by_name.setdefault(norm, target_id)
            poet_list.append((target_id, norm))
            added_poets += 1

        source_to_db_poet[source_id] = target_id
        for source_poem_id, raw, h, fields, explanation in prepared:
            existing_poem_id = poem_ids_by_hash[target_id].get(h)
            if existing_poem_id is not None:
                duplicate_poems += 1
            else:
                title, formatted, meter, rhyme, rhyme_label, verses, ptype, content_hash = fields
                cur = db.execute("""INSERT INTO poems(poet_id,title,text,text_hash,meter,rhyme_letter,rhyme_label,verse_count,poem_type,content_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (target_id, title, formatted, h, meter, rhyme, rhyme_label, verses, ptype, content_hash))
                existing_poem_id = int(cur.lastrowid)
                poem_ids_by_hash[target_id][h] = existing_poem_id
                poem_hashes_by_poet[target_id].add(h)
                added_poems += 1

    db.commit()
    simulated_poets = int(db.execute("SELECT COUNT(*) FROM poets").fetchone()[0])
    simulated_poems = int(db.execute("SELECT COUNT(*) FROM poems").fetchone()[0])
    report["merge_simulation"] = {
        "source_files_processed": source_file_count,
        "source_poem_records": source_poem_rows,
        "source_poem_texts_empty": source_empty_texts,
        "merged_source_poets": merged_poets,
        "added_source_poets": added_poets,
        "deduplicated_source_poems_within_target_poet": duplicate_poems,
        "added_source_poems": added_poems,
        "simulated_total_poets": simulated_poets,
        "simulated_total_poems": simulated_poems,
        "matches_handoff_counts": simulated_poets == 2012 and simulated_poems == 134478,
        "legacy_ids": "reconstructed by replaying import.php then import_diwan.php insertion order; DB was not accessible to verify",
    }

    # Mirror import_explanations.php's majority-vote mapping from source poet IDs to target DB poet IDs.
    db_poets_by_hash: dict[str, set[int]] = defaultdict(set)
    for hash_value, poet_id in db.execute("SELECT DISTINCT text_hash, poet_id FROM poems"):
        db_poets_by_hash[str(hash_value)].add(int(poet_id))
    votes: dict[str, Counter[int]] = defaultdict(Counter)
    source_rows = db.execute("SELECT source_poet_id, text_hash FROM source_poems")
    for source_id, h in source_rows:
        for poet_id in db_poets_by_hash.get(str(h), ()):
            votes[str(source_id)][poet_id] += 1
    target_norms = {int(pid): str(norm) for pid, norm in db.execute("SELECT id,normalized_name FROM poets")}
    source_to_explanation_poet: dict[str, int] = {}
    for source_id, vote_counts in votes.items():
        ordered = vote_counts.most_common()
        if not ordered:
            continue
        top = ordered[0][1]
        tied = [pid for pid, count in ordered if count == top]
        if len(tied) > 1:
            normalized_source_name = normalize_name(source_names.get(source_id, ""))
            matches = [pid for pid in tied if target_norms.get(pid, "") == normalized_source_name]
            if len(matches) != 1:
                source_ambiguous_poets += 1
                continue
            source_to_explanation_poet[source_id] = matches[0]
        else:
            source_to_explanation_poet[source_id] = tied[0]
    unmatched_explanations = 0
    for source_id, source_poem_id, h, explanation in db.execute(
        "SELECT source_poet_id, source_poem_id, text_hash, explanation FROM source_poems WHERE explanation<>''"
    ):
        target_id = source_to_explanation_poet.get(str(source_id))
        poem_id = poem_ids_by_hash.get(target_id, {}).get(str(h)) if target_id is not None else None
        if poem_id is None:
            unmatched_explanations += 1
            continue
        db.execute("INSERT OR IGNORE INTO explanations(poem_id,explanation) VALUES(?,?)", (poem_id, str(explanation)))
    db.commit()
    explanation_count = int(db.execute("SELECT COUNT(*) FROM explanations").fetchone()[0])
    poems_with_explanations = int(db.execute("SELECT COUNT(DISTINCT poem_id) FROM explanations").fetchone()[0])
    report["explanations"] = {
        "source_poem_records_with_explanation": int(db.execute("SELECT COUNT(*) FROM source_poems WHERE explanation<>''").fetchone()[0]),
        "explanation_rows_linked": explanation_count,
        "poems_with_explanations": poems_with_explanations,
        "unmatched_explanations": unmatched_explanations,
        "ambiguous_source_poets_skipped": source_ambiguous_poets,
    }

    if malformed:
        report["build_blocked_by_malformed_source"] = True
        report_path = root / "source_audit.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        db.close()
        raise RuntimeError(f"Source files were malformed; audit written to {report_path}")

    # Content export starts only after source count and importer replay have been measured.
    content = root / "content"
    report["pages_generated"] = {"poet": 0, "poem": 0, "era": 0}
    report["unmatched_era_values"] = Counter()
    report["duplicate_ids_in_simulated_db"] = 0
    for poet_id, name, era, info in db.execute("SELECT id,name,era,info FROM poets ORDER BY id"):
        if era:
            report["unmatched_era_values"][era] += 1
        poem_links = []
        for pid, poem_title, meter, rhyme in db.execute(
            "SELECT id,title,meter,rhyme_letter FROM poems WHERE poet_id=? ORDER BY id", (poet_id,)
        ):
            label = str(poem_title or "قصيدة بلا عنوان").replace("[", "\\[").replace("]", "\\]")
            details = " · ".join(x for x in (str(meter or ""), str(rhyme or "")) if x)
            suffix = f" — {details}" if details else ""
            poem_links.append(f"- [{label}](/poem/{int(pid)}/){suffix}")
        poet_body = "## القصائد\n\n" + "\n".join(poem_links) if poem_links else "## لا توجد قصائد مسجلة\n"
        write_front_matter(content / "poet" / str(poet_id) / "index.md", {
            "type": "poet", "slug": str(poet_id), "title": name, "poetId": int(poet_id), "era": era,
            "info": info, "poemCount": len(poem_links), "poemCatalogHtml": poet_catalog_html,
        }, "")
        report["pages_generated"]["poet"] += 1

    # Add explanation rows with a streaming merge to avoid holding hundreds of MiB in RAM.
    poet_cache = {
        int(pid): (str(name), str(era))
        for pid, name, era in db.execute("SELECT id,name,era FROM poets")
    }
    poems_cursor = db.execute("""SELECT id,poet_id,title,text,text_hash,meter,rhyme_letter,rhyme_label,verse_count,poem_type
        FROM poems ORDER BY id""")
    exp_cursor = iter(db.execute("SELECT poem_id,explanation FROM explanations ORDER BY poem_id,explanation"))
    pending_exp = next(exp_cursor, None)
    for row in poems_cursor:
        poem_id, poet_id, title, poem_text, h, meter, rhyme, rhyme_label, verses, ptype = row
        exp_parts: list[str] = []
        while pending_exp is not None and int(pending_exp[0]) == int(poem_id):
            exp_parts.append(str(pending_exp[1]))
            pending_exp = next(exp_cursor, None)
        literary, vocabulary, general = ("", "", "")
        if exp_parts:
            # The PHP site shows each unique explanation separately. Keep the first for compact page data
            # while retaining all source rows in the report/intermediate model.
            literary, vocabulary, general = split_explanation(exp_parts[0])
        title_text = str(title or "قصيدة بلا عنوان")
        write_front_matter(content / "poem" / str(poem_id) / "index.md", {
            "type": "poem", "slug": str(poem_id), "title": title_text, "poemId": int(poem_id), "poetId": int(poet_id),
            "poet": poet_cache[int(poet_id)][0], "era": poet_cache[int(poet_id)][1],
            "poemHtml": render_poem_html(str(poem_text)), "meter": str(meter or ""), "rhyme": str(rhyme or ""),
            "rhymeLabel": str(rhyme_label or ""), "verseCount": int(verses) if verses is not None else 0,
            "poemType": int(ptype) if ptype is not None else -1,
            "literary": literary, "vocabulary": vocabulary, "generalExplanation": general,
        })
        report["pages_generated"]["poem"] += 1

    era_order = ["العصر الجاهلي", "المخضرمون", "صدر الإسلام", "الأموي", "العباسي", "الأندلسي", "الأيوبي", "مملوكي", "عثماني", "العصر الحديث"]
    eras_present = {str(row[0]) for row in db.execute("SELECT DISTINCT era FROM poets WHERE era<>''")}
    for era in [*era_order, *sorted(eras_present - set(era_order))]:
        if era not in eras_present:
            continue
        slug = hashlib.sha1(era.encode("utf-8")).hexdigest()[:12]
        poet_links = []
        for poet_id, poet_name in db.execute("SELECT id,name FROM poets WHERE era=? ORDER BY name", (era,)):
            poet_links.append(f"- [{poet_name}](/poet/{int(poet_id)}/)")
        write_front_matter(content / "era" / slug / "index.md", {
            "type": "era", "title": era, "era": era, "slug": slug,
        }, "## الشعراء\n\n" + "\n".join(poet_links))
        report["pages_generated"]["era"] += 1

    write_front_matter(content / "_index.md", {
        "type": "home", "title": "ديوان الشعر العربي", "poetCount": simulated_poets,
        "poemCount": simulated_poems,
    })
    report["unmatched_era_values"] = dict(report["unmatched_era_values"])
    report["export_seconds"] = round(time.perf_counter() - started, 3)
    report["reconstruction_db_bytes"] = db_path.stat().st_size
    report_path = root / "source_audit.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    db.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    for path in (args.project / "json_data", args.source / "_index.json", args.root):
        if not path.exists():
            print(f"Missing required path: {path}", file=sys.stderr)
            return 2
    start = time.perf_counter()
    try:
        report = generate(args)
    except Exception as exc:
        print(f"BUILD PREP FAILED: {exc}", file=sys.stderr)
        return 1
    report["total_python_seconds"] = round(time.perf_counter() - start, 3)
    (args.root / "source_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source": report.get("source"), "base_import_simulation": report.get("base_import_simulation"),
        "merge_simulation": report.get("merge_simulation"), "explanations": report.get("explanations"),
        "pages_generated": report.get("pages_generated"), "total_python_seconds": report.get("total_python_seconds"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
