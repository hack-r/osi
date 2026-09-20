#!/usr/bin/env python3
"""Map the Prometheus Foundation harvest into the canonical D1 schema.

Emits prometheus_load.sql, prometheus_bodies.sql, prometheus_citations.csv,
prometheus_zotero.csl.json and prometheus_citations.bib.

Dates come from the WP REST `date_gmt` field -> api_verbatim. Author display
names came from the rendered author archive pages (/wp/v2/users is 401), but
that affects the author field, not the date basis.

Only URLs present in prometheus_runlog.csv with a 2xx are eligible.
"""
import csv, json, os, sqlite3
import canonical as C

ORG_ID, CORPUS = 8, "PROMETHEUS"

ok = {r["url"] for r in csv.DictReader(open("prometheus_runlog.csv"))
      if r["status"].startswith("2")}
print(f"run log: {len(ok)} URLs confirmed 2xx")

conn = sqlite3.connect("prometheus.sqlite"); conn.row_factory = sqlite3.Row
load = C.Load(CORPUS, ORG_ID)
people = json.load(open("ref/people.json"))
matcher = C.PersonMatcher(people)

rejected = 0
for r in conn.execute("SELECT * FROM items ORDER BY key"):
    if r["url"] not in ok:          # invariant: never emit an unfetched URL
        rejected += 1; print(f"  REJECT (not 2xx in run log): {r['url']}"); continue
    load.add(url=r["url"], canonical_url=r["url"], slug=r["slug"], title=r["title"],
             platform="wp", pub_date=r["published"], pub_year=r["year"],
             date_basis="api_verbatim", access="everyone",
             text_status="ok" if (r["body"] or "").split() else "unavailable",
             word_count=len((r["body"] or "").split()), excerpt=r["excerpt"],
             subtitle=r["excerpt"], source=r["key"], source_url=r["url"],
             body=r["body"] or None, authors=r["author"] or "", matcher=matcher)

print(load.summary()); print(f"rejected: {rejected}")
load.write("prometheus_load.sql"); load.write_bodies("prometheus_bodies.sql")

# citation exports, same shape as the OSI ones
with open("prometheus_citations.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["key","url","slug","title","excerpt","author","published","year",
                "ptype","word_count","corpus","org_id"])
    for r in conn.execute("SELECT * FROM items ORDER BY published"):
        if r["url"] in ok:
            w.writerow([r["key"],r["url"],r["slug"],r["title"],r["excerpt"],r["author"],
                        r["published"],r["year"],r["ptype"],len((r["body"] or "").split()),
                        CORPUS,ORG_ID])

csl, bib, used = [], [], set()
for r in conn.execute("SELECT * FROM items ORDER BY published"):
    if r["url"] not in ok: continue
    names = [{"literal": r["author"]}] if r["author"] else []
    if r["author"] and " " in r["author"]:
        p = r["author"].split(); names = [{"given":" ".join(p[:-1]),"family":p[-1]}]
    item = {"id": r["key"], "type": "post-weblog", "title": r["title"],
            "container-title": "Prometheus Foundation", "URL": r["url"],
            "language": "en", "abstract": r["excerpt"],
            "issued": {"date-parts": C._date_parts_pub(r["published"], r["year"])} if False else None}
    dp = []
    if r["published"] and r["year"]:
        dp = [[r["year"], int(r["published"][5:7]), int(r["published"][8:10])]]
    if dp: item["issued"] = {"date-parts": dp}
    else: item.pop("issued", None)
    csl.append({k: v for k, v in item.items() if v not in (None, "", [], {})})
    last = (r["author"] or "Anon").split()[-1]
    key = f"{last}{r['year'] or 'nd'}"; i = 0
    while key in used: i += 1; key = f"{last}{r['year'] or 'nd'}{chr(96+i)}"
    used.add(key)
    bib.append("@article{%s,\n  title   = {{%s}},\n  author  = {%s},\n"
               "  journal = {Prometheus Foundation},\n  year    = {%s},\n"
               "  date    = {%s},\n  url     = {%s}\n}\n"
               % (key, (r["title"] or "").replace("{","").replace("}",""),
                  r["author"] or "", r["year"] or "", (r["published"] or "")[:10], r["url"]))
json.dump(csl, open("prometheus_zotero.csl.json","w",encoding="utf-8"),
          indent=1, ensure_ascii=False)
open("prometheus_citations.bib","w",encoding="utf-8").write("\n".join(bib))
print(f"wrote prometheus_citations.csv, prometheus_zotero.csl.json ({len(csl)}), "
      f"prometheus_citations.bib ({len(bib)})")
