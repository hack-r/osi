#!/usr/bin/env python3
"""
osi_harvest.py - Build a citation database of everything published by
the Objective Standard Institute (OSI) / The Objective Standard (TOS).

Sources
-------
A. Substack year-sitemaps   https://www.theobjectivestandard.com/sitemap/<YEAR>   (2006-present)
   -> complete URL/title enumeration, immune to API offset limits.
B. Substack archive API     /api/v1/archive?sort=new&offset=N&limit=50
   -> full metadata (date, audience/paywall flag, subtitle byline, wordcount,
      section, tags, canonical_url). Back catalog 2006-present was migrated in,
      so this is the primary metadata spine.
C. WordPress REST API       https://objectivestandard.org/wp-json/wp/v2/<type>
   -> OSI org site: blog posts (~315, 2021-present), podcasts, conferences,
      courses, fellows. Distinct content from TOS; not a duplicate.
D. (optional) legacy WP     https://archive.theobjectivestandard.com/
   -> the pre-Substack TOS site. As of 2026-09 it serves a TLS certificate that
      does not match the hostname, so it fails verification. Enable with
      --legacy-insecure only if you accept that; it is mostly redundant with (B)
      but uniquely holds /issues/ (quarterly TOC pages) and author pages.
E. Issue grouping pass      reads the "<Season Year> Issue of TOS Is Published!"
   announcement posts out of stored raw JSON and stamps `issue_label` onto every
   article they link to.

Outputs
-------
  osi_citations.sqlite   normalized table `items` (+ raw JSON in `raw`)
  osi_citations.csv      flat export
  osi_citations.json     CSL-JSON (drop straight into Zotero / pandoc)
  osi_citations.bib      BibTeX

Usage
-----
  pip install requests
  python osi_harvest.py                      # everything except legacy site
  python osi_harvest.py --only substack
  python osi_harvest.py --only wp
  python osi_harvest.py --legacy-insecure    # also crawl archive.* ignoring TLS
  python osi_harvest.py --export-only        # re-export from existing sqlite

Politeness: one request at a time, 1.0s delay, exponential backoff on 429.
Run it from your own machine; scripted access to these hosts is blocked from
Claude's cloud sandbox by egress policy.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pip install requests")

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

SUBSTACK = "https://www.theobjectivestandard.com"
WPSITE = "https://objectivestandard.org"
LEGACY = "https://archive.theobjectivestandard.com"

SUBSTACK_FIRST_YEAR = 2006
SUBSTACK_LAST_YEAR = datetime.now(timezone.utc).year

WP_TYPES = ["posts", "podcasts", "conferences", "courses", "fellows"]

DB = "osi_citations.sqlite"
DELAY = 1.0
TIMEOUT = 45
MAX_RETRIES = 6
UA = "OSI-citation-harvester/1.0 (personal bibliographic research; contact: you@example.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"})


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

def get(url: str, *, params: Optional[dict] = None, verify: bool = True,
        expect_json: bool = False) -> Optional[Any]:
    """GET with backoff. Returns parsed JSON, or text, or None on hard failure."""
    delay = DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT, verify=verify)
        except requests.RequestException as e:
            print(f"    ! {type(e).__name__}: {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                return None
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code == 200:
            time.sleep(DELAY)
            if expect_json:
                try:
                    return r.json()
                except ValueError:
                    print(f"    ! non-JSON body from {url}", file=sys.stderr)
                    return None
            return r.text

        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", 0)) or delay
            print(f"    . {r.status_code} on {url} - sleeping {wait:.0f}s "
                  f"(attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
            continue

        if r.status_code in (400, 404):
            return None

        print(f"    ! HTTP {r.status_code} {url}", file=sys.stderr)
        return None
    return None


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key           TEXT PRIMARY KEY,     -- source:id
    source        TEXT NOT NULL,        -- substack | wp | legacy
    source_id     TEXT,
    url           TEXT,
    slug          TEXT,
    title         TEXT,
    subtitle      TEXT,
    authors       TEXT,                 -- '; ' joined
    published     TEXT,                 -- ISO8601
    year          INTEGER,
    item_type     TEXT,                 -- newsletter | podcast | video | wp-post | ...
    access        TEXT,                 -- everyone | only_paid | founding | unknown
    section       TEXT,
    tags          TEXT,                 -- '; ' joined
    wordcount     INTEGER,
    issue_label   TEXT,                 -- e.g. 'Fall 2026' when resolvable
    retrieved_at  TEXT
);
CREATE TABLE IF NOT EXISTS raw (
    key   TEXT PRIMARY KEY,
    body  TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_year   ON items(year);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_slug   ON items(slug);
"""

