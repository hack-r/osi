#!/usr/bin/env python3
"""Offline regression tests for the harvester: parsing, upsert precedence,
issue linking, full-text extraction and file naming, and all four export
targets. No network.

The D1 export is checked by executing the generated SQL against a fresh
in-memory SQLite database -- D1 is SQLite, so SQL that SQLite accepts and that
reproduces the expected rows is SQL D1 will accept.

    python test_offline.py
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import osi_export
import osi_harvest as H
import osi_text as T

os.chdir(tempfile.mkdtemp(prefix="osi-test-"))
FAIL = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAIL.append(label)


# --------------------------------------------------------------------------- #
print("\n[parsing]")
# empty rendered excerpt: strip_tags returns "" -> clip must not raise
check("clip tolerates empty excerpt",
      H.clip(H.strip_tags(({"rendered": ""}).get("rendered")), 400) is None)
check("clip truncates", H.clip(H.strip_tags("<p>hi &amp; bye</p>"), 4) == "hi &")
check("corrupt year dropped", H.year_of("0002-08-12") is None)
check("valid year kept", H.year_of("2006-05-20T00:00:00Z") == 2006)
check("null date", H.year_of(None) is None)
check("structured byline wins",
      H.authors_from([{"name": "Craig Biddle"}], "By Someone Else") == "Craig Biddle")
check("subtitle byline fallback",
      H.authors_from([], "By Ari Armstrong") == "Ari Armstrong")
check("multi-author subtitle",
      H.authors_from(None, "<p>By Ari Armstrong and Craig Biddle</p>")
      == "Ari Armstrong; Craig Biddle")
check("subtitle without byline", H.authors_from(None, "A subtitle") == "")
check("issue label", H.issue_label("The Fall 2026 Issue of TOS Is Published!") == "Fall 2026")
check("autumn normalized", H.issue_label("The Autumn 2011 Issue") == "Fall 2011")
check("split-year issue",
      H.issue_label("Winter 2006-07 Issue of TOS Is Published!") == "Winter 2006-07")
check("no issue label", H.issue_label("nothing here") is None)

# --------------------------------------------------------------------------- #
print("\n[html to text]")
markup = """<html><head><title>x</title><style>p{color:red}</style></head>
<body><nav>Skip this nav</nav><script>var drop = 1;</script>
<article><h1>Heading</h1><p>First &amp; best paragraph.</p>
<p>Second   paragraph<br>with a break.</p>
<ul><li>Alpha</li><li>Beta</li></ul>
<blockquote>Quoted&nbsp;line.</blockquote></article>
<footer>Skip footer</footer></body></html>"""
txt = T.html_to_text(markup)
check("drops script/style/nav/footer",
      not any(w in txt for w in ("drop", "color:red", "Skip this nav", "Skip footer")), txt)
check("entities decoded", "First & best paragraph." in txt, txt)
check("paragraphs separated", "\n\n" in txt)
check("br is a single newline", "Second paragraph\nwith a break." in txt, repr(txt))
check("nbsp normalized", "Quoted line." in txt, txt)
check("list items kept", "Alpha" in txt and "Beta" in txt)
check("no triple newline", "\n\n\n" not in txt, repr(txt))
check("empty markup safe", T.html_to_text(None) == "" and T.html_to_text("") == "")
check("malformed markup safe", "hello" in T.html_to_text("<p>hello<div><span>"))

# --------------------------------------------------------------------------- #
print("\n[filenames]")
row = {"published": "2006-05-20T00:00:00Z", "year": 2006, "source": "substack",
       "source_id": "99", "slug": "teaching-values",
       "title": "Teaching Values in the Classroom", "authors": "Ari Armstrong"}
fn = T.text_filename(row)
check("filename shape",
      fn == "2006-05-20_Armstrong-Ari_teaching-values-in-the-classroom_substack-99.txt", fn)
check("two authors joined",
      T.author_slug("Ari Armstrong; Craig Biddle") == "Armstrong-Ari+Biddle-Craig")
check("three authors collapse",
      T.author_slug("A One; B Two; C Three") == "One-A+et-al",
      T.author_slug("A One; B Two; C Three"))
check("single-name author", T.author_slug("Aristotle") == "Aristotle")
check("no author", T.author_slug(None) == "unattributed")
check("undated filename",
      T.text_filename({"source": "wp", "source_id": "7", "title": "T", "authors": ""})
      .startswith("undated_unattributed_t_wp-7"))
check("year-only filename",
      T.text_filename({"year": 2009, "source": "wp", "source_id": "7", "title": "T",
                       "authors": ""}).startswith("2009-00-00_"))
check("unicode title transliterated",
      T.slugify("Ayn Rand’s Éthique & Reason!") == "ayn-rands-ethique-reason",
      T.slugify("Ayn Rand’s Éthique & Reason!"))
check("long title truncated at a word boundary",
      len(T.slugify("word " * 60, 70)) <= 70)

# --------------------------------------------------------------------------- #
print("\n[text file format]")
body = "Paragraph one.\n\nParagraph two."
out = T.render_text_file(dict(row, key="substack:teaching-values",
                              retrieved_at="2026-09-20T00:00:00+00:00",
                              access="only_paid", wordcount=3200,
                              issue_label="Winter 2006-07", tags="education",
                              section="Education", item_type="newsletter",
                              subtitle="By Ari Armstrong", url="https://x/p/tv"),
                         body, status="paywalled-preview", strategy="html-preloads")
for label in ("Title:", "Author(s):", "Published:", "Publication:", "Issue:",
              "URL:", "Access:", "Item key:", "Text status:", "Text sha256:"):
    check(f"header has {label}", label in out)
check("publication named", "The Objective Standard" in out)
check("body present", body in out)
check("two rule lines", out.count("=" * 78) == 2)
with open("sample.txt", "w", encoding="utf-8") as fh:
    fh.write(out)
check("header strips back to bare body", osi_export._read_body("sample.txt") == body,
      repr(osi_export._read_body("sample.txt")))
check("missing file reads empty", osi_export._read_body("nope.txt") == "")

# --------------------------------------------------------------------------- #
print("\n[paywall classification]")
check("short body unavailable", T.classify("a b c", 3000) == "unavailable")
check("preview detected", T.classify("word " * 100, 3000) == "paywalled-preview")
check("full body ok", T.classify("word " * 2900, 3000) == "ok")
check("no expected wordcount -> ok", T.classify("word " * 100, None) == "ok")
check("zero expected wordcount -> ok", T.classify("word " * 100, 0) == "ok")

# --------------------------------------------------------------------------- #
print("\n[database: upsert and issue linking]")
conn = H.connect("t.sqlite")
# sitemap-style row (title+year only), then the API row enriching it
H.save(conn, {"key": "substack:teaching-values", "source": "substack",
              "source_id": "teaching-values",
              "url": "https://www.theobjectivestandard.com/p/teaching-values",
              "slug": "teaching-values", "title": "Teaching Values in the Classroom",
              "year": 2006, "item_type": "newsletter"})
H.save(conn, {"key": "substack:fall-2026-issue", "source": "substack", "source_id": "1",
              "url": "https://www.theobjectivestandard.com/p/fall-2026-issue",
              "slug": "fall-2026-issue",
              "title": "The Fall 2026 Issue of TOS Is Published!",
              "published": "2026-09-01T00:00:00Z", "year": 2026, "access": "everyone",
              "item_type": "newsletter", "issue_label": "Fall 2026"},
       raw={"body_html": '<a href="/p/teaching-values">Teaching Values</a>'
                         '<a href="https://www.theobjectivestandard.com/p/other">Other</a>'})
H.save(conn, {"key": "substack:teaching-values", "source": "substack", "source_id": "99",
              "url": "https://www.theobjectivestandard.com/p/teaching-values",
              "slug": "teaching-values", "title": "Teaching Values in the Classroom",
              "subtitle": "By Ari Armstrong", "authors": "Ari Armstrong",
              "published": "2006-05-20T00:00:00Z", "year": 2006,
              "item_type": "newsletter", "access": "only_paid", "section": "Education",
              "tags": "education; values", "wordcount": 3200},
       raw={"id": 99})
# an apostrophe in the title, to prove the D1 SQL escaping
H.save(conn, {"key": "wp:posts:1", "source": "wp", "source_id": "1",
              "url": "https://objectivestandard.org/p1", "slug": "s1",
              "title": "Rand's Ethics {braces}", "authors": "Ari Armstrong",
              "published": "2006-11-02T00:00:00Z", "year": 2006, "item_type": "wp-post",
              "access": "everyone", "section": "posts", "tags": "ethics"},
       raw={"content": {"rendered": "<p>" + "body " * 300 + "</p>"}})
H.save(conn, {"key": "wp:posts:2", "source": "wp", "source_id": "2",
              "url": "https://objectivestandard.org/p2", "slug": "s2",
              "title": "No Date Post", "authors": "Aristotle", "item_type": "wp-post",
              "access": "everyone", "section": "posts"},
       raw={"content": {"rendered": "<p>tiny</p>"}})
conn.commit()

H.link_issues(conn)
lab = conn.execute("SELECT issue_label FROM items WHERE key='substack:teaching-values'"
                   ).fetchone()[0]
check("issue stamped onto linked article", lab == "Fall 2026", lab)
got = tuple(conn.execute("SELECT authors,access,tags,wordcount FROM items "
                         "WHERE key='substack:teaching-values'").fetchone())
check("upsert never nulls a populated field",
      got == ("Ari Armstrong", "only_paid", "education; values", 3200), got)

# --------------------------------------------------------------------------- #
print("\n[pass F with a stub fetcher]")
PAGE = ('<html><body><script>window._preloads = JSON.parse("'
        '{\\"post\\": {\\"body_html\\": \\"<p>' + "preloaded " * 60 + '</p>\\"}}'
        '");</script><div class="available-content"><p>container</p></div>'
        '</body></html>')
calls = []


def stub_get(url, *, params=None, verify=True, expect_json=False):
    calls.append(url)
    if expect_json:
        return None          # force the HTML path for the substack item
    return PAGE


stats = T.harvest_text(conn, stub_get, substack_base=H.SUBSTACK, text_dir="texts")
check("wp body came from stored REST payload, no request",
      not any("objectivestandard.org/p1" in u for u in calls), calls)
check("one ok body", stats["ok"] == 1, stats)
check("preview flagged", stats["paywalled-preview"] == 1, stats)
check("short bodies unavailable", stats["unavailable"] == 2, stats)
conn.row_factory = sqlite3.Row
r = dict(conn.execute("SELECT * FROM items WHERE key='wp:posts:1'").fetchone())
check("wp strategy recorded", r["text_strategy"] == "wp-rest-content", r["text_strategy"])
check("text file written", os.path.exists(r["text_path"]), r["text_path"])
check("words recorded", r["text_words"] == 300, r["text_words"])
check("sha256 recorded", len(r["text_sha256"] or "") == 64)
sub = dict(conn.execute("SELECT * FROM items WHERE key='substack:teaching-values'"
                        ).fetchone())
check("substack fell through to preloads", sub["text_strategy"] == "html-preloads",
      sub["text_strategy"])
check("preview status stored", sub["text_status"] == "paywalled-preview",
      sub["text_status"])
none_row = dict(conn.execute("SELECT * FROM items WHERE key='wp:posts:2'").fetchone())
check("unavailable writes no file", none_row["text_path"] is None)

# resumability
calls.clear()
stats2 = T.harvest_text(conn, stub_get, substack_base=H.SUBSTACK, text_dir="texts")
check("good bodies skipped on re-run", stats2["ok"] == 0 and stats2["skipped"] == 0,
      stats2)
check("only unavailable items are retried",
      stats2["unavailable"] == 2, stats2)

n = T.write_manifest(conn)
check("manifest lists written files only", n == 2, n)

# --------------------------------------------------------------------------- #
print("\n[migration of a pre-text database]")
old = sqlite3.connect("old.sqlite")
old.executescript("""CREATE TABLE items (key TEXT PRIMARY KEY, source TEXT,
 source_id TEXT, url TEXT, slug TEXT, title TEXT, subtitle TEXT, authors TEXT,
 published TEXT, year INTEGER, item_type TEXT, access TEXT, section TEXT,
 tags TEXT, wordcount INTEGER, issue_label TEXT, retrieved_at TEXT);
 CREATE TABLE raw (key TEXT PRIMARY KEY, body TEXT);
 INSERT INTO items (key, source, title) VALUES ('wp:posts:9','wp','Legacy row');""")
old.commit()
T.migrate(old)
T.migrate(old)  # idempotent
cols = {r[1] for r in old.execute("PRAGMA table_info(items)")}
check("text columns added", set(T.TEXT_COLUMNS).issubset(cols))
osi_export.export_csv(old, "old.csv")
check("export works on a migrated db", os.path.getsize("old.csv") > 0)
old.close()

# --------------------------------------------------------------------------- #
print("\n[exports]")
osi_export.export_csv(conn)
osi_export.export_csl(conn)
osi_export.export_bibtex(conn)
osi_export.export_d1(conn, include_text=True)

csl = json.load(open("osi_zotero.csl.json"))
check("csl every item has an id", all("id" in i for i in csl))
tv = [i for i in csl if i["id"] == "substack:teaching-values"][0]
check("csl full date", tv["issued"] == {"date-parts": [[2006, 5, 20]]}, tv.get("issued"))
check("csl author split", tv["author"] == [{"given": "Ari", "family": "Armstrong"}])
check("csl magazine type", tv["type"] == "article-magazine")
check("csl container", tv["container-title"] == "The Objective Standard")
check("csl accessed date-parts", isinstance(tv["accessed"]["date-parts"][0][0], int))
check("csl keywords comma-joined", tv["keyword"] == "education, values", tv.get("keyword"))
check("csl extra carries fulltext path", "fulltext file:" in tv["note"], tv["note"])
check("csl extra carries item key", "item key: substack:teaching-values" in tv["note"])
check("csl extra carries access", "access: only_paid" in tv["note"])
wp = [i for i in csl if i["id"] == "wp:posts:1"][0]
check("csl blog type for wp", wp["type"] == "post-weblog")
check("csl wp container", wp["container-title"] == "Objective Standard Institute")
nd = [i for i in csl if i["id"] == "wp:posts:2"][0]
check("csl omits issued when undated", "issued" not in nd, nd)
check("csl literal single name", nd["author"] == [{"literal": "Aristotle"}])

bib = open("osi_citations.bib").read()
keys = sorted(l.split("{", 1)[1].rstrip(",\n") for l in bib.splitlines()
              if l.startswith("@article"))
check("bibtex keys unique", len(keys) == len(set(keys)), keys)
check("bibtex collision suffixed",
      "Armstrong2006" in keys and "Armstrong2006b" in keys, keys)
check("bibtex strips braces from titles", "{braces}" not in bib)
check("bibtex notes the fulltext file", "fulltext: texts/" in bib)

# --------------------------------------------------------------------------- #
print("\n[D1 sql executes and round-trips]")
d1 = sqlite3.connect(":memory:")
d1.executescript(open("d1_schema.sql").read())
d1.executescript(open("d1_schema.sql").read())   # schema is re-runnable
d1.executescript(open("d1_data.sql").read())
d1.executescript(open("d1_data.sql").read())     # data is re-runnable
d1.row_factory = sqlite3.Row
check("items loaded", d1.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4)
check("apostrophe survived escaping",
      d1.execute("SELECT title FROM items WHERE key='wp:posts:1'").fetchone()[0]
      == "Rand's Ethics {braces}")
check("authors normalized",
      d1.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 3)
check("author position recorded",
      d1.execute("SELECT name FROM authors WHERE item_key='wp:posts:2' AND position=1"
                 ).fetchone()[0] == "Aristotle")
check("tags normalized, no duplicates on re-run",
      d1.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 3,
      d1.execute("SELECT COUNT(*) FROM tags").fetchone()[0])
check("tag query works",
      d1.execute("SELECT item_key FROM tags WHERE tag='values'").fetchone()[0]
      == "substack:teaching-values")
check("bodies embedded with --d1-include-text",
      d1.execute("SELECT COUNT(*) FROM texts").fetchone()[0] == 2)
check("embedded body has no metadata header",
      "Text sha256:" not in d1.execute(
          "SELECT body FROM texts WHERE item_key='wp:posts:1'").fetchone()[0])
check("text columns present in items",
      d1.execute("SELECT text_status FROM items WHERE key='substack:teaching-values'"
                 ).fetchone()[0] == "paywalled-preview")
d1.close()

# a body-free D1 export is the default
osi_export.export_d1(conn, data_path="d1_nobody.sql", include_text=False)
check("default D1 export omits bodies",
      "INSERT OR REPLACE INTO texts" not in open("d1_nobody.sql").read())

osi_export.summarize(conn)
conn.close()


# --------------------------------------------------------------------------- #
print("\n[auth: cookie scoping]")
check("no cookies by default", H.have_substack_cookies() is False)
note = H.load_cookies(cookie_str="substack.sid=abc123; other=x")
check("cookie header parsed", "2 cookies" in note, note)
check("cookies detected", H.have_substack_cookies() is True)
_jar = {c.name: c.domain for c in H.SESSION.cookies}
check("cookies pinned to the substack domain",
      all("theobjectivestandard" in d for d in _jar.values()), _jar)
check("subscriber cookie never sent to objectivestandard.org",
      H.SESSION.cookies.get_dict(domain="objectivestandard.org") == {},
      H.SESSION.cookies.get_dict(domain="objectivestandard.org"))
check("cookie value reaches the substack host",
      H.SESSION.cookies.get_dict(domain=".theobjectivestandard.com").get("substack.sid")
      == "abc123")
check("missing cookie file is survivable",
      H.load_cookies(cookie_file="/nonexistent/cookies.txt") == "")
H.SESSION.cookies.clear()
check("cleared", H.have_substack_cookies() is False)

# previews are retried only when asked
_calls = []
conn2 = H.connect("prev.sqlite")
H.save(conn2, {"key": "substack:p", "source": "substack", "source_id": "1",
               "url": "https://www.theobjectivestandard.com/p/p", "slug": "p",
               "title": "Paywalled", "authors": "A B", "published": "2010-01-01",
               "year": 2010, "wordcount": 3000})
conn2.commit()
T.migrate(conn2)
conn2.execute("UPDATE items SET text_status='paywalled-preview', "
              "text_path='texts/x.txt' WHERE key='substack:p'")
conn2.commit()


def _stub(url, *, params=None, verify=True, expect_json=False):
    _calls.append(url)
    return None


T.harvest_text(conn2, _stub, substack_base=H.SUBSTACK, text_dir="texts2")
check("previews left alone by default", len(_calls) == 0, _calls)
T.harvest_text(conn2, _stub, substack_base=H.SUBSTACK, text_dir="texts2",
               retry_previews=True)
check("previews retried when asked", len(_calls) > 0, _calls)
conn2.close()

print("\n[cli: wizard defaults]")
import builtins
_input = builtins.input
builtins.input = lambda *a: ""            # user just holds Enter
check("empty answer takes the y default", H._ask("q") is True)
check("empty answer takes the n default", H._ask("q", "n") is False)
builtins.input = lambda *a: "n"
check("explicit no", H._ask("q") is False)
builtins.input = lambda *a: (_ for _ in ()).throw(EOFError())
check("EOF takes the default", H._ask("q") is True and H._ask("q", "n") is False)
builtins.input = _input

print("\n[cli: bare invocation runs everything]")
called = []


def _record(name, ret=0):
    def f(*a, **k):
        called.append(name)
        return ret
    return f


_orig = {n: getattr(H, n) for n in
         ("harvest_substack_sitemap", "harvest_substack_api", "harvest_wp",
          "harvest_legacy", "link_issues", "connect")}
_orig_text = T.harvest_text
_orig_manifest = T.write_manifest
for n in ("harvest_substack_sitemap", "harvest_substack_api", "harvest_wp",
          "harvest_legacy", "link_issues"):
    setattr(H, n, _record(n))
T.harvest_text = _record("text", {"ok": 0, "paywalled-preview": 0,
                                  "unavailable": 0, "skipped": 0})
T.write_manifest = _record("manifest")
H.connect = lambda path=None: _orig["connect"]("cli.sqlite")

_argv, _isatty = sys.argv, sys.stdin.isatty
sys.argv = ["osi_harvest.py"]
sys.stdin.isatty = lambda: False        # non-interactive: run everything
try:
    H.main()
finally:
    sys.argv, sys.stdin.isatty = _argv, _isatty
    for n, fn in _orig.items():
        setattr(H, n, fn)
    T.harvest_text, T.write_manifest = _orig_text, _orig_manifest

check("bare run does sitemap, api, wp, issues, text",
      called == ["harvest_substack_sitemap", "harvest_substack_api", "harvest_wp",
                 "link_issues", "text", "manifest"], called)
check("bare run skips the legacy TLS-broken site",
      "harvest_legacy" not in called)
for f in ("osi_citations.csv", "osi_zotero.csl.json", "osi_citations.bib",
          "d1_schema.sql", "d1_data.sql"):
    check(f"bare run wrote {f}", os.path.exists(f))

# --------------------------------------------------------------------------- #
print("\n[implausible upstream dates]")
# Substack serves post_date "0002-09-01T15:34:15.000Z" for three real TOS posts.
# Its own data, not a parse artifact -- so the only safe handling is to drop the
# date entirely. year_of already guarded this at harvest time, but `published`
# kept the bad string and every consumer that re-parsed it reintroduced year 2.
BAD = "0002-09-01T15:34:15.000Z"

check("year_of rejects year 2", H.year_of(BAD) is None)
check("sanitize_published nulls date and year together",
      H.sanitize_published(BAD) == (None, None), H.sanitize_published(BAD))
check("sanitize_published passes a good date through",
      H.sanitize_published("2024-09-01T15:34:15.000Z")
      == ("2024-09-01T15:34:15.000Z", 2024))
check("sanitize_published tolerates None", H.sanitize_published(None) == (None, None))
for edge, want in (("1989-01-01T00:00:00Z", None), ("1990-01-01T00:00:00Z", 1990),
                   ("2100-01-01T00:00:00Z", 2100), ("2101-01-01T00:00:00Z", None)):
    check(f"year_of boundary {edge[:4]}", H.year_of(edge) == want, H.year_of(edge))

# the export path must not re-materialise it even from an already-dirty database
check("_date_parts drops an implausible published",
      osi_export._date_parts(BAD, None) == [], osi_export._date_parts(BAD, None))
check("_date_parts still handles a good published",
      osi_export._date_parts("2006-05-20T00:00:00Z", 2006) == [[2006, 5, 20]])
check("_date_parts falls back to a plausible year column",
      osi_export._date_parts(None, 2012) == [[2012]])

# filenames must not sort two millennia early
check("text_filename refuses year 2",
      T.text_filename({"published": BAD, "year": None, "source": "substack",
                       "source_id": "1", "title": "T", "authors": "A B"})
      .startswith("undated_"),
      T.text_filename({"published": BAD, "year": None, "source": "substack",
                       "source_id": "1", "title": "T", "authors": "A B"}))

# end to end: a dirty row through every exporter
_dc = sqlite3.connect(":memory:")
_dc.row_factory = sqlite3.Row
_dc.executescript(H.SCHEMA)
H.save(_dc, {"key": "substack:bad", "source": "substack", "source_id": "9",
             "url": "https://example.com/p/bad", "slug": "bad", "title": "Bad Date",
             "authors": "Ann Author", "published": BAD, "year": H.year_of(BAD)})
_dc.commit()
osi_export.export_csl(_dc, "bad.csl.json")
_bad = json.load(open("bad.csl.json"))
check("CSL omits issued entirely for an implausible date",
      "issued" not in _bad[0], _bad[0].get("issued"))
osi_export.export_bibtex(_dc, "bad.bib")
_bib = open("bad.bib").read()
check("BibTeX carries no 0002 date", "0002" not in _bib)
osi_export.export_csv(_dc, "bad.csv")
check("CSV carries no 0002 date", "0002" not in open("bad.csv").read())
osi_export.export_d1(_dc, "bad.schema.sql", "bad.data.sql")
check("D1 SQL carries no 0002 date", "0002" not in open("bad.data.sql").read())

print()
if FAIL:
    print(f"FAILED ({len(FAIL)}): " + ", ".join(FAIL))
    sys.exit(1)
print("ALL CHECKS PASSED")
