# Odisha Record of Rights

Tenant name, father's name, **caste** and residence, scraped from the Odisha
land-records portal at <https://bhulekh.ori.nic.in>. Built to give a
name-to-religion corpus Indian Christian vocabulary from a region Kerala does
not cover.

## What it is

The Record of Rights (`ଅଧିକାର ଅଭିଲେଖ`) is the settlement record for one
khatiyan — one holding. Its third column is headed

> ୩) ପ୍ରଜାର ନାମ, ପିତାର ନାମ, ଜାତି ଓ ବାସସ୍ଥାନ

*tenant's name, father's name, caste and residence*, and a row reads

> ଅଇଁଠୁ ଦ୍ଵିବେଦୀ **ପି:** ବୈଦ୍ୟନାଥ ଦ୍ଵିବେଦୀ **ଜା:** ବ୍ରାହ୍ମଣ **ବା:** ନିଜଗାଁ

which is Ainthu Dwibedi, father Baidyanath Dwibedi, caste Brahmin, of Nijgaon.
The four fields sit in one free-text cell behind `ପି:` / `ଜା:` / `ବା:`
markers, so the parse is a marker scan rather than a column read.

The portal publishes its own totals, and they are the only external check on
whether an enumeration is complete:

| | portal says | collected |
| --- | --- | --- |
| districts | 30 | 30 |
| tahsils | 317 | 317 |
| RI circles | 2,721 | 2,717 |
| villages | 51,796 | **51,823** (+0.05%, no duplicates) |
| khatiyans | 20,432,717 | crawling |
| tenants | 47,166,788 | crawling |

## What it measures, and what it cannot

**Christian is marked explicitly.** The caste value carries the religion as a
suffix — `ସଉରା ଖ୍ରୀଷ୍ଟିୟାନ` (Saura Christian), `ପାଣ ଖ୍ରୀଷ୍ଟିୟାନ` (Pana
Christian), `କନ୍ଧ ଖ୍ରୀଷ୍ଟିୟାନ`, `ଶବର ଖ୍ରୀଷ୍ଟିୟାନ` — across 27 spelling
variants that a crosswalk has to fold.

**Muslim is never marked.** Four rows in 57,969 carried anything Muslim, in
districts that are 5–7% Muslim. There is a plausible mechanism: conversion to
Christianity strips Scheduled Caste status under the Constitution (Scheduled
Castes) Order, 1950, so it is legally salient and recorded, and nothing
analogous applies to Muslims.

**So an unmarked row is not evidence of not-Muslim.** It is Hindu-or-Muslim,
and any adapter built on this must contribute Hindu and Christian evidence
only, never a Muslim negative.

Two further limits. The records are settlement-era — the sample above was last
published 25/08/1980 — so the caste is as recorded then. And they cover
landholders only, which under-represents the landless in a way that correlates
with the religions being measured.

## Handling

Three traps, each of which cost hours:

- **The khatiyan option value is space-padded to 30 characters and the padding
  is load-bearing.** Trimming it returns a 4 KB error page, not a record.
- **`village_code` is unique only within a tahsil.** Keying checkpoints on
  district plus village merged 322 of 476 files and made the resume check skip
  one village's khatiyans because another's were done.
- **One session serves exactly one RoR.** Reusing it, re-syncing through the
  village or search-type dropdown, posting the `h1` field on the RoR page, and
  the async UpdatePanel postback were all tested and all fail. Each record
  costs a fresh six-request cascade.

The crawl is resumable per khatiyan. A record that yields nothing is written
with `ok` false so the next pass retries it, and a checkpoint truncated by a
killed worker is salvaged rather than skipped.

## Provenance and vintage

Public portal, no authentication, no rate limit observed — zero HTTP 429 across
700k records. Served to a non-Indian IP without geo-blocking.

## Run

```bash
uv run python list_locations.py                 # the frame, ~2 h
uv run python fetch_ror.py --workers 256        # the crawl, resumable
uv run python parse_ror.py                      # -> raw/tenants.parquet
```

`fetch_ror.py --districts` takes district names in crawl order and defaults to
the Christian-heavy ones. Output lands in `raw/`, which is not committed.

## Downstream

`raw/tenants.parquet` is one row per tenant-occurrence-on-a-khatiyan, with the
place columns dictionary-encoded and `raw_cell` kept for audit. Names and
castes are Odia script; the portal has no English rendering, so romanisation
is a separate step and deliberately not done here.

## Unattended crawl and monitoring on macOS

