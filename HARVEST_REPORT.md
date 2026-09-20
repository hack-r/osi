# Harvest verification report

Database: `whoneedsit` (`82c55bae-2ae2-4502-bd8e-1a790ba981c4`), verified against the
remote D1 instance on 2026-09-20. All figures are live query results, not local
estimates.

## Row counts

| Table | Rows | Note |
|---|---|---|
| `articles` | **3048** | 2961 OSI + 87 PROMETHEUS |
| `authors` | 215 | deduplicated on `name_norm` |
| `tags` | 692 | deduplicated on `tag_norm` |
| `article_authors` | 2403 | |
| `article_tags` | 1292 | |
| `orgs` | 85 | was 41; +44 grantee organisations |
| `people` | 133 | was 108; +25 grantee individuals |
| `relationships` | 224 | was 144; +80 `Grantee` edges |

### `articles` by corpus, platform and date basis

| corpus | platform | date_basis | n | bodies |
|---|---|---|---|---|
| OSI | substack | api_verbatim | 2418 | 288 |
| OSI | substack | sitemap_year | 149 | 53 |
| OSI | substack | unknown | 3 | 2 |
| OSI | wp | api_verbatim | 391 | 318 |
| PROMETHEUS | wp | api_verbatim | 87 | 87 |

OSI total 2961, matching the expected count exactly. Platform split 2570 substack /
391 wp, also as expected. 748 bodies carry a `body_sha256`.

## `pub_date IS NULL`, by date basis

| corpus | date_basis | n |
|---|---|---|
| OSI | sitemap_year | 149 |
| OSI | unknown | 3 |

The 149 are Substack posts enumerated only from a year-sitemap and never matched by the
archive API. The sitemap gives a year and nothing finer, so `pub_year` is set and
`pub_date` stays NULL — no day or month was synthesised.

The 3 `unknown` rows are covered below.

## `pub_year` outside 2006–2026

**0.** This was the point of the date fix.

## The three implausible dates

Substack serves `post_date: "0002-..-.."` for three real TOS posts. This is Substack's
own stored value, not a parse artifact — the rendered page for
`/p/letter-from-tos-reader-burgess-laughlin` returns
`"datePublished": "0002-09-01T15:34:15+00:00"` and displays "Sep 01, 0002", and
`/sitemap/2` returns HTTP 400. No true date is recoverable from any reachable source,
so `pub_date` and `pub_year` are both NULL with `date_basis='unknown'`.

The affected rows are `substack:letter-to-the-editor-how-to-evaluate` (`0002-08-12`),
`substack:letter-from-tos-reader-burgess-laughlin` (`0002-09-01`) and
`substack:six-wistful-winter-poems` (`0002-11-15`). Verbatim upstream values are
preserved in `date_anomalies.csv` and remain untouched in the harvest `raw` table.

The defect that needed fixing was in the exporter, not the harvester: `year_of()`
already range-checked 1990..2100, but `_date_parts()` re-parsed the raw `published`
string with no such check, so `int("0002")` produced year 2 in `osi_zotero.csl.json`.
The same string also reached the BibTeX `date` field, the CSV, `d1_data.sql` and three
filenames in `texts/`. Blast radius was exactly 3 rows — so it did affect rows beyond
the one obvious outlier, but only two more.

## Duplicate `title_norm` within a corpus

| corpus | duplicate groups | rows involved |
|---|---|---|
| OSI | 19 | 38 |
| PROMETHEUS | 0 | 0 |

19 groups of exactly 2. This is the expected shape: TOS reposted content from the
WordPress site onto Substack, so a title appears once per platform. No group has more
than 2 members, which is what you would want if the only cause is cross-posting.

## Authors with `person_id IS NULL`

192 of 215 authors are unlinked; 23 matched. Matching is deliberately conservative —
exact normalised full name, or exact `first last`, and a normalised name shared by two
people is discarded rather than guessed.

The unlinked remainder is dominated by institutional and placeholder bylines that have
no `people` row to match and should not get one: `Admin Tos`, `Authors Various`,
`The Editors`, and similar. Genuine individuals among the unlinked are simply absent
from `people`; leaving `person_id` NULL is correct until they are added. No unlinked
author was found that has an obvious exact `people.name_full` counterpart.

