# osi — OSI / TOS citation harvester

Builds a citation database of everything published by the **Objective Standard
Institute** (`objectivestandard.org`) and ***The Objective Standard***
(`theobjectivestandard.com`), 2006 to present.

The key finding from reconnaissance: the Substack migration brought the whole
2006–present back catalogue with it, and the paywall withholds only article
*bodies* — every citation field (title, byline, date, canonical URL, word count,
section, tags) comes back in public JSON. So no authentication is needed, and no
Wayback reconstruction. See [docs/SOURCES.md](docs/SOURCES.md) for the endpoint
inventory, verified counts, and the blockers.

## Run it

```bash
pip install -r requirements.txt
python osi_harvest.py                    # sources A+B+C+E; ~10 min at 1s delay
```

Must be run from your own machine — the agent sandbox's egress allowlist blocks
both target hosts.

| Command | Does |
|---|---|
| `python osi_harvest.py` | Substack sitemaps + archive API + OSI WordPress + issue linking |
| `python osi_harvest.py --only substack` | TOS only |
| `python osi_harvest.py --only wp` | objectivestandard.org only |
| `python osi_harvest.py --legacy-insecure` | also crawl `archive.theobjectivestandard.com` with TLS verification **off** (its cert doesn't match its hostname) |
| `python osi_harvest.py --export-only` | re-export from the existing SQLite, no network |
| `python osi_harvest.py --delay 2.0` | slow down (Substack 429s on bursts) |

## Outputs

| File | Format |
|---|---|
| `osi_citations.sqlite` | normalized `items` table + `raw` JSON per item |
| `osi_citations.csv` | flat export |
| `osi_citations.json` | CSL-JSON — import straight into Zotero, or use with pandoc |
| `osi_citations.bib` | BibTeX |

Upserts are idempotent and field-wise non-destructive (a later pass never
overwrites a populated field with `NULL`), so re-running refreshes rather than
rebuilds. Outputs are gitignored.

## Sources

| Pass | Source | Coverage |
|---|---|---|
| A | `theobjectivestandard.com/sitemap/<YEAR>` | URL+title enumeration, 2006–2026 |
| B | `/api/v1/archive?sort=new&offset=N&limit=50` | metadata spine, ~2,400 posts |
| C | `objectivestandard.org/wp-json/wp/v2/*` | 315 blog posts (2021→) + podcasts, conferences, courses, fellows |
| D | `archive.theobjectivestandard.com` | optional; pre-Substack site, TLS hostname mismatch |
| E | issue-announcement posts | stamps `issue_label` (e.g. `Fall 2026`) onto each issue's articles |

## Tests

```bash
python test_offline.py
```

Covers parsing, upsert precedence, issue linking and all three exports against
synthetic rows, including the real edge cases found in the data (empty excerpt,
corrupt `0002-08-12` date, subtitle-only byline, single-name author, BibTeX key
collision). No network.

## Politeness

One request at a time, 1s default delay, exponential backoff honouring
`Retry-After`. Set a real contact address in the `UA` constant before a long run.
