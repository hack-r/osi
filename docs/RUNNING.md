# Where to run this

Short answer: **run the first pass locally, then move the refresh to GitHub
Actions if you want it scheduled.**

| Environment | Works? | Notes |
|---|---|---|
| Your own machine | **Yes** | Residential IP, you can watch it, cookies stay local. Recommended for the first run. |
| GitHub Actions (hosted runner) | **Yes, with caveats** | Real outbound internet, and the 60–90 min run fits inside the [6-hour job limit](https://docs.github.com/en/actions/reference/limits). See below. |
| Claude's cloud sandbox | **No** | Egress allowlist is GitHub/PyPI/npm only; both target hosts return 403 at CONNECT. |

## Why local first

The first run is the one that discovers whether the probed Substack
post-detail endpoints actually answer — their shapes are undocumented and
unverified. A `403` or `429` from a shared datacenter IP looks exactly like a
code bug from the log, and you would be debugging two things at once. Run it
once locally, confirm `text_status='ok'` counts look sane, and only then
automate it.

Also: Substack rate-limits by IP. Hosted runners come from shared Azure ranges
that other people's scrapers have already been hammering. This is a risk, not a
measured fact — the harvester honours `Retry-After` and backs off either way,
but a run that takes 90 minutes from your laptop may take longer, or stall, from
a runner.

## If you do run it in Actions

Four things need handling, none hard:

1. **State.** The SQLite database is what makes re-runs cheap. A runner starts
   empty, so either commit `osi_citations.sqlite` to the repo, cache it with
   `actions/cache`, or accept a full re-harvest each time.
2. **Outputs.** The text corpus is 100–200 MB. Upload it with
   `actions/upload-artifact` (retained
   [90 days by default](https://docs.github.com/en/actions/tutorials/store-and-share-data),
   configurable up to 400 on a private repo) rather than committing it.
3. **Cookies.** If you have a subscription, put the `cookies.txt` contents in a
   repository secret and write it to a file in the job, then pass
   `--cookie-file`. Never commit it. Note that a browser session cookie will
   expire, so a scheduled run will silently fall back to previews once it does —
   watch the `paywalled-preview` count in the summary for that.
4. **Minutes.** This repo is private, so Actions minutes are billable against
   your plan's included allowance; see
   [billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage).
   A 90-minute monthly refresh is small, a nightly one is not.

`--all` exists for exactly this: it runs every pass with no prompts, which is
what a non-interactive runner needs. (A bare invocation also detects that stdin
is not a terminal and runs everything rather than hanging on a question.)

## Neither environment fixes the paywall

Running in the cloud does not get you paywalled bodies. That is an account
question, not a network one — see
[Paywalled bodies](../README.md#paywalled-bodies).
