# Odisha Record of Rights

A scraper and parser for Odisha's [Bhulekh](https://bhulekh.ori.nic.in/) land
records. Each tenant row carries a name, the name of a father or husband, a
jati, a village and a residence, which makes it one of the few Indian sources
that puts a caste and a place beside every individual.

**The records are not in this repository.** `raw/` and `logs/` are ignored. The
scraper is published; the scraped rows are not. They name individuals and give
their caste, and republishing them outside the portal that serves them is a
different act from publishing the code that reads it.

## The pipeline

Three steps, each of which can be re-run without redoing the one before it.

```
python list_locations.py     # districts, tahsils, RI circles, villages -> raw/villages.parquet
python fetch_ror.py          # one gzipped JSONL checkpoint per village -> raw/ror/
python parse_ror.py          # typed tenant rows -> raw/tenants.parquet
```

`fetch_ror.py` checkpoints per khatiyan and skips what it already has, so it can
be stopped and restarted freely. Useful flags:

```
--districts ଗଜପତି,କନ୍ଧମାଳ   crawl these first; the rest follow
--workers 8                  concurrent fetchers
--per-village 40             cap khatiyans per village; omit for a census
```

Checkpoints are written to `raw/ror/district_*/tahsil_*/village_*.jsonl.gz`. The
tahsil level is part of the key because `village_code` repeats across tahsils:
without it, 322 of 476 villages shared a file and the resume check read one
village's khatiyans while skipping another's.

## Reading a tenant cell

The cell is one free-text run with inline markers rather than a set of columns:

```
ଅଇଁଠୁ ଦ୍ଵିବେଦୀ ପି:ବୈଦ୍ୟନାଥ ଦ୍ଵିବେଦୀ ଜା: ବ୍ରାହ୍ମଣ ବା: ନିଜଗାଁ
^ name        ^ father      ^ caste        ^ residence
```

`parse_ror.py` scans for the markers rather than matching one whole-cell regex.
A single regex has to describe every shape the cell can take, and the observed
shapes already include a husband where the father belongs, a comma-suffixed
alias inside the name, a parenthesised gloss inside the caste, and a missing
residence. A whole-cell regex drops every row it does not fully describe,
silently and selectively.

## Tests

```
python -m pytest tests -q
```

## Moving the data here

Nothing is moved automatically, because a fetch may be running. When one is
not:

```
pkill -f fetch_ror.py
mv ~/Documents/GitHub/pranaam/scripts/data-acquisition/odisha_ror/raw  .
mv ~/Documents/GitHub/pranaam/scripts/data-acquisition/odisha_ror/logs .
python fetch_ror.py           # resumes from the checkpoints it finds
```

The fetcher resolves its output directory from its own location, so a running
process cannot survive the move; it must be stopped first and restarted here.

## Who reads this

[last-name-basis](https://github.com/in-rolls/last-name-basis) analysis 09 uses
it to ask whether the village premium found in Bihar travels to another state.
It reads the checkpoints directly and materialises its own table, so it does not
depend on `raw/tenants.parquet` existing.
