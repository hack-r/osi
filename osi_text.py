"""
osi_text.py - full-text retrieval and plain-text file output for text mining.

Pass F of the harvest. For every item in the database it tries, in order, the
cheapest source of an article body that is likely to work, writes the result to
a self-describing .txt file, and records in the database which strategy
succeeded so a later run can tell a clean API body from a scraped page.

Body sources by item source
---------------------------
wp        `content.rendered` from the WordPress REST payload already stored in
          `raw`. No extra request. Reliable.
substack  in order: `body_html` if the archive payload happened to carry it;
          then two candidate post-detail API endpoints; then the article page,
          read first from the `window._preloads` JSON blob and otherwise from
          the article container. The endpoint shapes are probed rather than
          assumed - whichever answers is recorded per item in `text_strategy`.
legacy    the article page, generic extraction.

Paywalled posts return a short preview. Because the archive API gives a
`wordcount` for every post, a body that comes back at less than
`PREVIEW_RATIO` of the expected length is marked `paywalled-preview` rather
than silently stored as if it were complete.

No dependencies beyond requests + the standard library: HTML is reduced to text
by a small HTMLParser subclass, not by bs4/lxml.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import unicodedata
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

# A body shorter than this fraction of the API-reported wordcount is treated as
# a paywall preview rather than the article.
PREVIEW_RATIO = 0.40
# Below this many words nothing is worth calling an article body.
MIN_BODY_WORDS = 40

TEXT_DIR = "texts"
MANIFEST = "text_manifest.csv"

SKIP_TAGS = {
    "script", "style", "noscript", "head", "svg", "nav", "footer", "aside",
    "form", "button", "iframe", "template",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol", "blockquote", "pre", "tr", "figcaption", "figure",
    "table", "hr", "dd", "dt", "dl", "header", "main",
}


class _TextExtractor(HTMLParser):
    """Collapse HTML to readable plain text, preserving paragraph breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._out.append("\n")
        elif tag in BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        self._out.append(data)

    def text(self) -> str:
        return normalize_text("".join(self._out))


def normalize_text(s: str) -> str:
    """Trim each line, collapse runs of blank lines, drop stray whitespace."""
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace(" ", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    out: List[str] = []
    blank = 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    return "\n".join(out).strip()


def html_to_text(markup: Optional[str]) -> str:
    if not markup:
        return ""
    p = _TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    return p.text()


# --------------------------------------------------------------------------- #
# filenames
# --------------------------------------------------------------------------- #

def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return s.encode("ascii", "ignore").decode("ascii")


def slugify(s: Optional[str], maxlen: int = 80) -> str:
    s = _ascii(html.unescape(s or "")).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]
    return s or "untitled"


def author_slug(authors: Optional[str]) -> str:
    """'Ari Armstrong; Craig Biddle' -> 'Armstrong-Ari+Biddle-Craig'.

    Three or more authors collapse to the first plus '+et-al' so filenames stay
    a usable length.
    """
    names = [a.strip() for a in (authors or "").split(";") if a.strip()]
    if not names:
        return "unattributed"
    parts: List[str] = []
    for n in names[:2]:
        toks = _ascii(n).split()
        if len(toks) == 1:
            parts.append(slugify(toks[0], 30).title())
        else:
            parts.append(f"{slugify(toks[-1], 30).title()}-{slugify(' '.join(toks[:-1]), 30).title()}")
    if len(names) > 2:
        parts = parts[:1] + ["et-al"]
    return "+".join(parts)


