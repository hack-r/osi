#!/usr/bin/env python3
"""Map the existing OSI/TOS harvest into the canonical D1 schema.

Reads the harvest already in `osi_citations.sqlite` -- this does not re-fetch
anything -- and emits `osi_load.sql` plus `osi_bodies.sql`.

    python load_osi.py
"""

import argparse
import json
import os
import sqlite3

import canonical as C
import osi_export
import osi_harvest as H

ORG_ID = 3
CORPUS = "OSI"


def date_fields(r) -> tuple:
    """(pub_date, pub_year, date_basis) by genuine provenance.

    - a stored `published` came from the Substack archive API or the WP REST API
      -> api_verbatim
    - no `published` but a year -> that year came from a Substack year-sitemap,
      so sitemap_year, and the day/month stay NULL rather than being invented
    - neither -> unknown (the three posts whose upstream date was implausible;
      see fix_dates.py and date_anomalies.csv)
    """
    if r["published"]:
        return r["published"], r["year"], "api_verbatim"
    if r["year"]:
        return None, r["year"], "sitemap_year"
    return None, None, "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=H.DB)
    ap.add_argument("--out", default="osi_load.sql")
    ap.add_argument("--bodies", default="osi_bodies.sql")
    ap.add_argument("--people", default="ref/people.json")
    args = ap.parse_args()

    people = json.load(open(args.people, encoding="utf-8")) \
        if os.path.exists(args.people) else []
    matcher = C.PersonMatcher(people)
    print(f"person matcher: {len(people)} people, "
          f"{sum(1 for v in matcher.index.values() if v)} usable name keys")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    load = C.Load(CORPUS, ORG_ID)

    for r in conn.execute("SELECT * FROM items ORDER BY key"):
        pub_date, pub_year, basis = date_fields(r)
        body = None
        if r["text_status"] == "ok" and r["text_path"] and os.path.exists(r["text_path"]):
            body = osi_export._read_body(r["text_path"])
        load.add(
            url=r["url"],
            canonical_url=r["url"],
            slug=r["slug"],
            title=r["title"],
            subtitle=r["subtitle"],
            # theobjectivestandard.com is the Substack journal; objectivestandard.org
            # is the org's WordPress site.
            platform="substack" if r["source"] == "substack" else "wp",
            pub_date=pub_date, pub_year=pub_year, date_basis=basis,
            access=r["access"] or "unknown",
            text_status=r["text_status"],
            word_count=r["wordcount"] if r["wordcount"] else r["text_words"],
            excerpt=r["subtitle"],
            source=r["key"],
            source_url=r["url"],
            body=body,
            authors=r["authors"], tags=r["tags"], matcher=matcher)

    print(load.summary())
    for u, why in load.skipped:
        print(f"  SKIPPED {u}: {why}")

    load.write(args.out)
    load.write_bodies(args.bodies)
    print(f"wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB), "
          f"{args.bodies} ({os.path.getsize(args.bodies)/1e6:.1f} MB)")

    linked = sum(1 for _, (_, pid) in load.authors.items() if pid)
    print(f"authors matched to a people row: {linked}/{len(load.authors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