UPSERT = """
INSERT INTO items (key,source,source_id,url,slug,title,subtitle,authors,published,
                   year,item_type,access,section,tags,wordcount,issue_label,retrieved_at)
VALUES (:key,:source,:source_id,:url,:slug,:title,:subtitle,:authors,:published,
        :year,:item_type,:access,:section,:tags,:wordcount,:issue_label,:retrieved_at)
ON CONFLICT(key) DO UPDATE SET
    url=COALESCE(excluded.url, items.url),
    title=COALESCE(excluded.title, items.title),
    subtitle=COALESCE(excluded.subtitle, items.subtitle),
    authors=COALESCE(NULLIF(excluded.authors,''), items.authors),
    published=COALESCE(excluded.published, items.published),
    year=COALESCE(excluded.year, items.year),
    item_type=COALESCE(excluded.item_type, items.item_type),
    access=COALESCE(excluded.access, items.access),
    section=COALESCE(excluded.section, items.section),
    tags=COALESCE(NULLIF(excluded.tags,''), items.tags),
    wordcount=COALESCE(excluded.wordcount, items.wordcount),
    issue_label=COALESCE(excluded.issue_label, items.issue_label),
    retrieved_at=excluded.retrieved_at
"""


def connect(path: str = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(conn: sqlite3.Connection, rec: Dict[str, Any], raw: Optional[Any] = None) -> None:
    rec.setdefault("retrieved_at", now())
    for col in ("source_id", "url", "slug", "title", "subtitle", "authors", "published",
                "year", "item_type", "access", "section", "tags", "wordcount", "issue_label"):
        rec.setdefault(col, None)
    conn.execute(UPSERT, rec)
    if raw is not None:
        conn.execute("INSERT OR REPLACE INTO raw (key, body) VALUES (?,?)",
                     (rec["key"], json.dumps(raw, ensure_ascii=False)))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

BY_RE = re.compile(r"^\s*(?:by|By|BY)[\s:]+(.+?)\s*$")
SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b)\s*")
TAG_RE = re.compile(r"<[^>]+>")
ISSUE_RE = re.compile(
    r"\b(Spring|Summer|Fall|Autumn|Winter)\s+((?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)?\d{2})?)",
    re.I)