def text_filename(row: Dict[str, Any]) -> str:
    """`2006-05-20_Armstrong-Ari_teaching-values_substack-99.txt`

    Date first so a directory listing sorts chronologically; author next so
    one writer's output groups together; then title; then the source and id,
    which make the name unique and traceable back to `items.key`.
    """
    date = (row.get("published") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = f"{row['year']}-00-00" if row.get("year") else "undated"
    src = row.get("source") or "unknown"
    ident = slugify(str(row.get("source_id") or row.get("slug") or "x"), 40)
    return f"{date}_{author_slug(row.get('authors'))}_{slugify(row.get('title'), 70)}_{src}-{ident}.txt"


# --------------------------------------------------------------------------- #
# the .txt file itself
# --------------------------------------------------------------------------- #

RULE = "=" * 78
HEADER_FIELDS = [
    ("Title", "title"),
    ("Subtitle", "subtitle"),
    ("Author(s)", "authors"),
    ("Published", "published"),
    ("Publication", "_publication"),
    ("Issue", "issue_label"),
    ("Section", "section"),
    ("Tags", "tags"),
    ("Item type", "item_type"),
    ("Access", "access"),
    ("URL", "url"),
    ("Word count (source)", "wordcount"),
    ("Item key", "key"),
    ("Retrieved", "retrieved_at"),
]


def publication_of(source: Optional[str]) -> str:
    return ("The Objective Standard" if source in ("substack", "legacy")
            else "Objective Standard Institute")


def render_text_file(row: Dict[str, Any], body: str, *, status: str,
                     strategy: str) -> str:
    """A header block a miner can parse or strip, then the body.

    The header is delimited by two rule lines so `sed '1,/^=\\{78\\}$/d'` (or a
    split on the second rule) yields the bare article text.
    """
    meta = dict(row)
    meta["_publication"] = publication_of(row.get("source"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    lines = [RULE]
    for label, field in HEADER_FIELDS:
        val = meta.get(field)
        if val in (None, ""):
            continue
        lines.append(f"{label + ':':<22}{val}")
    lines.append(f"{'Text status:':<22}{status}")
    lines.append(f"{'Text strategy:':<22}{strategy}")
    lines.append(f"{'Text words:':<22}{len(body.split())}")
    lines.append(f"{'Text sha256:':<22}{digest}")
    lines.append(RULE)
    lines.append("")
    lines.append(body)
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# body retrieval
# --------------------------------------------------------------------------- #

# Substack post-detail endpoints, tried in order. Neither shape is documented;
# whichever answers is recorded per item, and the HTML fallback covers both
# failing.
SUBSTACK_POST_ENDPOINTS = (
    "{base}/api/v1/posts/by-id/{id}",
    "{base}/api/v1/posts/{slug}",
)

# Article containers, most specific first. Substack's paywalled pages use
# `available-content` for the free portion.
CONTAINER_RES = (
    re.compile(r'<div[^>]+class="[^"]*available-content[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S),
    re.compile(r'<div[^>]+class="[^"]*body markup[^"]*"[^>]*>(.*?)</div>', re.S),
    re.compile(r'<div[^>]+class="[^"]*(?:entry-content|post-content|article-content)[^"]*"[^>]*>(.*?)</div>\s*(?:</div>|<footer)', re.S),
    re.compile(r"<article[^>]*>(.*?)</article>", re.S),
)
PRELOADS_RE = re.compile(r'window\._preloads\s*=\s*JSON\.parse\("(.*?)"\)\s*[;<]', re.S)


def _from_preloads(page: str) -> Tuple[str, str]:
    """Substack embeds the post JSON in a double-encoded string. Best source of
    a clean body when the API endpoints are unavailable."""
    m = PRELOADS_RE.search(page)
    if not m:
        return "", ""
    try:
        decoded = json.loads('"' + m.group(1) + '"')
        data = json.loads(decoded)
    except ValueError:
        return "", ""
    for path in (("post", "body_html"), ("post", "body"), ("body_html",)):
        node: Any = data
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node.strip():
            return node, "html-preloads"
    return "", ""


def _from_container(page: str) -> Tuple[str, str]:
    for i, rx in enumerate(CONTAINER_RES):
        m = rx.search(page)
        if m and len(m.group(1)) > 200:
            return m.group(1), f"html-container-{i}"
    return "", ""


def fetch_body_html(getter, row: Dict[str, Any], raw: Optional[dict],
                    substack_base: str) -> Tuple[str, str]:
    """Return (body_html, strategy). Empty html means nothing was found."""
    source = row.get("source")

    if source == "wp":
        content = ((raw or {}).get("content") or {}).get("rendered")
        if content:
            return content, "wp-rest-content"

    if source == "substack":
        for field in ("body_html", "body"):
            val = (raw or {}).get(field)
            if isinstance(val, str) and val.strip():
                return val, f"substack-archive-{field}"
        pid, slug = (raw or {}).get("id") or row.get("source_id"), row.get("slug")
        for tmpl in SUBSTACK_POST_ENDPOINTS:
            if "{id}" in tmpl and not pid:
                continue
            if "{slug}" in tmpl and not slug:
                continue
            url = tmpl.format(base=substack_base, id=pid, slug=slug)
            data = getter(url, expect_json=True)
            if isinstance(data, dict):
                for field in ("body_html", "body"):
                    val = data.get(field)
                    if isinstance(val, str) and val.strip():
                        return val, f"substack-api:{tmpl.split('/api/v1/')[1]}"

    url = row.get("url")
    if url:
        page = getter(url)
        if isinstance(page, str) and page:
            for extractor in (_from_preloads, _from_container):
                body, how = extractor(page)
                if body:
                    return body, how
    return "", "none"


def classify(body: str, expected_words: Optional[int]) -> str:
    words = len(body.split())
    if words < MIN_BODY_WORDS:
        return "unavailable"
    if expected_words and expected_words > 0:
        if words < expected_words * PREVIEW_RATIO:
            return "paywalled-preview"
    return "ok"


# --------------------------------------------------------------------------- #
# pass F
# --------------------------------------------------------------------------- #

TEXT_COLUMNS = {
    "text_path": "TEXT",
    "text_status": "TEXT",       # ok | paywalled-preview | unavailable
    "text_strategy": "TEXT",
    "text_sha256": "TEXT",
    "text_words": "INTEGER",
    "text_chars": "INTEGER",
}


def migrate(conn: sqlite3.Connection) -> None:
    """Add the text columns to a database written by an earlier version."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in TEXT_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
    conn.commit()


def harvest_text(conn: sqlite3.Connection, getter, *, substack_base: str,
                 text_dir: str = TEXT_DIR, refetch: bool = False,
                 limit: Optional[int] = None,
                 sources: Optional[List[str]] = None) -> Dict[str, int]:
    """Write one .txt per item. Resumable: items that already have a readable
    file on disk are skipped unless `refetch`."""
    migrate(conn)
    os.makedirs(text_dir, exist_ok=True)
    conn.row_factory = sqlite3.Row

    where, params = "1=1", []
    if sources:
        where += f" AND source IN ({','.join('?' * len(sources))})"
        params += sources
    if not refetch:
        # Retry anything that has no file yet, and anything previously
        # unavailable; leave good bodies and known previews alone.
        where += " AND (text_status IS NULL OR text_status='unavailable')"
    sql = (f"SELECT * FROM items WHERE {where} "
           "ORDER BY (published IS NULL), published DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    stats = {"ok": 0, "paywalled-preview": 0, "unavailable": 0, "skipped": 0}
    print(f"  {len(rows)} item(s) to fetch text for")

    for i, r in enumerate(rows, 1):
        row = dict(r)
        path = os.path.join(text_dir, text_filename(row))
        if not refetch and row.get("text_path") and os.path.exists(path) \
                and row.get("text_status") == "ok":
            stats["skipped"] += 1
            continue

        raw = None
        got = conn.execute("SELECT body FROM raw WHERE key=?", (row["key"],)).fetchone()
        if got and got["body"]:
            try:
                raw = json.loads(got["body"])
            except ValueError:
                raw = None

        markup, strategy = fetch_body_html(getter, row, raw, substack_base)
        body = html_to_text(markup)
        status = classify(body, row.get("wordcount"))

        if status == "unavailable":
            stats["unavailable"] += 1
            conn.execute(
                "UPDATE items SET text_status=?, text_strategy=?, text_words=0 "
                "WHERE key=?", (status, strategy, row["key"]))
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(render_text_file(row, body, status=status, strategy=strategy))
            stats[status] += 1
            conn.execute(
                "UPDATE items SET text_path=?, text_status=?, text_strategy=?, "
                "text_sha256=?, text_words=?, text_chars=? WHERE key=?",
                (path, status, strategy,
                 hashlib.sha256(body.encode("utf-8")).hexdigest(),
                 len(body.split()), len(body), row["key"]))
        if i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}  ok={stats['ok']} "
                  f"preview={stats['paywalled-preview']} "
                  f"none={stats['unavailable']}")
    conn.commit()
    print(f"  text: ok={stats['ok']} paywalled-preview={stats['paywalled-preview']} "
          f"unavailable={stats['unavailable']} skipped={stats['skipped']}")
    return stats


def write_manifest(conn: sqlite3.Connection, path: str = MANIFEST) -> int:
    """Index of the text corpus: one row per written file."""
    import csv as _csv
    conn.row_factory = sqlite3.Row
    cols = ["key", "text_path", "text_status", "text_strategy", "text_words",
            "text_chars", "text_sha256", "source", "published", "year",
            "authors", "title", "issue_label", "access", "url"]
    rows = conn.execute(
        f"SELECT {','.join(cols)} FROM items WHERE text_path IS NOT NULL "
        "ORDER BY (published IS NULL), published DESC").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in cols})
    print(f"  wrote {path} ({len(rows)} files)")
    return len(rows)
