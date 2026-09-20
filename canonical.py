#!/usr/bin/env python3
"""Shared mapping into the canonical D1 schema (`articles`/`authors`/`tags`).

The canonical schema lives in the remote `whoneedsit` database and is treated as
read-only here: `d1_canonical_schema.sql` is a dump of what is applied, kept for
reference only. Both corpus loaders (`load_osi.py`, `load_prometheus.py`) go
through this module so the two emit structurally identical SQL.

Everything is `INSERT OR IGNORE` against the UNIQUE columns `articles.url`,
`authors.name_norm` and `tags.tag_norm`, so a reload is idempotent. Junction
rows resolve their parents by subselect on those same UNIQUE keys rather than
assuming an AUTOINCREMENT id, which keeps them idempotent too.
"""

import hashlib
import json
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# CHECK-constrained vocabularies, copied from the applied schema. Emitting a
# value outside these sets fails at generation time rather than at load time.
CORPORA = {"OSI", "PROMETHEUS"}
DATE_BASES = {"api_verbatim", "sitemap_year", "page_scrape", "unknown"}
ACCESS = {"everyone", "only_paid", "unknown"}
TEXT_STATUS = {"ok", "paywalled-preview", "unavailable", "skipped"}

NONALNUM = re.compile(r"[^a-z0-9]+")


def norm(s: Optional[str]) -> str:
    """Lowercase, strip accents, remove every non-alphanumeric character.

    Used for `title_norm`, `authors.name_norm` and `tags.tag_norm`, so the same
    rule decides article identity, author dedup and tag dedup.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return NONALNUM.sub("", s.lower())


def norm_spaced(s: Optional[str]) -> str:
    """Like `norm` but keeps single spaces -- for comparing person names."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def sql(v) -> str:
    """SQLite literal. None and empty string both become NULL."""
    if v is None or v == "":
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''").replace("\x00", "") + "'"


def sha256(s: Optional[str]) -> Optional[str]:
    return hashlib.sha256(s.encode("utf-8")).hexdigest() if s else None


def split_names(s: Optional[str]) -> List[str]:
    return [p.strip() for p in (s or "").split(";") if p.strip()]


class PersonMatcher:
    """Conservative `authors.person_id` resolution against the `people` table.

    Matches only on an exact normalised full name, or on an exact
    `first last` reconstruction. Anything ambiguous -- a normalised name shared
    by two people, a surname-only or initials-only byline -- resolves to None.
    Leaving person_id NULL is always preferable to attaching an article to the
    wrong person.
    """

    def __init__(self, people: List[dict]):
        self.index: Dict[str, Optional[int]] = {}
        for p in people:
            for cand in (p.get("name_full"),
                         f"{p.get('first_name','')} {p.get('last_name','')}"):
                k = norm_spaced(cand)
                if not k or " " not in k:        # require at least two tokens
                    continue
                # a collision makes the key unusable rather than a coin flip
                self.index[k] = None if k in self.index and self.index[k] != p["id"] \
                    else self.index.get(k, p["id"])

    def match(self, name: str) -> Optional[int]:
        return self.index.get(norm_spaced(name))


