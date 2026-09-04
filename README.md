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
