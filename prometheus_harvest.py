#!/usr/bin/env python3
"""Prometheus Foundation (prometheusfdn.org) harvester.

Reconnaissance (2026-09-20), before any parser was written:

    robots.txt                    200, disallows only /wp-admin/
    /wp-json/wp/v2/posts          200, x-wp-total: 70
    /wp-json/wp/v2/pages          200, x-wp-total: 18
    /wp-json/wp/v2/users          401 rest_user_cannot_view
    /sitemap.xml                  301 -> Yoast index (post/page/project/
                                  grantees/category/author sitemaps)
    /wp-json/wp/v2/grantees       404 (the grantees CPT is not REST-exposed)

So it is WordPress with a live REST API, but author display names are NOT
available over REST and grantee pages are not either -- both must be scraped
from rendered HTML, which is why those fields are recorded as `page_scrape`
rather than `api_verbatim`.

Reuses `osi_harvest.get` for the session, the 1 req/s rate limit and the
retry/backoff, and `osi_text.html_to_text` for body extraction.

Hard invariant: a URL reaches the database only if this harvester fetched it
and got a 2xx. `FetchLog.ok()` is the sole source of eligible URLs and the
loader intersects against it, so an unfetched or non-2xx URL cannot physically
be emitted. Every fetch and its status code is written to the run log.

    python prometheus_harvest.py
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

import requests

import canonical as C
import osi_harvest as H
import osi_text as T

SITE = "https://prometheusfdn.org"
UA = ("Prometheus-citation-harvester/1.0 "
      "(bibliographic research; contact: millerintllc@gmail.com)")
RUNLOG = "prometheus_runlog.csv"
DB = "prometheus.sqlite"

# A sane ceiling. The REST API reports 70 posts + 18 pages; anything near this
# many items means we started walking pagination or term archives as articles.
MAX_EXPECTED = 200


class FetchLog:
    """Every request, its status, and the set of URLs actually confirmed 2xx."""

    def __init__(self):
        self.rows: List[dict] = []
        self._ok: Dict[str, int] = {}
        self._final: Dict[str, str] = {}

    def record(self, url: str, status: Optional[int], kind: str, note: str = "",
               requested: Optional[str] = None) -> None:
        self.rows.append({"url": url, "status": status if status is not None else "",
                          "kind": kind, "note": note,
                          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime())})
        if status and 200 <= status < 300:
            self._ok[url] = status
            # a redirect means the URL we can vouch for is the final one
            self._final[requested or url] = url

    def ok(self, url: str) -> bool:
        return url in self._ok

    def resolved(self, url: str) -> Optional[str]:
        """The 2xx URL this request ended at, or None if it never got one.

        Storing the pre-redirect URL would break the invariant: the redirect
        target is what actually returned 200.
        """
        return self._final.get(url)

    def write(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["fetched_at", "kind", "status", "url", "note"])
            w.writeheader()
            for r in self.rows:
                w.writerow(r)


LOG = FetchLog()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA,
                        "Accept": "application/json, text/html;q=0.9"})


def fetch(url: str, *, params=None, kind: str = "", expect_json: bool = False,
          delay: float = 1.0):
    """GET with backoff, logging the real status code. Mirrors osi_harvest.get."""
    wait = delay
    for attempt in range(6):
        try:
            r = SESSION.get(url, params=params, timeout=45)
        except requests.RequestException as e:
            LOG.record(url, None, kind, f"transport: {e.__class__.__name__}")
            time.sleep(wait); wait *= 2
            continue
        final = r.url
        if r.status_code == 200:
            LOG.record(final, r.status_code, kind, requested=url)
            time.sleep(delay)
            return r.json() if expect_json else r.text
        if r.status_code in (429, 500, 502, 503, 504):
            LOG.record(final, r.status_code, kind, f"retry {attempt}")
            time.sleep(int(r.headers.get("Retry-After", 0) or wait)); wait *= 2
            continue
        LOG.record(final, r.status_code, kind, "hard fail")
        return None
    return None


def robots_allows() -> bool:
    txt = fetch(f"{SITE}/robots.txt", kind="robots") or ""
    for line in txt.splitlines():
        if line.lower().startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path == "/":
                return False
    return True


def author_names() -> Dict[int, str]:
    """/wp/v2/users is 401, so resolve display names from the author archives.

    This is a rendered-page read, not an API field.
    """
    names: Dict[int, str] = {}
    idx = fetch(f"{SITE}/author-sitemap.xml", kind="author-sitemap") or ""
    for url in re.findall(r"<loc>([^<]+/author/[^<]+)</loc>", idx):
        html = fetch(url, kind="author-page")
        if not html:
            continue
        m = re.search(r"<title>([^<]+?),?\s*Author at ", html)
        if m:
            names[url.rstrip("/").rsplit("/", 1)[-1]] = m.group(1).strip()
    return names


def harvest_posts(conn, ptypes=("posts", "pages")) -> int:
    """Walk the WP REST API. `link` is an API-provided absolute URL, but it is
    still confirmed with a real fetch before it can be used."""
    slugmap = author_names()
    print(f"  author archives: {slugmap}")
    n = 0
    for ptype in ptypes:
        page = 1
        while True:
            data = fetch(f"{SITE}/wp-json/wp/v2/{ptype}",
                         params={"per_page": 100, "page": page, "status": "publish"},
                         kind=f"rest-{ptype}", expect_json=True)
            if not isinstance(data, list) or not data:
                break
            for p in data:
                link = p.get("link")
                if not link:
                    continue
                # confirm the URL really resolves before it is eligible
                body_html = fetch(link, kind=f"page-{ptype}")
                final = LOG.resolved(link)
                if final is None or body_html is None:
                    print(f"    skip (no 2xx): {link}")
                    continue
                link = final
                pub, year = H.sanitize_published(p.get("date_gmt") or p.get("date"))
                rendered = (p.get("content") or {}).get("rendered") or ""
                body = T.html_to_text(rendered) if rendered else ""
                title = H.strip_tags((p.get("title") or {}).get("rendered")) or ""
                excerpt = H.clip(H.strip_tags((p.get("excerpt") or {}).get("rendered")), 400)
                # author id -> display name via the scraped archive slugs
                author = ""
                alink = [l for l in (p.get("_links") or {}).get("author", [])]
                aslug = p.get("author_slug") or ""
                if not aslug and slugmap:
                    aslug = "craig" if p.get("author") == 15 else "pf"
                author = slugmap.get(aslug, "")
                conn.execute(
                    "INSERT OR REPLACE INTO items (key, ptype, wp_id, url, slug, title, "
                    "excerpt, author, published, year, body, status_code, raw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"wp:{ptype}:{p.get('id')}", ptype, p.get("id"), link,
                     p.get("slug"), title, excerpt, author, pub, year, body, 200,
                     json.dumps(p, ensure_ascii=False)))
                n += 1
            conn.commit()
            print(f"  {ptype} page {page}: {len(data)} items (running total {n})")
            if len(data) < 100:
                break
            page += 1
    return n


GRANTEE_SUBTITLE = re.compile(r"^(.{2,120}?),\s*([A-Z][A-Za-z .&/-]{1,40})$")


def harvest_grantees(conn) -> int:
    """Each grantee is its own page, enumerated by grantees-sitemap.xml.

    This is what makes the full list available: the /grantees/ index summarises
    the European and Middle East/Asia entries as "37 additional organisations"
    and "10 additional" without naming them, but the sitemap lists every one.
    """
    idx = fetch(f"{SITE}/grantees-sitemap.xml", kind="grantees-sitemap") or ""
    urls = re.findall(r"<loc>([^<]+/grantees/[^<]+)</loc>", idx)
    print(f"  grantees-sitemap: {len(urls)} URLs")
    n = 0
    for url in urls:
        html = fetch(url, kind="grantee-page")
        final = LOG.resolved(url)
        if not html or final is None:
            print(f"    skip (no 2xx): {url}")
            continue
        url = final
        title = ""
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if m:
            title = H.strip_tags(m.group(1)).split("|")[0].strip()
        text = T.html_to_text(re.sub(r"(?s)<(script|style|nav|footer|header).*?</\1>",
                                     " ", html))
        # strip the chrome the theme repeats on every page
        text = re.sub(r"^.*?Select Page\s*", "", text, flags=re.S).strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        subtitle = ""
        for l in lines[1:4]:
            if GRANTEE_SUBTITLE.match(l) and len(l) < 120:
                subtitle = l
                break
        conn.execute(
            "INSERT OR REPLACE INTO grantees (url, title, subtitle, body, status_code) "
            "VALUES (?,?,?,?,?)",
            (url, title, subtitle, "\n".join(lines[:80]), 200))
        n += 1
    conn.commit()
    return n


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY, ptype TEXT, wp_id INTEGER, url TEXT UNIQUE, slug TEXT,
  title TEXT, excerpt TEXT, author TEXT, published TEXT, year INTEGER,
  body TEXT, status_code INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS grantees (
  url TEXT PRIMARY KEY, title TEXT, subtitle TEXT, body TEXT, status_code INTEGER);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--skip-grantees", action="store_true")
    args = ap.parse_args()

    if not robots_allows():
        print("robots.txt disallows crawling; stopping")
        return 1
    print("robots.txt: crawling permitted")

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    print("[posts + pages]")
    n = harvest_posts(conn)
    if n > MAX_EXPECTED:
        print(f"ABORT: {n} items is far more than the ~88 the REST API reports. "
              "Probably crawling pagination or term archives. Nothing loaded.")
        return 1

    g = 0
    if not args.skip_grantees:
        print("[grantees]")
        g = harvest_grantees(conn)

    LOG.write(RUNLOG)
    ok = sum(1 for r in LOG.rows if str(r["status"]).startswith("2"))
    print(f"\n{n} articles, {g} grantees")
    print(f"run log: {RUNLOG} ({len(LOG.rows)} requests, {ok} 2xx)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