```bash
uv sync --extra dev --group crawl
mkdir -p logs
.venv/bin/supervisord -c supervisord.conf
.venv/bin/supervisorctl -c supervisord.conf status
```

Supervisor runs the existing 256-worker, full-village crawl under `caffeinate`.
Unsuccessful passes return a nonzero status and retry after five minutes;
crashed jobs restart automatically. An exclusive lock prevents overlapping
checkpoint writers. Successful cached khatiyans are reused, and truncated
checkpoints are rewritten atomically from valid readable records.

`crawl_health.py` audits every 30 minutes, caches unchanged file summaries, and
writes `logs/crawl-health.json` plus `logs/crawl-health-history.jsonl`. It checks
composite village keys, duplicate successes, malformed payloads, truncated tails,
progress and disk space. Census parsing checks cover every saved successful record
and every cell. Saved cell counts include village-header matches and must not be
reported as tenant counts. Completion sidecars record enumerated khatiyans,
saved records, and remaining work using exact, untrimmed option values. Printed
labels can collide. Older status files based on labels must be revalidated;
villages without current completion evidence have
unknown completion status. Villages with records are not necessarily complete.

`raw/repair-villages.json`, when present, puts those composite village keys first
on restart. Originals from the September 2026 cleanup are preserved under
`raw/checkpoint-backups/`.

Use `.venv/bin/supervisorctl -c supervisord.conf stop crawl` to pause,
`start crawl` to resume, or `shutdown` to stop both jobs and Supervisor.
The supervisor continues after the terminal closes, but must be started again
after a reboot or logout. A separate four-hour scheduler in the neighboring
`rajasthan-ror` project queues a combined anomaly review into the existing Codex
chat. Keep Codex available for those reviews.


### External storage on this workstation

The local `raw` path links to `/Volumes/Staging/land-records/odisha-ror/raw`.
Raw checkpoints, retained PDFs and all repair backups reside there. Keep Staging
mounted while crawling or auditing; stop both Supervisor jobs before ejecting it.
Code, `.venv`, logs and Supervisor configuration stay in this checkout. Audit
reports distinguish checkout disk space (`free_disk_gb`) from data-volume space
(`data_free_disk_gb`). The September 12 migration verified every file with SHA-256;
its receipts are under `/Volumes/Staging/land-records/`.

### PDF RoRs and census coverage

The supervised crawl has no per-village record cap. It covers the entire village
frame; `--districts` changes priority, not which districts are included. Reports
separate a census of saved data from completion of the statewide crawl.

Some settlement RoRs open `HRoRView.aspx?Param=1` as a PDF. The fetcher follows
that popup in the same session and retains original PDF bytes under `raw/pdf/`.
It reconstructs missing Unicode maps from the embedded Kalinga 6 fonts and their
substitution tables, restores Odia character order, and reads every table page.
The printed village and khatiyan must match the request. Repeated owner blocks
across pages are counted once. Unknown fonts, unresolved glyphs, incomplete
PDFs, and identity mismatches remain failures. Retained PDFs can be reprocessed
without downloading them again; they are never silently overwritten.

Each PDF checkpoint includes its raw path, SHA-256 checksum, page count,
extractor version and printed identity. Census audits check these references
and all extracted cells. A blank caste does not delete an otherwise parsed
owner. An unchanged checkpoint may reuse its complete census summary; changed
checkpoints and changed referenced PDFs are checked again.


The PDF decoder also reads embedded Arial text and recognizes pages containing
only the exact certification footer. Undefined source glyphs and missing owner
tables remain unresolved. A village spelling exception is accepted only from
`raw/pdf-village-aliases.json`, with evidence from independent records; the
complete selected code path and khatiyan checks remain active. Census reports
flag saved records that yield no parsed owners, since capture alone does not
establish complete owner extraction.


HTML capture uses the portal's explicit owner, village and khatiyan spans rather
than searching for a caste marker. This retains owner-field text even when caste
is blank, including institutions and land-status descriptions. Parsed owner
entries are not necessarily people. Legacy checkpoints containing only village
headers are marked incomplete, preserved, and queued for refetch; that correction
reduces the valid-capture count without deleting the recorded responses.

Legacy boundary descriptions containing the village marker are also excluded
from completion and requeued after preserving the original captures. The owner
parser recognizes full Odia labels for father, husband, caste and residence,
including colon and hyphen separators. Ambiguous abbreviated periods are not
treated as field markers, preserving names with initials. Blank name slots remain visible
in the census as records with no named owner entry.
