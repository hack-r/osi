#!/usr/bin/env python3
"""One-time repair for implausible upstream dates already in the database.

Substack serves `post_date: "0002-09-01T15:34:15.000Z"` for three real TOS
posts. That is Substack's own stored value, not a parse artifact -- the rendered
page shows "Sep 01, 0002" and /sitemap/2 returns 400 -- so no true date is
recoverable and the only honest handling is to drop the date.

`year_of` already rejected these at harvest time, so `items.year` was NULL, but
`items.published` kept the bad string and every consumer that re-parsed it
reintroduced year 2 (see `osi_export._date_parts`). `save()` now sanitizes both
together, which fixes future harvests; this script fixes the rows already
stored, and records the verbatim upstream value in `date_anomalies.csv` so the
upstream defect stays auditable rather than silently vanishing.

The full upstream payload also remains in the `raw` table untouched.

    python fix_dates.py [--db osi_citations.sqlite]
"""

import argparse
import csv
import json
import os
import sqlite3

import osi_harvest as H
import osi_text as T

ANOMALIES = "date_anomalies.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=H.DB)
    ap.add_argument("--anomalies", default=ANOMALIES)
    ap.add_argument("--text-dir", default=T.TEXT_DIR)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    bad = [r for r in conn.execute(
        "SELECT key, source, source_id, url, title, published, year, text_path "
        "FROM items WHERE published IS NOT NULL")
        if H.year_of(r["published"]) is None]

    if not bad:
        print("no implausible dates found")
        return 0

    print(f"{len(bad)} row(s) with an implausible upstream date:")
    with open(args.anomalies, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "source", "source_id", "url", "title",
                    "published_upstream_verbatim", "raw_post_date_verbatim",
                    "resolution"])
        for r in bad:
            raw = conn.execute("SELECT body FROM raw WHERE key=?",
                               (r["key"],)).fetchone()
            raw_date = ""
            if raw:
                try:
                    j = json.loads(raw["body"])
                    raw_date = j.get("post_date") or j.get("date_gmt") or j.get("date") or ""
                except (ValueError, AttributeError):
                    raw_date = ""
            print(f"  {r['key']}  published={r['published']}  -> NULL")
            w.writerow([r["key"], r["source"], r["source_id"], r["url"], r["title"],
                        r["published"], raw_date,
                        "pub_date NULL, pub_year NULL; upstream date not recoverable"])
    print(f"wrote {args.anomalies}")

    # rename the text files so they no longer sort two millennia early
    renamed = 0
    for r in bad:
        old_path = r["text_path"]
        if not old_path or not os.path.exists(old_path):
            continue
        row = dict(r)
        row["published"], row["year"] = None, None
        row["authors"] = conn.execute(
            "SELECT authors FROM items WHERE key=?", (r["key"],)).fetchone()[0]
        new_path = os.path.join(args.text_dir, T.text_filename(row))
        if new_path != old_path:
            os.replace(old_path, new_path)
            conn.execute("UPDATE items SET text_path=? WHERE key=?",
                         (new_path, r["key"]))
            print(f"  renamed {os.path.basename(old_path)}\n       -> {os.path.basename(new_path)}")
            renamed += 1

    conn.executemany("UPDATE items SET published=NULL, year=NULL WHERE key=?",
                     [(r["key"],) for r in bad])
    conn.commit()
    print(f"nulled {len(bad)} date(s), renamed {renamed} text file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
