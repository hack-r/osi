# OSI / TOS Citation Database — Source Reconnaissance

Reconnaissance date: 2026-09-19. All counts verified against live endpoints on
that date.

## Headline finding

The migration of *The Objective Standard* onto Substack carried the **entire
back catalogue with it**. The Substack archive returns posts dated from **May
2006** (the journal's first issue) through the present. There is no need to
reconstruct the pre-2024 material from a dead WordPress site or from the Wayback
Machine: one API and one sitemap cover roughly twenty years.

The paywall is not a blocker for a citation database. Older posts are marked
`audience: "only_paid"`, but title, subtitle (which carries the byline on
migrated posts), publication date, canonical URL, word count, section and tags
are all returned in the public archive JSON regardless of access level. Only the
article body is withheld.

## Source inventory

| # | Source | Endpoint | Coverage | Est. items | Access |
|---|--------|----------|----------|-----------|--------|
| A | TOS Substack year sitemaps | `www.theobjectivestandard.com/sitemap/<YEAR>` for 2006–2026 | full URL + title enumeration, 21 pages | 2006 alone = 81 | open |
| B | TOS Substack archive API | `/api/v1/archive?sort=new&offset=N&limit=50` | full metadata spine, 2006-05 → present | ~2,400–2,430 | open metadata; bodies paywalled pre-~2024 |
| C | OSI WordPress REST | `objectivestandard.org/wp-json/wp/v2/{posts,podcasts,conferences,courses,fellows}` | org blog + media, 2021-07 → present | 315 blog posts (3×100 + 15) | open |
| D | Legacy TOS WordPress | `archive.theobjectivestandard.com` | pre-Substack site, incl. `/issues/` quarterly TOCs and author pages | unknown | **unreachable** — see below |
| E | Quarterly issue grouping | "The *Season Year* Issue of TOS Is Published!" announcement posts on the Substack | issue-level tables of contents, one post per issue | ~80 issues | open |

Verified spot checks: Substack offset 2400 still returns posts dated 2006-05-20
("Teaching Values in the Classroom"); offset 3000 returns `[]`. Substack
`/sitemap` lists year pages 2006 through 2026 inclusive. WordPress
`?per_page=100&page=4` returns 15 items, oldest 2021-07-17, so the org blog is
315 posts and is **separate content**, not a duplicate of TOS.

## Blockers found

1. **`archive.theobjectivestandard.com` serves a TLS certificate that does not
   match its hostname.** Both a plain HTTPS client and the fetch tooling fail at
   `robots.txt` with `CERTIFICATE_VERIFY_FAILED: Hostname mismatch`. The host
   resolves (A record 18.235.162.163) and is presumably still serving, but
   cannot be read without disabling verification. Because source B covers the
   same articles, this is a low-priority gap — it matters only for the
   `/issues/` pages and author index pages.
2. **Wayback Machine is blocked** from the agent environment (`SITE_BLOCKED`),
   so the usual CDX fallback for source D is unavailable there. It should work
   from a normal machine.
3. **Substack rate-limits** the archive API (HTTP 429 after a modest burst).
   The harvester backs off exponentially and honours `Retry-After`.
4. **Scripted HTTP from the agent's cloud sandbox is blocked by egress policy** —
   only an allowlist (GitHub, PyPI, npm) is reachable. The harvester therefore
   has to be run locally, not in the session container.
5. **`publishedBylines` is empty on migrated posts.** The author is in the
   `subtitle` field as "By Ari Armstrong". The harvester parses both, preferring
   the structured byline and falling back to the subtitle regex.
6. **At least one corrupt date** exists in the migrated data (a post returned
   `0002-08-12`). Year values outside 1990–2100 are dropped rather than trusted.

## Full text (pass F)

Article bodies are retrieved per source, cheapest reliable route first:

| Source | Route |
|---|---|
| `wp` | `content.rendered` from the REST payload already stored in `raw` — no extra request |
| `substack` | archive payload's `body_html` if present; then two candidate post-detail API endpoints; then the article page, read from the `window._preloads` JSON blob, else from the article container |
| `legacy` | the article page, generic extraction |

Neither Substack post-detail endpoint shape is documented, so both are **probed
rather than assumed**, and whichever answers is recorded per article in
`items.text_strategy`. The HTML fallback covers both failing.

Paywalled posts return a preview. Since the archive API reports a `wordcount`
for every post, a body under 40% of the expected length is recorded as
`paywalled-preview`, not silently stored as though complete. Under 40 words is
`unavailable` and no file is written.

## Tooling

- `osi_harvest.py` — entry point. Passes A/B/C/E, optional D behind
  `--legacy-insecure`, pass F, and the exports. A bare invocation asks once and
  then does everything; `--all` skips the question. Idempotent upserts and a
  resumable text pass, so re-running refreshes rather than rebuilds.
- `osi_text.py` — HTML→text (stdlib `HTMLParser`, no bs4), filename
  construction, the `.txt` files, pass F.
- `osi_export.py` — CSV, Zotero CSL-JSON, BibTeX, Cloudflare D1 SQL.
- `test_offline.py` — 108 offline checks, including executing the generated D1
  SQL against a fresh SQLite database to prove it loads and round-trips.

## Open questions

- Whether any pre-2024 TOS article failed to migrate to Substack. Comparing the
  sitemap enumeration (A) against the API enumeration (B) catches internal
  inconsistency; catching a migration loss needs source D or a print index.
- Whether the print quarterly should be cited with page numbers. Nothing online
  carries pagination; that needs the physical issues or a library database
  record. WorldCat record OCLC 62468522 lists Glen Allen Press, LLC as publisher
  from 2006 but exposes no ISSN in its public page.
- Whether Craig Biddle's personal Substack and the OSI "The Future Today"
  newsletter should be in scope as separate publications.