## URL re-fetch sample

20 URLs drawn at random from `articles` and re-fetched. **19 confirmed 200**; one
produced no response within the timeout and is neither a confirmed pass nor a confirmed
failure. Sample spanned both corpora and both OSI platforms.

## Prometheus harvest provenance

Site reconnaissance was done before any parser was written. It is WordPress with a live
REST API (`/wp-json/wp/v2/posts` → `x-wp-total: 70`), but `/wp-json/wp/v2/users`
returns `401 rest_user_cannot_view`, so author display names were read from the
`/author/craig/` and `/author/pf/` archive pages rather than an API field. The grantees
custom post type is not REST-exposed (`/wp/v2/grantees` → 404) and was scraped from
HTML.

`robots.txt` disallows only `/wp-admin/`. Requests were rate-limited to 1/second with
retry and exponential backoff, identifying as
`Prometheus-citation-harvester/1.0 (bibliographic research; contact: ...)`.

**176 requests, all 200**, recorded in `prometheus_runlog.csv` with status codes.
Every URL written to the database was fetched and confirmed 2xx — `load_prometheus.py`
intersects against the run log and rejected 0 rows. Where a URL redirected, the final
2xx URL is what was stored, not the requested one. No URL was reconstructed from a
title or slug pattern.

87 articles (70 posts + 17 pages) is comfortably within the expected range, so the
pagination/term-archive guard (`MAX_EXPECTED = 200`) did not trigger.

## Relationship edges

80 new `Grantee` edges to org 8, all `confidence='verified'`: 48 org→org and 32
person→org. Each carries the verbatim page text as `evidence` and the fetched
`source_url`. One grantee already had an edge and was not duplicated.

Source: all 81 `https://prometheusfdn.org/grantees/<slug>/` pages, enumerated from
`grantees-sitemap.xml` and each individually fetched with a 200. This is what made the
full list available — the `/grantees/` index page summarises the European and Middle
East/Asia entries as "37 additional organisations" and "10 additional" without naming
them, while the sitemap lists every one.

**Naming:** grantee names are stored exactly as published, which means both spellings
occur. `Ayn Rand Centre UK` uses the British "Centre" and matched existing org 4;
`Ayn Rand Center Japan`, `Center Israel`, `Center Georgia`, `Center Armenia`,
`Center Belarus`, `Center Russia`, `Center Ukraine`, `Center Latin America`,
`Center Europe` and `Center Holland` use "Center". `Artistelion Centre for Reason and
Objectivism` and `Berlin Center for Individualist Thought` likewise differ. These are
separate organisations, not spelling errors to be normalised — but a query matching on
name must account for both forms.

### IRS Form 990

Attached to the existing Prometheus → OSI `funds` edge as org-level figures only:
Prometheus Foundation **EIN 27-1456655** (Crystal Bay NV, 501(c)(3), ruling 2010-09),
filings with data FY2011–2015 and FY2019–2023; FY2023 revenue $3,800,194 and assets
$68,229,080; FY2019 revenue $18,937,877. Objective Standard Institute is
**EIN 84-3575279** (Glen Allen VA).

Per-grantee amounts are **not** attached. ProPublica's structured API returns no
grants-paid figure for this organisation, and grantee-level detail exists only in
Schedule I inside the filing PDFs. Rather than risk misread figures, the edge records
fiscal years and org-level totals and says so.

### Jackson Upmann (person 44)

**Retired, not deleted.** `https://objectivestandard.org/team/` was fetched on
2026-09-20 (100,078 bytes) and contains zero occurrences of "Upmann". The edge's
`confidence` is now `retired` with that finding quoted in `evidence`. No departure date
is published anywhere I could reach, so `end_date` remains NULL rather than invented.

## Known gaps

- One of the 20 sampled URLs did not respond in time and is unconfirmed either way.
- The grantee person/organisation split is heuristic. `PragerU`,
  `Ideas Beyond Borders` and `Universidad Francisco Marroquín` were reclassified to
  organisations after review. One-person projects such as `Ricardo's Time` and
  `The Rubin Report` are recorded as organisations, which is how the source presents
  them, though each is effectively a single individual.
- 1292 `article_tags` against an expected 1293: one article carried the same tag twice
  upstream and the composite primary key collapsed it.
