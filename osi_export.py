"""
osi_export.py - export the harvested database to the formats we actually
consume: a flat CSV, CSL-JSON for Zotero, BibTeX, and SQL for Cloudflare D1.

Zotero
------
`osi_zotero.csl.json` is CSL-JSON, which Zotero imports directly
(File > Import). Every field Zotero can hold is populated; everything with no
CSL equivalent (access level, word count, item key, and the path + checksum of
the extracted .txt) goes into `note`, which Zotero shows as the item's Extra
field, one `Label: value` per line. CSL-JSON cannot carry file attachments, so
the .txt files are linked by path in Extra rather than embedded - attach them in
bulk afterwards if you want them inside the library, or leave them on disk for
mining.

Cloudflare D1
-------------
D1 speaks SQLite, so the export is plain SQL:

    d1_schema.sql   CREATE TABLE/INDEX, safe to re-run
    d1_data.sql     INSERT OR REPLACE, batched, safe to re-run

Three tables rather than one, because `authors` and `tags` as '; '-joined
strings are miserable to query from Workers:

    items    one row per article
    authors  (item_key, position, name)
    tags     (item_key, tag)
    texts    (item_key, body) - only with --d1-include-text

Article bodies are left out by default: the corpus is tens of megabytes and D1
is a poor place to keep it when the .txt files exist. Include it only if you
intend to query text from a Worker.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

COLUMNS = ["key", "source", "source_id", "url", "slug", "title", "subtitle", "authors",
           "published", "year", "item_type", "access", "section", "tags", "wordcount",
           "issue_label", "retrieved_at", "text_path", "text_status", "text_strategy",
           "text_sha256", "text_words", "text_chars"]


def _available(conn: sqlite3.Connection) -> List[str]:
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    return [c for c in COLUMNS if c in have]


def rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cols = _available(conn)
    return conn.execute(
        f"SELECT {','.join(cols)} FROM items "
        "ORDER BY (published IS NULL), published DESC, title"
    ).fetchall()


def _get(r: sqlite3.Row, key: str) -> Any:
    try:
        return r[key]
    except (IndexError, KeyError):
        return None


def publication_of(source: Optional[str]) -> str:
    return ("The Objective Standard" if source in ("substack", "legacy")
            else "Objective Standard Institute")


def split_names(s: Optional[str]) -> List[str]:
    return [a.strip() for a in (s or "").split(";") if a.strip()]


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def export_csv(conn: sqlite3.Connection, path: str = "osi_citations.csv") -> None:
    cols = _available(conn)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows(conn):
            w.writerow({k: r[k] for k in cols})
    print(f"  wrote {path}")


# --------------------------------------------------------------------------- #
# CSL-JSON / Zotero
# --------------------------------------------------------------------------- #

def _csl_authors(s: Optional[str]) -> List[dict]:
    out = []
    for name in split_names(s):
        parts = name.split()
        if len(parts) == 1:
            out.append({"literal": name})
        else:
            out.append({"given": " ".join(parts[:-1]), "family": parts[-1]})
    return out


def _date_parts(published: Optional[str], year: Optional[int]) -> List[List[int]]:
    if published:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published)
        if m:
            return [[int(x) for x in m.groups()]]
    return [[year]] if year else []


def _extra_note(r: sqlite3.Row) -> str:
    """Zotero surfaces CSL `note` as the Extra field, parsing `Label: value`
    lines. Everything CSL has no home for goes here."""
    bits: List[str] = []
    if _get(r, "access"):
        bits.append(f"access: {r['access']}")
    if _get(r, "item_type"):
        bits.append(f"item type: {r['item_type']}")
    if _get(r, "wordcount"):
        bits.append(f"source wordcount: {r['wordcount']}")
    if _get(r, "text_path"):
        bits.append(f"fulltext file: {r['text_path']}")
        bits.append(f"fulltext status: {_get(r, 'text_status') or 'unknown'}")
        if _get(r, "text_words"):
            bits.append(f"fulltext words: {r['text_words']}")
        if _get(r, "text_sha256"):
            bits.append(f"fulltext sha256: {r['text_sha256']}")
    bits.append(f"item key: {r['key']}")
    return "\n".join(bits)


def export_csl(conn: sqlite3.Connection, path: str = "osi_zotero.csl.json") -> None:
    items = []
    for r in rows(conn):
        dp = _date_parts(_get(r, "published"), _get(r, "year"))
        accessed = _date_parts((_get(r, "retrieved_at") or "")[:10], None)
        item: Dict[str, Any] = {
            "id": r["key"],
            "type": "post-weblog" if r["source"] == "wp" else "article-magazine",
            "title": _get(r, "title"),
            "author": _csl_authors(_get(r, "authors")),
            "container-title": publication_of(r["source"]),
            "issue": _get(r, "issue_label"),
            "section": _get(r, "section"),
            "abstract": _get(r, "subtitle"),
            "keyword": ", ".join(t.strip() for t in (_get(r, "tags") or "").split(";") if t.strip()),
            "URL": _get(r, "url"),
            "language": "en",
            "issued": {"date-parts": dp} if dp else None,
            "accessed": {"date-parts": accessed} if accessed else None,
            "note": _extra_note(r),
        }
        items.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    print(f"  wrote {path} ({len(items)} items; Zotero: File > Import)")


# --------------------------------------------------------------------------- #
# BibTeX
# --------------------------------------------------------------------------- #

def _bibkey(r: sqlite3.Row, used: set) -> str:
    names = split_names(_get(r, "authors"))
    first = names[0] if names else ""
    last = first.split()[-1] if first.split() else "anon"
    base = f"{re.sub(r'[^A-Za-z0-9]', '', last) or 'anon'}{_get(r, 'year') or 'nd'}"
    key, i = base, 1
    while key in used:
        i += 1
        key = f"{base}{chr(96 + i)}"
    used.add(key)
    return key


def export_bibtex(conn: sqlite3.Connection, path: str = "osi_citations.bib") -> None:
    used: set = set()
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows(conn):
            k = _bibkey(r, used)
            title = (_get(r, "title") or "").replace("{", "").replace("}", "")
            auth = " and ".join(split_names(_get(r, "authors")))
            fh.write(f"@article{{{k},\n")
            fh.write(f"  title   = {{{{{title}}}}},\n")
            if auth:
                fh.write(f"  author  = {{{auth}}},\n")
            fh.write(f"  journal = {{{publication_of(r['source'])}}},\n")
            if _get(r, "year"):
                fh.write(f"  year    = {{{r['year']}}},\n")
            if _get(r, "published"):
                fh.write(f"  date    = {{{r['published'][:10]}}},\n")
            if _get(r, "issue_label"):
                fh.write(f"  issue   = {{{r['issue_label']}}},\n")
            if _get(r, "url"):
                fh.write(f"  url     = {{{r['url']}}},\n")
            fh.write(f"  urldate = {{{(_get(r, 'retrieved_at') or '')[:10]}}},\n")
            note = f"access: {_get(r, 'access') or 'unknown'}"
            if _get(r, "text_path"):
                note += f"; fulltext: {r['text_path']}"
            fh.write(f"  note    = {{{note}}}\n}}\n\n")
    print(f"  wrote {path}")


# --------------------------------------------------------------------------- #
# Cloudflare D1
# --------------------------------------------------------------------------- #

D1_SCHEMA = """-- Cloudflare D1 schema for the OSI/TOS citation database.
-- Apply with:  wrangler d1 execute <DB> --remote --file=d1_schema.sql
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS items (
    key           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT,
    url           TEXT,
    slug          TEXT,
    title         TEXT,
    subtitle      TEXT,
    authors       TEXT,
    published     TEXT,
    year          INTEGER,
    item_type     TEXT,
    access        TEXT,
    section       TEXT,
    tags          TEXT,
    wordcount     INTEGER,
    issue_label   TEXT,
    retrieved_at  TEXT,
    text_path     TEXT,
    text_status   TEXT,
    text_strategy TEXT,
    text_sha256   TEXT,
    text_words    INTEGER,
    text_chars    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_year    ON items(year);
CREATE INDEX IF NOT EXISTS idx_items_source  ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_issue   ON items(issue_label);
CREATE INDEX IF NOT EXISTS idx_items_access  ON items(access);

CREATE TABLE IF NOT EXISTS authors (
    item_key  TEXT NOT NULL,
    position  INTEGER NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (item_key, position)
);
CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name);