def strip_tags(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    return html.unescape(TAG_RE.sub("", s)).strip()


def clip(s: Optional[str], n: int) -> Optional[str]:
    """Truncate, tolerating None from strip_tags on an empty rendered field."""
    return s[:n] if s else None


def authors_from(bylines: Optional[list], subtitle: Optional[str]) -> str:
    """Substack bylines first; migrated posts store 'By X' in the subtitle."""
    names: List[str] = []
    for b in (bylines or []):
        n = (b or {}).get("name")
        if n:
            names.append(n.strip())
    if not names and subtitle:
        m = BY_RE.match(strip_tags(subtitle) or "")
        if m:
            names = [p.strip() for p in SPLIT_RE.split(m.group(1)) if p.strip()]
    return "; ".join(dict.fromkeys(names))


def year_of(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    m = re.match(r"(\d{4})", iso)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1990 <= y <= 2100 else None


def issue_label(*texts: Optional[str]) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        m = ISSUE_RE.search(t)
        if m:
            season = m.group(1).title().replace("Autumn", "Fall")
            return f"{season} {m.group(2).replace(' ', '')}"
    return None


# --------------------------------------------------------------------------- #
# A. Substack year sitemaps  (enumeration / completeness check)
# --------------------------------------------------------------------------- #

SITEMAP_LINK_RE = re.compile(
    r'href="(https://www\.theobjectivestandard\.com/p/[^"#?]+)"[^>]*>(.*?)</a>', re.S)


def harvest_substack_sitemap(conn: sqlite3.Connection) -> int:
    n = 0
    for year in range(SUBSTACK_FIRST_YEAR, SUBSTACK_LAST_YEAR + 1):
        body = get(f"{SUBSTACK}/sitemap/{year}")
        if not body:
            print(f"  sitemap {year}: unavailable")
            continue
        found = SITEMAP_LINK_RE.findall(body)
        seen = set()
        for url, label in found:
            slug = url.rsplit("/p/", 1)[-1]
            if slug in seen:
                continue
            seen.add(slug)
            save(conn, {
                "key": f"substack:{slug}",
                "source": "substack",
                "source_id": slug,
                "url": url,
                "slug": slug,
                "title": strip_tags(label) or None,
                "year": year,
                "item_type": "newsletter",
            })
            n += 1
        conn.commit()
        print(f"  sitemap {year}: {len(seen)} posts")
    return n


# --------------------------------------------------------------------------- #
# B. Substack archive API (metadata spine)
# --------------------------------------------------------------------------- #

def harvest_substack_api(conn: sqlite3.Connection, limit: int = 50) -> int:
    offset, total, empty_streak = 0, 0, 0
    while True:
        data = get(f"{SUBSTACK}/api/v1/archive",
                   params={"sort": "new", "search": "", "offset": offset, "limit": limit},
                   expect_json=True)
        if data is None:
            print(f"  api offset={offset}: request failed, stopping")
            break
        if not isinstance(data, list) or not data:
            empty_streak += 1
            if empty_streak >= 2:
                break
            offset += limit
            continue
        empty_streak = 0
        for p in data:
            slug = p.get("slug") or str(p.get("id"))
            subtitle = p.get("subtitle")
            pub = p.get("post_date")
            tags = "; ".join(filter(None, (t.get("name") for t in (p.get("postTags") or []))))
            rec = {
                "key": f"substack:{slug}",
                "source": "substack",
                "source_id": str(p.get("id") or slug),
                "url": p.get("canonical_url") or f"{SUBSTACK}/p/{slug}",
                "slug": slug,
                "title": strip_tags(p.get("title")),
                "subtitle": strip_tags(subtitle),
                "authors": authors_from(p.get("publishedBylines"), subtitle),
                "published": pub,
                "year": year_of(pub),
                "item_type": p.get("type"),
                "access": p.get("audience") or "unknown",
                "section": p.get("section_name"),
                "tags": tags,
                "wordcount": p.get("wordcount"),
                "issue_label": issue_label(p.get("title"), subtitle),
            }
            save(conn, rec, raw=p)
            total += 1
        conn.commit()
        print(f"  api offset={offset}: +{len(data)} (running {total})")
        offset += len(data)
    return total


# --------------------------------------------------------------------------- #
# C. objectivestandard.org WordPress REST
# --------------------------------------------------------------------------- #

def wp_author_map() -> Dict[int, str]:
    out: Dict[int, str] = {}
    page = 1
    while True:
        data = get(f"{WPSITE}/wp-json/wp/v2/users",
                   params={"per_page": 100, "page": page}, expect_json=True)
        if not isinstance(data, list) or not data:
            break
        for u in data:
            out[u.get("id")] = u.get("name") or ""
        if len(data) < 100:
            break
        page += 1
    return out


def harvest_wp(conn: sqlite3.Connection) -> int:
    authors = wp_author_map()
    total = 0
    for ptype in WP_TYPES:
        page, got = 1, 0
        while True:
            data = get(f"{WPSITE}/wp-json/wp/v2/{ptype}",
                       params={"per_page": 100, "page": page}, expect_json=True)
            if not isinstance(data, list) or not data:
                break
            for p in data:
                pid = p.get("id")
                pub = p.get("date_gmt") or p.get("date")
                title = strip_tags((p.get("title") or {}).get("rendered"))
                rec = {
                    "key": f"wp:{ptype}:{pid}",
                    "source": "wp",
                    "source_id": str(pid),
                    "url": p.get("link"),
                    "slug": p.get("slug"),
                    "title": title,
                    "subtitle": clip(strip_tags((p.get("excerpt") or {}).get("rendered")), 400),
                    "authors": authors.get(p.get("author"), ""),
                    "published": pub,
                    "year": year_of(pub),
                    "item_type": f"wp-{ptype[:-1] if ptype.endswith('s') else ptype}",
                    "access": "everyone",
                    "section": ptype,
                    "wordcount": None,
                    "issue_label": issue_label(title),
                }
                save(conn, rec, raw=p)
                got += 1
                total += 1
            conn.commit()
            if len(data) < 100:
                break
            page += 1
        print(f"  wp/{ptype}: {got}")
    return total


# --------------------------------------------------------------------------- #
# D. legacy archive.theobjectivestandard.com (optional, TLS hostname mismatch)
# --------------------------------------------------------------------------- #

LEGACY_ITEM_RE = re.compile(
    r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def harvest_legacy(conn: sqlite3.Connection, max_pages: int = 400) -> int:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print("  NOTE: archive.theobjectivestandard.com presents a certificate that does not "
          "match its hostname; verification is disabled for this source only.")
    total = 0
    for page in range(1, max_pages + 1):
        url = f"{LEGACY}/archive/" if page == 1 else f"{LEGACY}/archive/page/{page}/"
        body = get(url, verify=False)
        if not body:
            break
        items = LEGACY_ITEM_RE.findall(body)
        if not items:
            break
        for link, label in items:
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            save(conn, {
                "key": f"legacy:{slug}",
                "source": "legacy",
                "source_id": slug,
                "url": link,
                "slug": slug,
                "title": strip_tags(label),
                "item_type": "article",
                "access": "unknown",
            })
            total += 1
        conn.commit()
        print(f"  legacy page {page}: +{len(items)} (running {total})")
    return total


# --------------------------------------------------------------------------- #
# E. map articles to their quarterly issue via the "Issue of TOS Is Published!"
#    announcement posts, which carry the issue's table of contents.
# --------------------------------------------------------------------------- #

ANN_LINK_RE = re.compile(
    r'href="(?:https://www\.theobjectivestandard\.com)?/p/([A-Za-z0-9\-_]+)"')


def link_issues(conn: sqlite3.Connection) -> int:
    """Read announcement posts' body_html out of `raw` and stamp issue_label
    onto every article they link to. Safe to re-run."""
    conn.row_factory = sqlite3.Row
    anns = conn.execute("""
        SELECT i.key, i.slug, i.title, r.body
        FROM items i JOIN raw r ON r.key = i.key
        WHERE i.source='substack'
          AND (i.title LIKE '%Issue of%' OR i.slug LIKE '%-issue-of-%')
          AND (i.title LIKE '%Published%' OR i.title LIKE '%Released%')
    """).fetchall()
    stamped = 0
    for a in anns:
        label = issue_label(a["title"])
        if not label:
            continue
        try:
            body = json.loads(a["body"]) or {}
        except (TypeError, ValueError):
            continue
        html_body = body.get("body_html") or body.get("truncated_body_text") or ""
        for slug in dict.fromkeys(ANN_LINK_RE.findall(html_body)):
            if slug == a["slug"]:
                continue
            cur = conn.execute(
                "UPDATE items SET issue_label=? WHERE key=? AND (issue_label IS NULL)",
                (label, f"substack:{slug}"))
            stamped += cur.rowcount
        conn.commit()
    print(f"  issue announcements read: {len(anns)}; articles stamped: {stamped}")
    return stamped


# --------------------------------------------------------------------------- #
# exports
# --------------------------------------------------------------------------- #

COLUMNS = ["key", "source", "source_id", "url", "slug", "title", "subtitle", "authors",
           "published", "year", "item_type", "access", "section", "tags", "wordcount",
           "issue_label", "retrieved_at"]


def rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"SELECT {','.join(COLUMNS)} FROM items "
        "ORDER BY (published IS NULL), published DESC, title"
    ).fetchall()


def export_csv(conn: sqlite3.Connection, path="osi_citations.csv") -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows(conn):
            w.writerow({k: r[k] for k in COLUMNS})
    print(f"  wrote {path}")


def _csl_authors(s: Optional[str]) -> List[dict]:
    out = []
    for name in (s or "").split(";"):
        name = name.strip()
        if not name:
            continue
        parts = name.split()
        if len(parts) == 1:
            out.append({"literal": name})
        else:
            out.append({"given": " ".join(parts[:-1]), "family": parts[-1]})
    return out


def export_csl(conn: sqlite3.Connection, path="osi_citations.json") -> None:
    items = []
    for r in rows(conn):
        date_parts: List[List[int]] = []
        if r["published"]:
            bits = re.match(r"(\d{4})-(\d{2})-(\d{2})", r["published"])
            if bits:
                date_parts = [[int(x) for x in bits.groups()]]
            elif r["year"]:
                date_parts = [[r["year"]]]
        elif r["year"]:
            date_parts = [[r["year"]]]
        items.append({
            "id": r["key"],
            "type": "article-magazine" if r["source"] != "wp" else "post-weblog",
            "title": r["title"],
            "author": _csl_authors(r["authors"]),
            "container-title": "The Objective Standard" if r["source"] in ("substack", "legacy")
                               else "Objective Standard Institute",
            "issue": r["issue_label"] or None,
            "URL": r["url"],
            "issued": {"date-parts": date_parts} if date_parts else None,
            "accessed": {"raw": (r["retrieved_at"] or "")[:10]},
            "note": f"access={r['access'] or 'unknown'}"
                    + (f"; tags={r['tags']}" if r["tags"] else ""),
        })
    items = [{k: v for k, v in it.items() if v} for it in items]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    print(f"  wrote {path}")


def _bibkey(r: sqlite3.Row, used: set) -> str:
    first = (r["authors"] or "").split(";")[0].strip()
    last = first.split()[-1] if first.split() else "anon"
    base = re.sub(r"[^A-Za-z0-9]", "", last) or "anon"
    base = f"{base}{r['year'] or 'nd'}"
    key, i = base, 1
    while key in used:
        i += 1
        key = f"{base}{chr(96 + i)}"
    used.add(key)
    return key


def export_bibtex(conn: sqlite3.Connection, path="osi_citations.bib") -> None:
    used: set = set()
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows(conn):
            k = _bibkey(r, used)
            title = (r["title"] or "").replace("{", "").replace("}", "")
            auth = " and ".join(a.strip() for a in (r["authors"] or "").split(";") if a.strip())
            journal = ("The Objective Standard" if r["source"] in ("substack", "legacy")
                       else "Objective Standard Institute")
            fh.write(f"@article{{{k},\n")
            fh.write(f"  title   = {{{{{title}}}}},\n")
            if auth:
                fh.write(f"  author  = {{{auth}}},\n")
            fh.write(f"  journal = {{{journal}}},\n")
            if r["year"]:
                fh.write(f"  year    = {{{r['year']}}},\n")
            if r["published"]:
                fh.write(f"  date    = {{{r['published'][:10]}}},\n")
            if r["issue_label"]:
                fh.write(f"  issue   = {{{r['issue_label']}}},\n")
            if r["url"]:
                fh.write(f"  url     = {{{r['url']}}},\n")
            fh.write(f"  urldate = {{{(r['retrieved_at'] or '')[:10]}}},\n")
            fh.write(f"  note    = {{access: {r['access'] or 'unknown'}}}\n}}\n\n")
    print(f"  wrote {path}")


def summarize(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    print("\n--- summary ---")
    for r in conn.execute("SELECT source, COUNT(*) n FROM items GROUP BY source ORDER BY n DESC"):
        print(f"  {str(r['source']):<10} {r['n']}")
    for r in conn.execute("SELECT access, COUNT(*) n FROM items GROUP BY access ORDER BY n DESC"):
        print(f"  access={str(r['access'] or 'unknown'):<12} {r['n']}")
    r = conn.execute("SELECT MIN(published) a, MAX(published) b FROM items "
                     "WHERE published IS NOT NULL").fetchone()
    print(f"  date range {(r['a'] or '?')[:10]} .. {(r['b'] or '?')[:10]}")
    r = conn.execute("SELECT COUNT(*) n FROM items WHERE authors IS NULL OR authors=''").fetchone()
    print(f"  missing author: {r['n']}")
    r = conn.execute("SELECT COUNT(*) n FROM items WHERE published IS NULL").fetchone()
    print(f"  missing date (sitemap-only, not yet matched by API): {r['n']}")
    print(f"  TOTAL {conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]}")


# --------------------------------------------------------------------------- #

def main() -> None:
    global DELAY
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--only", choices=["sitemap", "api", "substack", "wp", "legacy"],
                    action="append", help="restrict to these sources (repeatable)")
    ap.add_argument("--legacy-insecure", action="store_true",
                    help="also crawl archive.theobjectivestandard.com with TLS verification off")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--delay", type=float, default=DELAY)
    args = ap.parse_args()
    DELAY = args.delay

    conn = connect(args.db)
    want = set(args.only or ["substack", "wp"])
    if "substack" in want:
        want |= {"sitemap", "api"}

    if not args.export_only:
        if "sitemap" in want:
            print("[A] Substack year sitemaps")
            harvest_substack_sitemap(conn)
        if "api" in want:
            print("[B] Substack archive API")
            harvest_substack_api(conn)
        if "wp" in want:
            print("[C] objectivestandard.org WordPress REST")
            harvest_wp(conn)
        if args.legacy_insecure or "legacy" in want:
            print("[D] legacy archive.theobjectivestandard.com")
            harvest_legacy(conn)
        if "api" in want:
            print("[E] mapping articles to quarterly issues")
            link_issues(conn)

    print("\n[export]")
    export_csv(conn)
    export_csl(conn)
    export_bibtex(conn)
    summarize(conn)
    conn.close()


if __name__ == "__main__":
    main()
