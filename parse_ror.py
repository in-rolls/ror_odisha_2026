"""Split fetched RoR tenant cells into typed rows.

Reads only ``raw/ror/`` and writes ``raw/tenants.parquet``, so a parser change
never costs a refetch.

The tenant cell is one free-text run with inline markers rather than a set of
columns:

    ଅଇଁଠୁ ଦ୍ଵିବେଦୀ ପି:ବୈଦ୍ୟନାଥ ଦ୍ଵିବେଦୀ ଜା: ବ୍ରାହ୍ମଣ ବା: ନିଜଗାଁ
    ^ name        ^ father      ^ caste        ^ residence

Splitting scans for the markers rather than matching one whole-cell regex.
A single regex has to describe every shape the cell can take, and the shapes
observed already include a husband (``ସ୍ଵା:``) where the father belongs, a
comma-suffixed alias inside the name, a parenthesised gloss inside the caste
(``କୈବର୍ତ୍ତ (ମାଛଧରା)``), and a missing residence. A whole-cell regex drops
every row it does not fully describe, silently and selectively -- which is how
the Kerala parser lost 39% of its rows, a Christian-heavy slice, to a rule
about marks digits.

Usage:
    uv run python scripts/data-acquisition/odisha_ror/parse_ror.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import unicodedata
import zlib
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROR = HERE / "raw" / "ror"
TENANTS = HERE / "raw" / "tenants.parquet"

# The relation marker is either father or husband; both introduce the same
# field, and which one it was is worth keeping rather than flattening.
FATHER = "ପି"
HUSBAND = "ସ୍ଵା"
CASTE = "ଜା"
RESIDENCE = "ବା"

# The marker must start a word. Without the boundary, ``ଜା`` matches inside
# ``ମୌଜା :`` -- "Mauja", the village line printed above and below every
# tenant row -- and two thirds of the captured cells are village names.
BOUNDARY = r"(?:^|(?<=[\s,;।]))"
SEPARATOR = r"\s*[:：]\s*"

# A caste value longer than this is a runaway match that has swallowed the
# residence or the next tenant, not a real caste.
MAX_CASTE = 40

logger_counts: Counter = Counter()


def marker(label: str) -> re.Pattern:
    """Return a pattern matching one inline field marker.

    Args:
        label: The Odia marker text, without its colon.

    Returns:
        A compiled pattern for the marker and its separator.
    """
    return re.compile(BOUNDARY + re.escape(label) + SEPARATOR)


PATTERNS = {
    "father": marker(FATHER),
    "husband": marker(HUSBAND),
    "caste": marker(CASTE),
    "residence": marker(RESIDENCE),
}


def clean(value: str) -> str:
    """Return a field with collapsed whitespace and no stray punctuation.

    Args:
        value: The raw field text.

    Returns:
        NFC-normalised text with whitespace collapsed.
    """
    return " ".join(unicodedata.normalize("NFC", value).split()).strip(" ,;.")


RELATION = re.compile(
    BOUNDARY + f"(?:{re.escape(FATHER)}|{re.escape(HUSBAND)})" + SEPARATOR
)


def split_cell(cell: str) -> list[dict[str, str]]:
    """Split one tenant cell into one row per person named on it.

    A cell can carry several tenants who share a caste and residence:

        A ପି: B, C ପି: D ଜା: ସଉରା ବା: ଅନରଡ଼ା

    is two people -- A son of B, and C son of D -- not one. Splitting on the
    relation markers gives segments where each middle segment holds the
    previous person's father and the next person's name, separated by the
    last comma.

    Args:
        cell: The cell text as fetched.

    Returns:
        One dict per person, or an empty list if the cell carries no caste.
    """
    people: list[dict[str, str]] = []
    for head, caste, residence in caste_groups(cell):
        people.extend(_people_in(head, caste, residence))
    return people


def caste_groups(cell: str) -> list[tuple[str, str, str]]:
    """Split a cell into ``(names, caste, residence)`` groups.

    A cell can carry more than one caste. Everyone listed before a ``ଜା:``
    shares that caste, then the text continues with another party and another
    ``ଜା:``:

        A ପି: B ଜା: କ୍ଷେତ୍ରିୟ, C ପି: D ଜା: କାମ୍ପ

    The caste value itself is always short and ends at the first residence
    marker or comma, which is what separates it from the next party's names.

    Args:
        cell: The cell text as fetched.

    Returns:
        One tuple per caste group.
    """
    markers = list(PATTERNS["caste"].finditer(cell))
    if not markers:
        return []

    groups: list[tuple[str, str, str]] = []
    head_start = 0
    for index, hit in enumerate(markers):
        head = cell[head_start : hit.start()]
        limit = markers[index + 1].start() if index + 1 < len(markers) else len(cell)
        # Some clerks wrote ``ଜା:,`` -- a comma straight after the marker.
        # Without skipping it the caste terminates at once and comes back
        # empty.
        lead = re.match(r"[\s,;]*", cell[hit.end() : limit])
        offset = hit.end() + (lead.end() if lead else 0)
        tail = cell[offset:limit]

        residence_hit = PATTERNS["residence"].search(tail)
        comma = tail.find(",")
        if residence_hit is not None and (comma < 0 or residence_hit.start() < comma):
            caste = tail[: residence_hit.start()]
            rest = tail[residence_hit.end() :]
            stop = rest.find(",")
            residence = rest if stop < 0 else rest[:stop]
            head_start = offset + residence_hit.end() + (
                len(rest) if stop < 0 else stop + 1
            )
        elif comma >= 0:
            caste, residence = tail[:comma], ""
            head_start = offset + comma + 1
        else:
            caste, residence = tail, ""
            head_start = limit
        groups.append((head, clean(caste), clean(residence)))
    return groups


def _people_in(head: str, caste: str, residence: str) -> list[dict[str, str]]:
    """Split one caste group's name text into one row per person.

    Args:
        head: The text naming the parties, before the caste marker.
        caste: The caste shared by everyone in this group.
        residence: The residence shared by everyone in this group.

    Returns:
        One dict per person.
    """
    markers = list(RELATION.finditer(head))
    if not markers:
        # No father or husband recorded; the whole head is the name.
        name = clean(head)
        return [
            {
                "name": name,
                "relation": "",
                "relative_name": "",
                "caste": caste,
                "residence": residence,
            }
        ]

    segments = []
    previous_end = 0
    for hit in markers:
        segments.append(head[previous_end : hit.start()])
        previous_end = hit.end()
    segments.append(head[previous_end:])

    people: list[dict[str, str]] = []
    pending = clean(segments[0])
    for index, segment in enumerate(segments[1:], start=1):
        relation = "husband" if HUSBAND in markers[index - 1].group(0) else "father"
        if index < len(markers):
            # This segment ends the previous person and begins the next one.
            left, _, right = segment.rpartition(",")
            relative, following = (
                (clean(left), clean(right)) if left else (clean(segment), "")
            )
        else:
            relative, following = clean(segment), ""
        if pending:
            people.append(
                {
                    "name": pending,
                    "relation": relation,
                    "relative_name": relative,
                    "caste": caste,
                    "residence": residence,
                }
            )
        pending = following
    return people


def read_checkpoints(root: Path) -> list[dict]:
    """Read every fetched khatiyan record.

    Args:
        root: The ``raw/ror`` directory.

    Returns:
        One dict per fetched khatiyan.
    """
    records: list[dict] = []
    # The tahsil level is part of the checkpoint key: village_code repeats
    # across tahsils, and `village_path` says so. A glob without it matches
    # nothing against the layout the fetcher writes.
    for path in sorted(root.glob("district_*/tahsil_*/village_*.jsonl.gz")):
        # The crawler appends to these while this runs, so a truncated member
        # is expected rather than exceptional. zlib raises its own error type,
        # which is neither OSError nor EOFError, and one uncaught instance
        # aborts the entire parse over a single half-written file.
        try:
            with gzip.open(path, "rt", encoding="utf8") as handle:
                while True:
                    try:
                        line = handle.readline()
                    except (OSError, EOFError, zlib.error) as error:
                        logger_counts["truncated_file"] += 1
                        print(f"  truncated {path.name}: {error}")
                        break
                    if not line:
                        break
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        # A run killed mid-write leaves a partial final line.
                        logger_counts["partial_line"] += 1
        except (OSError, EOFError, zlib.error) as error:
            logger_counts["unreadable_file"] += 1
            print(f"  unreadable {path.name}: {error}")
    return records


SCHEMA = pa.schema(
    [
        ("district_code", pa.dictionary(pa.int32(), pa.string())),
        ("district_name", pa.dictionary(pa.int32(), pa.string())),
        ("tahsil_code", pa.dictionary(pa.int32(), pa.string())),
        ("tahsil_name", pa.dictionary(pa.int32(), pa.string())),
        ("ri_code", pa.dictionary(pa.int32(), pa.string())),
        ("ri_name", pa.dictionary(pa.int32(), pa.string())),
        ("village_code", pa.dictionary(pa.int32(), pa.string())),
        ("village_name", pa.dictionary(pa.int32(), pa.string())),
        ("khatiyan", pa.string()),
        ("tenant_seq", pa.int16()),
        # High cardinality: dictionary encoding costs more than it saves.
        ("name_or", pa.string()),
        ("relation", pa.dictionary(pa.int32(), pa.string())),
        ("relative_name_or", pa.string()),
        ("caste_or", pa.dictionary(pa.int32(), pa.string())),
        ("residence_or", pa.dictionary(pa.int32(), pa.string())),
        ("raw_cell", pa.string()),
        ("fetched_at", pa.timestamp("us", tz="UTC")),
    ]
)


def build(records: list[dict]) -> pd.DataFrame:
    """Turn fetched khatiyan records into tenant rows.

    Args:
        records: Fetched khatiyan records.

    Returns:
        One row per tenant occurrence on a khatiyan.
    """
    rows: list[dict] = []
    for record in records:
        if not record.get("ok"):
            logger_counts["khatiyan_failed"] += 1
            continue
        logger_counts["khatiyan_ok"] += 1
        seq = 0
        for cell in record.get("cells", []):
            people = split_cell(cell)
            if not people:
                # Most of these are the village line printed above and below
                # the tenant rows, not a failed parse.
                logger_counts["cell_skipped"] += 1
                continue
            for parts in people:
                if not parts["name"]:
                    logger_counts["cell_unparsed"] += 1
                    continue
                seq += 1
                rows.append(
                    {
                        "district_code": record["district_code"],
                        "district_name": record["district_name"],
                        "tahsil_code": record["tahsil_code"],
                        "tahsil_name": record["tahsil_name"],
                        "ri_code": record["ri_code"] or "",
                        "ri_name": record["ri_name"] or "",
                        "village_code": record["village_code"],
                        "village_name": record["village_name"],
                        "khatiyan": record["khatiyan"],
                        "tenant_seq": seq,
                        "name_or": parts["name"],
                        "relation": parts["relation"],
                        "relative_name_or": parts["relative_name"],
                        "caste_or": parts["caste"],
                        "residence_or": parts["residence"],
                        "raw_cell": cell,
                        "fetched_at": record["fetched_at"],
                    }
                )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    # Caste is the label; a row without one cannot be used downstream.
    # Mutation orders quote the blank form and then narrate in prose, so a
    # handful of "castes" are empty or a sentence long. Both are unusable.
    unusable = frame["caste_or"].eq("") | (
        frame["caste_or"].str.len() > MAX_CASTE
    )
    logger_counts["dropped_no_caste"] += int(unusable.sum())
    frame = frame[~unusable].reset_index(drop=True)
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
    frame["tenant_seq"] = frame["tenant_seq"].astype("int16")
    return frame


def check(frame: pd.DataFrame) -> None:
    """Assert the invariants that a drifted parser would violate.

    Args:
        frame: The tenant rows.

    Raises:
        AssertionError: If an invariant fails.
    """
    keys = ["district_code", "tahsil_code", "village_code", "khatiyan", "tenant_seq"]
    duplicates = int(frame.duplicated(keys).sum())
    assert duplicates == 0, f"{duplicates:,} duplicate tenant rows"

    # A row with no caste carries no label and is dropped upstream. Mutation
    # orders quote the blank form -- "ନା: ଜା: ବା:" with nothing filled -- so a
    # few are expected; a flood means the caste split has stopped working.
    blank = int(frame["caste_or"].eq("").sum())
    share = blank / max(len(frame), 1)
    assert share < 0.005, (
        f"{blank:,} of {len(frame):,} rows carry no caste ({share:.2%})"
    )

    dropped = logger_counts["dropped_no_caste"]
    share = dropped / max(len(frame) + dropped, 1)
    assert share < 0.005, (
        f"{dropped:,} rows had an empty or sentence-long caste "
        f"({share:.2%}); the caste split is running away"
    )

    unparsed = logger_counts["cell_unparsed"]
    total_cells = unparsed + len(frame)
    share = unparsed / total_cells if total_cells else 0
    assert share < 0.15, (
        f"{share:.1%} of tenant cells did not parse "
        f"({unparsed:,} of {total_cells:,}); the cell format has drifted"
    )

    # Every khatiyan that fetched successfully should yield a tenant. If most
    # do not, the caste marker has stopped matching and the run is silently
    # collecting nothing.
    produced = frame.groupby(
        ["district_code", "tahsil_code", "village_code", "khatiyan"], observed=True
    ).ngroups
    if logger_counts["khatiyan_ok"]:
        yield_rate = produced / logger_counts["khatiyan_ok"]
        assert yield_rate > 0.85, (
            f"only {yield_rate:.1%} of fetched khatiyans yielded a tenant "
            f"({produced:,} of {logger_counts['khatiyan_ok']:,})"
        )


def main() -> None:
    """Parse the fetched RoRs into a typed tenant table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ror", type=Path, default=ROR)
    parser.add_argument("--out", type=Path, default=TENANTS)
    arguments = parser.parse_args()

    if not arguments.ror.is_dir():
        raise SystemExit("no fetched RoRs; run fetch_ror.py first")

    records = read_checkpoints(arguments.ror)
    print(f"{len(records):,} khatiyan records read")
    frame = build(records)
    if frame.empty:
        raise SystemExit("no tenant rows parsed")

    print(
        f"  {logger_counts['khatiyan_ok']:,} with cells, "
        f"{logger_counts['khatiyan_failed']:,} failed, "
        f"{logger_counts['cell_unparsed']:,} cells unparsed"
    )
    check(frame)

    table = pa.Table.from_pandas(frame, schema=SCHEMA, preserve_index=False)
    pq.write_table(table, arguments.out, compression="zstd")

    print(f"\n{len(frame):,} tenant rows, {frame.name_or.nunique():,} distinct names")
    print(f"  districts {frame.district_name.nunique()}, villages {frame.village_code.nunique():,}")
    print(f"  castes    {frame.caste_or.nunique()}")
    print(f"\nwrote {arguments.out} ({arguments.out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
