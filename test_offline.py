#!/usr/bin/env python3
"""Offline regression tests for osi_harvest: parsing, upsert, issue-linking,
and the three export formats. No network required.

    python test_offline.py
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osi_harvest as H

os.chdir(tempfile.mkdtemp(prefix="osi-test-"))
c = H.connect("t.sqlite")

# edge: empty rendered excerpt -> strip_tags returns "" -> clip must not raise
assert H.clip(H.strip_tags(({"rendered": ""}).get("rendered")), 400) is None
assert H.clip(H.strip_tags("<p>hi &amp; bye</p>"), 4) == "hi &"

# edge: corrupt + valid dates
assert H.year_of("0002-08-12") is None
assert H.year_of("2006-05-20T00:00:00Z") == 2006
assert H.year_of(None) is None

# byline parsing both ways
assert H.authors_from([{"name":"Craig Biddle"}], "By Someone Else") == "Craig Biddle"
assert H.authors_from([], "By Ari Armstrong") == "Ari Armstrong"
assert H.authors_from(None, "<p>By Ari Armstrong and Craig Biddle</p>") == "Ari Armstrong; Craig Biddle"
assert H.authors_from(None, "A subtitle with no byline") == ""

# issue labels
assert H.issue_label("The Fall 2026 Issue of TOS Is Published!") == "Fall 2026"
assert H.issue_label("The Autumn 2011 Issue") == "Fall 2011"
assert H.issue_label("Winter 2006-07 Issue of TOS Is Published!") == "Winter 2006-07"
assert H.issue_label("nothing here") is None

# sitemap-style row (no date), then API row upserting over it
H.save(c, {"key":"substack:teaching-values","source":"substack","source_id":"teaching-values",
           "url":"https://www.theobjectivestandard.com/p/teaching-values",
           "slug":"teaching-values","title":"Teaching Values in the Classroom",
           "year":2006,"item_type":"newsletter"})
ann = {"body_html":'<a href="/p/teaching-values">Teaching Values</a><a href="https://www.theobjectivestandard.com/p/other">Other</a>'}
H.save(c, {"key":"substack:fall-2026-issue","source":"substack","source_id":"1",
           "url":"u","slug":"fall-2026-issue",
           "title":"The Fall 2026 Issue of TOS Is Published!","published":"2026-09-01T00:00:00Z",
           "year":2026,"access":"everyone","item_type":"newsletter",
           "issue_label":"Fall 2026"}, raw=ann)
H.save(c, {"key":"substack:teaching-values","source":"substack","source_id":"99",
           "url":"https://www.theobjectivestandard.com/p/teaching-values",
           "slug":"teaching-values","title":"Teaching Values in the Classroom",
           "subtitle":"By Ari Armstrong","authors":"Ari Armstrong",
           "published":"2006-05-20T00:00:00Z","year":2006,"item_type":"newsletter",
           "access":"only_paid","section":"Education","tags":"education; values","wordcount":3200},
       raw={"id":99})
# corrupt-date row + single-name author + same-year-same-author (bibkey collision)
H.save(c, {"key":"wp:posts:1","source":"wp","source_id":"1","url":"w1","slug":"s1",
           "title":"OSI Post One","subtitle":None,"authors":"Ari Armstrong",
           "published":"2006-11-02T00:00:00Z","year":2006,"item_type":"wp-post",
           "access":"everyone","section":"posts"})
H.save(c, {"key":"wp:posts:2","source":"wp","source_id":"2","url":"w2","slug":"s2",
           "title":"No Date {braces} Post","authors":"Aristotle","item_type":"wp-post",
           "access":"everyone","section":"posts"})
c.commit()

H.link_issues(c)
lab = c.execute("SELECT issue_label FROM items WHERE key='substack:teaching-values'").fetchone()[0]
assert lab == "Fall 2026", lab
# upsert must not clobber a populated field with NULL
row = c.execute("SELECT authors,access,tags,wordcount FROM items WHERE key='substack:teaching-values'").fetchone()
assert tuple(row) == ("Ari Armstrong","only_paid","education; values",3200), row

H.export_csv(c); H.export_csl(c); H.export_bibtex(c); H.summarize(c)

csl = json.load(open("osi_citations.json"))
assert all("id" in i for i in csl)
tv = [i for i in csl if i["id"]=="substack:teaching-values"][0]
assert tv["issued"] == {"date-parts":[[2006,5,20]]}, tv["issued"]
assert tv["author"] == [{"given":"Ari","family":"Armstrong"}]
assert tv["type"] == "article-magazine"
nd = [i for i in csl if i["id"]=="wp:posts:2"][0]
assert "issued" not in nd and nd["author"]==[{"literal":"Aristotle"}], nd
bib = open("osi_citations.bib").read()
keys = sorted(l.split("{",1)[1].rstrip(",\n") for l in bib.splitlines() if l.startswith("@article"))
assert len(keys)==len(set(keys)) and "Armstrong2006" in keys and "Armstrong2006b" in keys, keys
assert "{braces}" not in bib
print("\nALL SMOKE CHECKS PASSED  (bibkeys: %s)" % keys)
