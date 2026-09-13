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
comma-separated list of co-tenants in the name slot, a parenthesised gloss
inside the caste (``କୈବର୍ତ୍ତ (ମାଛଧରା)``), and a missing residence. A whole-cell regex drops
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
SEPARATOR = r"\s*[:：\-]\s*"
MARKER_LABELS = {
    FATHER: ("ପିତା", FATHER),
    HUSBAND: ("ସ୍ଵାମୀ", "ସ୍ବାମୀ", HUSBAND),
    CASTE: ("ଜାତି", CASTE),
    RESIDENCE: ("ବାସସ୍ଥାନ", RESIDENCE),
}

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
    labels = "|".join(re.escape(value) for value in MARKER_LABELS.get(label, (label,)))
    if label in (FATHER, HUSBAND):
        full_labels = "|".join(re.escape(value) for value in MARKER_LABELS[label][:-1])
        return re.compile(
            BOUNDARY
            + f"(?:{full_labels})"
            + SEPARATOR
            + "|"
            + BOUNDARY
            + re.escape(label)
            + r"\s*[:：]\s*"
        )
    return re.compile(BOUNDARY + f"(?:{labels})" + SEPARATOR)


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


RELATION = re.compile(PATTERNS["father"].pattern + "|" + PATTERNS["husband"].pattern)


def split_cell(cell: str, *, owner_block: bool = False) -> list[dict[str, str]]:
    """Split one tenant cell into one row per person named on it.

    A cell can carry several tenants who share a caste and residence:

        A ପି: B, C ପି: D ଜା: ସଉରା ବା: ଅନରଡ଼ା

    is two people -- A son of B, and C son of D -- not one. Splitting on the
    relation markers gives segments where each middle segment holds the
    previous person's father and the next person's name, separated by the
    last comma.

    Args:
        cell: The cell text as fetched.
        owner_block: True for an explicit HTML owner span or PDF owner table.

    Returns:
        One dict per named owner entry; empty when no name can be identified.
    """
    if owner_block and not PATTERNS["caste"].search(cell):
        residence = PATTERNS["residence"].search(cell)
        head = cell[: residence.start()] if residence else cell
        tail = clean(cell[residence.end() :]) if residence else ""
        return _people_in(head, "", tail)
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
            head_start = offset + residence_hit.end() + (len(rest) if stop < 0 else stop + 1)
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
        # No father or husband recorded; the whole head names the parties.
        return _named(head, "", "", caste, residence)

    segments = []
    previous_end = 0
    for hit in markers:
        segments.append(head[previous_end : hit.start()])
        previous_end = hit.end()
    segments.append(head[previous_end:])

    people: list[dict[str, str]] = []
    pending = clean(segments[0])
    for index, segment in enumerate(segments[1:], start=1):
        relation = (
            "husband" if PATTERNS["husband"].fullmatch(markers[index - 1].group(0)) else "father"
        )
        if index < len(markers):
            # This segment ends the previous person and begins the next one.
            # The boundary is the FIRST comma, not the last: a person has
            # exactly one father or husband, so everything after the first
            # comma names the next party. Splitting on the last comma put
            # those names in the relative field instead, which claimed 8.7%
            # of tenants had between two and nine fathers and dropped every
            # swallowed name from the table.
            left, found, right = segment.partition(",")
            relative, following = (clean(left), clean(right)) if found else (clean(segment), "")
        else:
            relative, following = clean(segment), ""
        people.extend(_named(pending, relation, relative, caste, residence))
        pending = following
    return people


def _named(names: str, relation: str, relative: str, caste: str, residence: str) -> list[dict]:
    """Split a comma-separated name slot into one row per person.

    Co-tenants who share a father, caste and residence are printed as one
    comma-separated run -- "ଲବ ବିଶୋଇ, ବେଡ଼ା ବିଶୋଇ ପି: ଦୁଃଖୁବି ବିଶୋଇ" is two
    brothers, not one person. An alias is written with an explicit marker
    (``ଓରଫ``, 524 occurrences in 1.5M records) and never with a comma, and
    69% of the comma elements occur elsewhere in the corpus as a tenant in
    their own right, so the comma separates people.

    Args:
        names: The name slot, possibly listing several people.
        relation: Whether the relative is a father or a husband.
        relative: The shared father or husband.
        caste: The shared caste.
        residence: The shared residence.

    Returns:
        One dict per person named.
    """
    return [
        {
            "name": name,
            "relation": relation,
            "relative_name": relative,
            "caste": caste,
            "residence": residence,
        }
        # Clerks write ",," and trailing commas; those elements name nobody.
        for name in (clean(part) for part in names.split(","))
        if name
    ]


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
        # The printed khatiyan number is not unique. Daringbadi village 71
        # lists two distinct khatiyans both labelled "368/658", holding
        # different tenants; the site's own option values differ only by an
        # embedded tab. That raw value is the real identity, so it is kept
        # verbatim -- any normalisation recreates the collision.
        ("khatiyan_value", pa.string()),
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
            people = split_cell(
                cell,
                owner_block=(
                    record.get("source", {}).get("format") == "pdf"
                    or record.get("source", {}).get("owner_spans", False)
                ),
            )
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
                        "khatiyan_value": record["khatiyan_value"],
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
    unusual = frame["caste_or"].eq("") | (frame["caste_or"].str.len() > MAX_CASTE)
    logger_counts["missing_or_long_caste"] += int(unusual.sum())
    # isoformat() omits ".%f" when microseconds are exactly zero, so the
    # column carries two shapes and pandas' inferred format breaks on one.
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], format="ISO8601", utc=True)
    frame["tenant_seq"] = frame["tenant_seq"].astype("int16")
    return frame


def check(frame: pd.DataFrame) -> None:
    """Assert the invariants that a drifted parser would violate.

    Args:
        frame: The tenant rows.

    Raises:
        AssertionError: If an invariant fails.
    """
    keys = [
        "district_code",
        "tahsil_code",
        "village_code",
        "khatiyan_value",
        "tenant_seq",
    ]
    duplicates = int(frame.duplicated(keys).sum())
    assert duplicates == 0, f"{duplicates:,} duplicate tenant rows"

    # A person has one father or husband, so a comma in the relative field
    # means the split ran past the boundary and swallowed the next party's
    # names -- deleting them as tenants. Splitting on the last comma instead
    # of the first put 8.7% of rows here; a handful survive on cells whose
    # punctuation defeats any boundary rule.
    shared = int(frame["relative_name_or"].str.contains(",", na=False).sum())
    share = shared / max(len(frame), 1)
    assert share < 0.01, (
        f"{shared:,} of {len(frame):,} rows name more than one father "
        f"({share:.2%}); the relation split is swallowing names"
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