CREATE TABLE IF NOT EXISTS tags (
    item_key  TEXT NOT NULL,
    tag       TEXT NOT NULL,
    PRIMARY KEY (item_key, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS texts (
    item_key  TEXT PRIMARY KEY,
    words     INTEGER,
    sha256    TEXT,
    body      TEXT
);
"""

# Rows per INSERT statement. Kept small so no single statement approaches D1's
# per-query size limit, and so a rejected batch is easy to locate.
D1_BATCH = 50


def _sql(v: Any) -> str:
    if v is None or v == "":
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''").replace("\x00", "") + "'"


def _insert_batches(fh, table: str, cols: List[str], tuples: List[tuple]) -> None:
    if not tuples:
        return
    collist = ",".join(cols)
    for i in range(0, len(tuples), D1_BATCH):
        chunk = tuples[i:i + D1_BATCH]
        values = ",\n  ".join("(" + ",".join(_sql(v) for v in t) + ")" for t in chunk)
        fh.write(f"INSERT OR REPLACE INTO {table} ({collist}) VALUES\n  {values};\n")
    fh.write("\n")


def export_d1(conn: sqlite3.Connection, schema_path: str = "d1_schema.sql",
              data_path: str = "d1_data.sql", include_text: bool = False,
              text_dir: str = "texts") -> None:
    with open(schema_path, "w", encoding="utf-8") as fh:
        fh.write(D1_SCHEMA)
    print(f"  wrote {schema_path}")

    item_cols = [c for c in COLUMNS]
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    item_cols = [c for c in item_cols if c in have]

    item_tuples: List[tuple] = []
    author_tuples: List[tuple] = []
    tag_tuples: List[tuple] = []
    text_tuples: List[tuple] = []
    seen_tags: set = set()

    for r in rows(conn):
        item_tuples.append(tuple(r[c] for c in item_cols))
        for pos, name in enumerate(split_names(_get(r, "authors")), 1):
            author_tuples.append((r["key"], pos, name))
        for tag in (t.strip() for t in (_get(r, "tags") or "").split(";")):
            if tag and (r["key"], tag) not in seen_tags:
                seen_tags.add((r["key"], tag))
                tag_tuples.append((r["key"], tag))
        if include_text and _get(r, "text_path"):
            body = _read_body(r["text_path"])
            if body:
                text_tuples.append((r["key"], len(body.split()),
                                    _get(r, "text_sha256"), body))

    with open(data_path, "w", encoding="utf-8") as fh:
        fh.write("-- OSI/TOS citation data for Cloudflare D1.\n")
        fh.write("-- Apply AFTER d1_schema.sql:\n")
        fh.write(f"--   wrangler d1 execute <DB> --remote --file={os.path.basename(data_path)}\n")
        fh.write("-- Idempotent (INSERT OR REPLACE); re-run to refresh.\n\n")
        _insert_batches(fh, "items", item_cols, item_tuples)
        _insert_batches(fh, "authors", ["item_key", "position", "name"], author_tuples)
        _insert_batches(fh, "tags", ["item_key", "tag"], tag_tuples)
        if include_text:
            _insert_batches(fh, "texts", ["item_key", "words", "sha256", "body"],
                            text_tuples)
    size = os.path.getsize(data_path)
    print(f"  wrote {data_path} ({len(item_tuples)} items, "
          f"{len(author_tuples)} author rows, {len(tag_tuples)} tag rows, "
          f"{len(text_tuples)} bodies, {size / 1e6:.1f} MB)")
    if not include_text:
        print("    (bodies omitted; pass --d1-include-text to embed them)")


HEADER_RULE = "=" * 78


def _read_body(path: str) -> str:
    """Read a written .txt and return only the article text, dropping the
    metadata header that sits between the two rule lines."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return ""
    parts = content.split(HEADER_RULE)
    return (parts[2] if len(parts) >= 3 else content).strip()


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #

SUSPECT_MIN_YEAR = 2006     # TOS's first issue; nothing predates it


def audit_dates(conn: sqlite3.Connection, min_year: int = SUSPECT_MIN_YEAR,
                max_year: Optional[int] = None) -> int:
    """Report every stored date that cannot be a publication date.

    Answers the question a single visible outlier raises: is it one row or
    many? Run against an existing database, it needs no network.
    """
    from datetime import datetime, timezone
    max_year = max_year or datetime.now(timezone.utc).year + 1
    conn.row_factory = sqlite3.Row
    print(f"\n--- date audit (plausible range {min_year}-{max_year}) ---")

    bad_str = conn.execute(
        "SELECT key, source, url, published, year FROM items "
        "WHERE published IS NOT NULL AND ("
        "  CAST(substr(published,1,4) AS INTEGER) < ? OR"
        "  CAST(substr(published,1,4) AS INTEGER) > ? OR"
        "  substr(published,1,4) NOT GLOB '[0-9][0-9][0-9][0-9]')",
        (min_year, max_year)).fetchall()
    bad_year = conn.execute(
        "SELECT key, source, url, published, year FROM items "
        "WHERE year IS NOT NULL AND (year < ? OR year > ?)",
        (min_year, max_year)).fetchall()
    mismatch = conn.execute(
        "SELECT key, url, published, year FROM items "
        "WHERE published IS NOT NULL AND year IS NOT NULL "
        "  AND CAST(substr(published,1,4) AS INTEGER) <> year").fetchall()
    orphan = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE published IS NOT NULL AND year IS NULL"
    ).fetchone()["n"]

    for label, rowset in (("implausible published", bad_str),
                          ("implausible year", bad_year),
                          ("year disagrees with published", mismatch)):
        print(f"  {label}: {len(rowset)}")
        for r in rowset[:20]:
            print(f"      {r['published']!r:28} year={r['year']!s:6} {r['key']}")
        if len(rowset) > 20:
            print(f"      ... and {len(rowset) - 20} more")
    print(f"  published set but year NULL (the old asymmetry): {orphan}")

    # Distinct rows, not the sum of category counts: one row can fail several
    # checks (an implausible `published` usually implies an implausible `year`),
    # and reporting it twice overstates the damage.
    affected = {r["key"] for r in bad_str} | {r["key"] for r in bad_year} \
        | {r["key"] for r in mismatch}
    orphan_keys = {r[0] for r in conn.execute(
        "SELECT key FROM items WHERE published IS NOT NULL AND year IS NULL")}
    total = len(affected | orphan_keys)
    if total == 0:
        print("  clean - no date anomalies")
    else:
        print(f"  {total} distinct row(s) need attention. Re-run the affected pass\n"
              "  with the current code, or clear them in place:\n"
              "    UPDATE items SET published=NULL, year=NULL WHERE <key IN ...>;")
    return total


def summarize(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    print("\n--- summary ---")
    for r in conn.execute("SELECT source, COUNT(*) n FROM items GROUP BY source ORDER BY n DESC"):
        print(f"  {str(r['source']):<10} {r['n']}")
    for r in conn.execute("SELECT access, COUNT(*) n FROM items GROUP BY access ORDER BY n DESC"):
        print(f"  access={str(r['access'] or 'unknown'):<14} {r['n']}")
    if "text_status" in have:
        for r in conn.execute("SELECT COALESCE(text_status,'not-fetched') s, COUNT(*) n "
                              "FROM items GROUP BY s ORDER BY n DESC"):
            print(f"  text={str(r['s']):<19} {r['n']}")
        r = conn.execute("SELECT SUM(text_words) w FROM items").fetchone()
        if r["w"]:
            print(f"  corpus words: {r['w']:,}")
    r = conn.execute("SELECT MIN(published) a, MAX(published) b FROM items "
                     "WHERE published IS NOT NULL").fetchone()
    print(f"  date range {(r['a'] or '?')[:10]} .. {(r['b'] or '?')[:10]}")
    bad = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE published IS NOT NULL AND ("
        "  CAST(substr(published,1,4) AS INTEGER) < ? OR"
        "  CAST(substr(published,1,4) AS INTEGER) > ?)",
        (SUSPECT_MIN_YEAR, datetime.now(timezone.utc).year + 1)).fetchone()["n"]
    if bad:
        print(f"  !! {bad} implausible date(s) stored - run --audit-dates")
    r = conn.execute("SELECT COUNT(*) n FROM items WHERE authors IS NULL OR authors=''").fetchone()
    print(f"  missing author: {r['n']}")
    r = conn.execute("SELECT COUNT(*) n FROM items WHERE published IS NULL").fetchone()
    print(f"  missing date (sitemap-only, not yet matched by API): {r['n']}")
    print(f"  TOTAL {conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]}")