class Load:
    """Accumulates canonical rows and renders them as idempotent SQL."""

    def __init__(self, corpus: str, org_id: int):
        assert corpus in CORPORA, corpus
        self.corpus, self.org_id = corpus, org_id
        self.articles: List[dict] = []
        self.authors: Dict[str, Tuple[str, Optional[int]]] = {}   # norm -> (name, person_id)
        self.tags: Dict[str, str] = {}                            # norm -> tag
        self.links: List[Tuple[str, str, int]] = []               # url, author_norm, order
        self.taglinks: List[Tuple[str, str]] = []                 # url, tag_norm
        self.bodies: List[Tuple[str, str, str]] = []              # url, body, sha
        self.skipped: List[Tuple[str, str]] = []

    def add(self, *, url, title, platform, date_basis, pub_date=None, pub_year=None,
            canonical_url=None, slug=None, subtitle=None, access=None,
            text_status=None, word_count=None, excerpt=None, source=None,
            source_url=None, body=None, authors="", tags="", matcher=None):
        if not url or not title:
            self.skipped.append((url or "?", "missing url or title"))
            return
        assert date_basis in DATE_BASES, date_basis
        assert access in ACCESS or access is None, access
        assert text_status in TEXT_STATUS or text_status is None, text_status
        # pub_date without a pub_year, or a year outside the plausible window,
        # would both mean the date mapping upstream is wrong -- fail loudly.
        if pub_year is not None:
            assert 1990 <= int(pub_year) <= 2100, (url, pub_year)
        if pub_date:
            assert date_basis in ("api_verbatim", "page_scrape"), (url, date_basis)

        self.articles.append(dict(
            org_id=self.org_id, corpus=self.corpus, platform=platform, url=url,
            canonical_url=canonical_url, slug=slug, title=title,
            title_norm=norm(title), subtitle=subtitle, pub_date=pub_date,
            pub_year=pub_year, date_basis=date_basis, access=access,
            text_status=text_status, word_count=word_count, excerpt=excerpt,
            body=None, body_sha256=sha256(body) if body else None,
            source=source, source_url=source_url))

        if body:
            self.bodies.append((url, body, sha256(body)))

        for i, name in enumerate(split_names(authors), start=1):
            k = norm(name)
            if not k:
                continue
            if k not in self.authors:
                self.authors[k] = (name, matcher.match(name) if matcher else None)
            self.links.append((url, k, i))

        for tag in split_names(tags):
            k = norm(tag)
            if not k:
                continue
            self.tags.setdefault(k, tag)
            self.taglinks.append((url, k))

    # -- rendering ---------------------------------------------------------- #

    ART_COLS = ["org_id", "corpus", "platform", "url", "canonical_url", "slug",
                "title", "title_norm", "subtitle", "pub_date", "pub_year",
                "date_basis", "access", "text_status", "word_count", "excerpt",
                "body", "body_sha256", "source", "source_url"]

    def write(self, path: str, batch: int = 40) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"-- {self.corpus} canonical load, org_id={self.org_id}\n"
                    f"-- {len(self.articles)} articles, {len(self.authors)} authors, "
                    f"{len(self.tags)} tags\n"
                    "-- idempotent: INSERT OR IGNORE on UNIQUE url/name_norm/tag_norm\n\n")
            for i in range(0, len(self.authors), batch):
                chunk = list(self.authors.items())[i:i + batch]
                f.write("INSERT OR IGNORE INTO authors (name, name_norm, person_id) VALUES\n" +
                        ",\n".join(f"  ({sql(n)}, {sql(k)}, {sql(pid)})"
                                   for k, (n, pid) in chunk) + ";\n")
            f.write("\n")
            for i in range(0, len(self.tags), batch):
                chunk = list(self.tags.items())[i:i + batch]
                f.write("INSERT OR IGNORE INTO tags (tag, tag_norm) VALUES\n" +
                        ",\n".join(f"  ({sql(t)}, {sql(k)})" for k, t in chunk) + ";\n")
            f.write("\n")
            cols = ", ".join(self.ART_COLS)
            for i in range(0, len(self.articles), batch):
                chunk = self.articles[i:i + batch]
                f.write(f"INSERT OR IGNORE INTO articles ({cols}) VALUES\n" +
                        ",\n".join("  (" + ", ".join(sql(a[c]) for c in self.ART_COLS) + ")"
                                   for a in chunk) + ";\n")
            f.write("\n")
            for url, an, order in self.links:
                f.write("INSERT OR IGNORE INTO article_authors (article_id, author_id, author_order) "
                        f"SELECT (SELECT id FROM articles WHERE url={sql(url)}), "
                        f"(SELECT id FROM authors WHERE name_norm={sql(an)}), {order} "
                        f"WHERE (SELECT id FROM articles WHERE url={sql(url)}) IS NOT NULL;\n")
            f.write("\n")
            for url, tn in self.taglinks:
                f.write("INSERT OR IGNORE INTO article_tags (article_id, tag_id) "
                        f"SELECT (SELECT id FROM articles WHERE url={sql(url)}), "
                        f"(SELECT id FROM tags WHERE tag_norm={sql(tn)}) "
                        f"WHERE (SELECT id FROM articles WHERE url={sql(url)}) IS NOT NULL;\n")

    def write_bodies(self, path: str, chunk: int = 40000) -> List[str]:
        """Bodies go in their own file -- never committed, loaded separately.

        A long article exceeds D1's per-statement limit (SQLITE_TOOBIG), so any
        body over `chunk` characters is written as one overwriting UPDATE
        followed by ordered appends. Re-running replays the same sequence from
        the same starting overwrite, so this stays idempotent.
        """
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"-- {self.corpus} bodies: {len(self.bodies)} rows. "
                    "Load after the main file. Not committed.\n\n")
            for url, body, sha in self.bodies:
                parts = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [""]
                f.write(f"UPDATE articles SET body={sql(parts[0])} "
                        f"WHERE url={sql(url)};\n")
                for p in parts[1:]:
                    f.write(f"UPDATE articles SET body=body||{sql(p)} "
                            f"WHERE url={sql(url)};\n")
                f.write(f"UPDATE articles SET body_sha256={sql(sha)} "
                        f"WHERE url={sql(url)};\n")
        return [path]

    def summary(self) -> str:
        from collections import Counter
        pf = Counter((a["platform"], a["date_basis"]) for a in self.articles)
        return (f"{self.corpus}: {len(self.articles)} articles, "
                f"{len(self.authors)} authors, {len(self.tags)} tags, "
                f"{len(self.bodies)} bodies, {len(self.skipped)} skipped\n" +
                "\n".join(f"  {p:10s} {d:14s} {n}" for (p, d), n in sorted(pf.items())))
