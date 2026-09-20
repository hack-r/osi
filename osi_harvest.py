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
F. Full text                fetches each article body and writes one
   self-describing .txt per article for text mining (see osi_text.py).

Outputs
-------
  osi_citations.sqlite   normalized `items` (+ raw JSON in `raw`)
  osi_citations.csv      flat export
  osi_zotero.csl.json    CSL-JSON - Zotero: File > Import
  osi_citations.bib      BibTeX
  texts/*.txt            one file per article, metadata header + body
  text_manifest.csv      index of the text corpus
  d1_schema.sql          Cloudflare D1 schema
  d1_data.sql            Cloudflare D1 data (items/authors/tags[/texts])

Usage
-----
  pip install requests

  python osi_harvest.py          # everything: asks once, then all passes and
                                 # all exports. Answer no and it asks about
                                 # each pass instead.
  python osi_harvest.py --all    # everything, no questions (for cron/CI)

Everything below is for partial and repeat runs:

  python osi_harvest.py --only substack        # TOS only
  python osi_harvest.py --only wp              # objectivestandard.org only
  python osi_harvest.py --fulltext-only        # pass F alone, resumable
  python osi_harvest.py --fulltext-only --text-limit 20    # trial run
  python osi_harvest.py --export-only          # no network, re-export
  python osi_harvest.py --export-only --d1-include-text    # bodies into D1
  python osi_harvest.py --legacy-insecure      # also crawl archive.* (TLS off)

Re-running is safe and cheap: metadata upserts never overwrite a populated
field with NULL, and pass F skips articles whose text is already on disk.

Politeness: one request at a time, 1.0s delay, exponential backoff on 429.
Run it from your own machine; scripted access to these hosts is blocked from
Claude's cloud sandbox by egress policy.
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import os
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

import osi_export
import osi_text

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

# Cookies are attached to this host only, never sent to objectivestandard.org.
SUBSTACK_COOKIE_DOMAIN = ".theobjectivestandard.com"


def load_cookies(cookie_file: Optional[str] = None,
                 cookie_str: Optional[str] = None) -> str:
    """Attach a logged-in Substack session to SESSION.

    Metadata needs no authentication, but article *bodies* for posts marked
    `only_paid` - which is most of the pre-2024 back catalogue - are withheld
    from anonymous clients. A subscriber session is the only way pass F gets
    those bodies rather than previews. A free account does not unlock them.

    Two ways in, neither of which asks for a password:
      --cookie-file  a Netscape cookies.txt exported from your browser
      --cookie       a raw 'name=value; name2=value2' string

    Cookies from --cookie are pinned to the Substack domain so a subscriber
    session can never be sent to the other host.
    """
    cookie_file = cookie_file or os.environ.get("OSI_COOKIE_FILE")
    cookie_str = cookie_str or os.environ.get("OSI_COOKIE")
    notes = []

    if cookie_file:
        path = os.path.expanduser(cookie_file)
        jar = http.cookiejar.MozillaCookieJar(path)
        try:
            # ignore_discard/ignore_expires so session cookies in an export survive
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            print(f"  ! could not read {path}: {exc}", file=sys.stderr)
        else:
            SESSION.cookies.update(jar)
            notes.append(f"{len(jar)} cookies from {os.path.basename(path)}")

    if cookie_str:
        n = 0
        for chunk in cookie_str.split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            SESSION.cookies.set(name.strip(), value.strip(),
                                domain=SUBSTACK_COOKIE_DOMAIN, path="/")
            n += 1
        notes.append(f"{n} cookies from --cookie")

    return ", ".join(notes)


def have_substack_cookies() -> bool:
    return any("theobjectivestandard" in (c.domain or "") for c in SESSION.cookies)


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
    retrieved_at  TEXT,
    text_path     TEXT,                 -- pass F: path to the written .txt
    text_status   TEXT,                 -- ok | paywalled-preview | unavailable
    text_strategy TEXT,                 -- which body source worked
    text_sha256   TEXT,
    text_words    INTEGER,
    text_chars    INTEGER
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

# --------------------------------------------------------------------------- #
# the simple paths: run everything, or be asked
# --------------------------------------------------------------------------- #

FULL_RUN_BLURB = """\
This will, in one pass:
  1. enumerate every TOS article from the year sitemaps, 2006 to now
  2. pull full metadata for all of them from the archive API
  3. pull objectivestandard.org's own posts, podcasts, conferences, courses
  4. group articles into their quarterly issues
  5. download each article body and write one .txt per article
  6. export CSV, Zotero CSL-JSON, BibTeX, and Cloudflare D1 SQL

Roughly 2,700 articles. Expect 60-90 minutes at the default 1s delay, and
about 100-200 MB on disk including the text corpus.
"""


def warn_if_anonymous() -> None:
    """Say plainly, before the long pass, what anonymous access cannot get."""
    if have_substack_cookies():
        print("  subscriber cookies present - paywalled bodies should be readable")
        return
    print("  NOTE: no subscriber cookies. Metadata is unaffected, but article\n"
          "  BODIES for posts marked only_paid - most of the pre-2024 back\n"
          "  catalogue - will come back as previews and be recorded as\n"
          "  text_status='paywalled-preview'. A free account does not unlock\n"
          "  them. To get them later: export cookies.txt from a logged-in\n"
          "  subscriber browser session and re-run:\n"
          "    python osi_harvest.py --fulltext-only --cookie-file cookies.txt")


def run_everything(conn: sqlite3.Connection, *, text_dir: str = osi_text.TEXT_DIR,
                   legacy: bool = False, d1_text: bool = False,
                   text_limit: Optional[int] = None) -> None:
    """Every pass, then every export. This is what a bare invocation does."""
    print("[A] Substack year sitemaps")
    harvest_substack_sitemap(conn)
    print("[B] Substack archive API")
    harvest_substack_api(conn)
    print("[C] objectivestandard.org WordPress REST")
    harvest_wp(conn)
    if legacy:
        print("[D] legacy archive.theobjectivestandard.com")
        harvest_legacy(conn)
    print("[E] mapping articles to quarterly issues")
    link_issues(conn)
    print("[F] full text")
    warn_if_anonymous()
    osi_text.harvest_text(conn, get, substack_base=SUBSTACK, text_dir=text_dir,
                          limit=text_limit,
                          retry_previews=have_substack_cookies())
    osi_text.write_manifest(conn)
    print("\n[export]")
    export_all(conn, d1_text=d1_text, text_dir=text_dir)


def export_all(conn: sqlite3.Connection, *, d1_text: bool = False,
               text_dir: str = osi_text.TEXT_DIR) -> None:
    osi_export.export_csv(conn)
    osi_export.export_csl(conn)
    osi_export.export_bibtex(conn)
    osi_export.export_d1(conn, include_text=d1_text, text_dir=text_dir)


def _ask(prompt: str, default: str = "y") -> bool:
    suffix = "[Y/n]" if default == "y" else "[y/N]"
    try:
        got = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default == "y"
    if not got:
        return default == "y"
    return got.startswith("y")


def wizard(args) -> int:
    """Asked when run with no arguments on a terminal. Every answer has a
    default, so holding Enter runs the whole thing."""
    print("OSI / TOS citation harvester\n")
    print(FULL_RUN_BLURB)
    if not have_substack_cookies():
        print("Article bodies for paywalled posts need a subscriber session; without\n"
              "one those articles yield previews. Pass --cookie-file to fix that,\n"
              "now or on a later --fulltext-only run.\n")
    if _ask("Run all of that now?"):
        conn = connect(args.db)
        osi_text.migrate(conn)
        run_everything(conn, legacy=False, d1_text=False)
        osi_export.summarize(conn)
        conn.close()
        return 0

    print("\nFine - piece by piece. Enter accepts the default in brackets.\n")
    meta = _ask("Fetch article metadata (passes A-E)?")
    text = _ask("Download article bodies to .txt files (pass F)?")
    if text and not have_substack_cookies():
        print("  Bodies of paywalled posts (most of the back catalogue) need a")
        print("  subscriber session. Leave blank to skip and get previews.")
        try:
            cf = input("  Path to a cookies.txt from a logged-in browser? [none] ").strip()
        except EOFError:
            cf = ""
        if cf:
            print("  " + (load_cookies(cookie_file=cf) or "no cookies loaded"))
    if text:
        try:
            raw = input("  Limit to how many articles? [all] ").strip()
            args.text_limit = int(raw) if raw else None
        except (EOFError, ValueError):
            args.text_limit = None
    legacy = _ask("Also crawl the legacy site? Its TLS cert does not match its "
                  "hostname, so verification would be disabled", "n")
    d1_text = _ask("Embed article bodies in the D1 SQL? Large; the .txt files "
                   "already hold them", "n")

    conn = connect(args.db)
    osi_text.migrate(conn)
    if meta:
        print("\n[A] Substack year sitemaps")
        harvest_substack_sitemap(conn)
        print("[B] Substack archive API")
        harvest_substack_api(conn)
        print("[C] objectivestandard.org WordPress REST")
        harvest_wp(conn)
        if legacy:
            print("[D] legacy archive.theobjectivestandard.com")
            harvest_legacy(conn)
        print("[E] mapping articles to quarterly issues")
        link_issues(conn)
    elif legacy:
        print("\n[D] legacy archive.theobjectivestandard.com")
        harvest_legacy(conn)
    if text:
        print("[F] full text")
        warn_if_anonymous()
        osi_text.harvest_text(conn, get, substack_base=SUBSTACK,
                              text_dir=args.text_dir, limit=args.text_limit,
                              retry_previews=have_substack_cookies())
        osi_text.write_manifest(conn)
    print("\n[export]")
    export_all(conn, d1_text=d1_text, text_dir=args.text_dir)
    osi_export.summarize(conn)
    conn.close()
    return 0


# --------------------------------------------------------------------------- #

def main() -> None:
    global DELAY
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="With no arguments: asks once, then does everything. "
               "The flags below are for partial and repeat runs.")
    ap.add_argument("--all", action="store_true",
                    help="every pass and every export, no questions asked")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--delay", type=float, default=DELAY,
                    help="seconds between requests (default 1.0)")

    g0 = ap.add_argument_group("partial runs")
    g0.add_argument("--only", choices=["sitemap", "api", "substack", "wp", "legacy"],
                    action="append", help="restrict the metadata passes (repeatable)")
    g0.add_argument("--legacy-insecure", action="store_true",
                    help="also crawl archive.theobjectivestandard.com with TLS "
                         "verification off (its cert does not match its hostname)")
    g0.add_argument("--export-only", action="store_true",
                    help="no network, just re-export from the existing database")
    g0.add_argument("--no-export", action="store_true")

    g = ap.add_argument_group("full text (pass F)")
    g.add_argument("--cookie-file", metavar="PATH",
                   help="Netscape cookies.txt from a logged-in Substack "
                        "subscriber browser session - needed for the bodies of "
                        "paywalled posts. Env: OSI_COOKIE_FILE")
    g.add_argument("--cookie", metavar="STR",
                   help="raw 'name=value; ...' cookie header instead of a file. "
                        "Env: OSI_COOKIE")
    g.add_argument("--retry-previews", action="store_true",
                   help="also re-fetch bodies stored as paywalled-preview "
                        "(implied when cookies are supplied)")
    g.add_argument("--fulltext", action="store_true",
                   help="include pass F alongside the metadata passes")
    g.add_argument("--fulltext-only", action="store_true",
                   help="pass F alone against the existing database")
    g.add_argument("--text-dir", default=osi_text.TEXT_DIR)
    g.add_argument("--refetch-text", action="store_true",
                   help="re-fetch bodies already stored (default: resume, "
                        "retrying only items with no body yet)")
    g.add_argument("--text-limit", type=int,
                   help="stop after N articles - useful for a trial run")
    g.add_argument("--text-source", choices=["substack", "wp", "legacy"],
                   action="append", help="restrict pass F to these sources")

    g2 = ap.add_argument_group("exports")
    g2.add_argument("--d1-include-text", action="store_true",
                    help="embed article bodies in d1_data.sql (large)")

    args = ap.parse_args()
    DELAY = args.delay

    loaded = load_cookies(args.cookie_file, args.cookie)
    if loaded:
        print(f"[auth] {loaded}")

    # No arguments at all: ask, or just run the lot when not on a terminal.
    if len(sys.argv) == 1:
        if sys.stdin.isatty():
            sys.exit(wizard(args))
        args.all = True

    conn = connect(args.db)
    osi_text.migrate(conn)          # adds text columns to a pre-existing db

    if args.all:
        run_everything(conn, text_dir=args.text_dir, legacy=args.legacy_insecure,
                       d1_text=args.d1_include_text, text_limit=args.text_limit)
        osi_export.summarize(conn)
        conn.close()
        return

    want = set(args.only or ["substack", "wp"])
    if "substack" in want:
        want |= {"sitemap", "api"}

    if not (args.export_only or args.fulltext_only):
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

    if (args.fulltext or args.fulltext_only) and not args.export_only:
        print("[F] full text")
        warn_if_anonymous()
        osi_text.harvest_text(conn, get, substack_base=SUBSTACK,
                              text_dir=args.text_dir, refetch=args.refetch_text,
                              limit=args.text_limit, sources=args.text_source,
                              retry_previews=(args.retry_previews
                                              or have_substack_cookies()))
        osi_text.write_manifest(conn)

    if not args.no_export:
        print("\n[export]")
        export_all(conn, d1_text=args.d1_include_text, text_dir=args.text_dir)
    osi_export.summarize(conn)
    conn.close()


if __name__ == "__main__":
    main()
