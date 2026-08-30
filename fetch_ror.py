"""Fetch Odisha Record-of-Rights documents, one khatiyan at a time.

The RoR is the only place the tenant's caste appears, and the portal renders
exactly one RoR per session: every cheaper access path was tested and fails.
Reusing a session serves one record and then returns the selection page;
re-syncing through the village or search-type dropdown does not restore it;
the ``h1`` field on the RoR page is ignored; the async UpdatePanel postback
answers with a redirect. So each record costs a fresh cascade -- about 16
seconds -- and the crawl is a sampling exercise that widens for as long as it
is left running, not something that finishes.

Two details that are easy to get wrong and expensive to rediscover:

* The khatiyan option value is space-padded to 30 characters and the padding
  is load-bearing. Trimming it returns a 4 KB error page rather than a record.
* ``village_code`` repeats across tahsils, so every key here is composite.

Checkpointing is per khatiyan, appended to a per-village gzipped JSONL. A
khatiyan that yields nothing is written with ``ok`` false and a reason so the
next pass retries it; it is never silently recorded as done.

Usage:
    uv run python scripts/data-acquisition/odisha_ror/fetch_ror.py \
        --districts ଗଜପତି,କନ୍ଧମାଳ --workers 8 --per-village 40
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import queue
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bhulekh import (  # noqa: E402
    BIND,
    DISTRICT,
    RI,
    SEARCH_TYPE,
    TAHSIL,
    VIEW_ROR,
    VILLAGE,
    PortalError,
    Session,
)

HERE = Path(__file__).resolve().parent
FRAME = HERE / "raw" / "villages.parquet"
ROR = HERE / "raw" / "ror"
LOGS = HERE / "logs"

# Christian-heavy districts first. The caste field marks Christians explicitly
# and marks nobody else, and Indian Christian names are the corpus's thinnest
# class, so this is where the marginal record is worth most.
PRIORITY = ("ଗଜପତି", "କନ୍ଧମାଳ", "ସୁନ୍ଦରଗଡ଼", "ରାୟଗଡ଼ା")

logger = logging.getLogger("fetch_ror")


def norm(value: str) -> str:
    """Return a comparable form of an Odia label.

    Args:
        value: Text as rendered by the portal or typed on the command line.

    Returns:
        NFC-normalised text with zero-width joiners and padding removed.
    """
    return (
        unicodedata.normalize("NFC", value)
        .replace("‌", "")
        .replace("‍", "")
        .strip()
    )


class Cells(HTMLParser):
    """Collect the visible text runs of a rendered RoR.

    Attributes:
        chunks: Non-blank text runs, in document order.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        """Keep one non-blank run of character data.

        Args:
            data: Character data from the document.
        """
        stripped = data.strip()
        if stripped:
            self.chunks.append(stripped)


# The tenant cell always carries a caste marker. Matching on that rather than
# on a full name/father/caste/residence shape keeps the rows whose father is
# recorded as a husband, whose name carries a comma-suffixed alias, or whose
# residence is missing -- all of which occur and all of which are wanted.
CASTE_MARKER = "ଜା"


def tenant_cells(body: str) -> list[str]:
    """Return the tenant cells of one RoR, unsplit.

    Splitting happens in the parser, not here, so the parse can be rerun
    against the same bytes without refetching.

    Args:
        body: The RoR page HTML.

    Returns:
        Every text run that carries a caste marker.
    """
    parser = Cells()
    parser.feed(body)
    return [
        chunk
        for chunk in parser.chunks
        if f"{CASTE_MARKER}:" in chunk or f"{CASTE_MARKER} :" in chunk
    ]


def village_path(district_code: str, tahsil_code: str, village_code: str) -> Path:
    """Return the checkpoint file for one village.

    The tahsil is part of the key because ``village_code`` repeats across
    tahsils. Leaving it out merged 322 of 476 villages into shared files, and
    the resume check then read one village's khatiyans and skipped another's
    as already fetched.

    Args:
        district_code: District code.
        tahsil_code: Tahsil code.
        village_code: Village code, unique only within its tahsil.

    Returns:
        Path to the gzipped JSONL checkpoint.
    """
    return (
        ROR
        / f"district_{district_code}"
        / f"tahsil_{tahsil_code}"
        / f"village_{village_code}.jsonl.gz"
    )


def done_khatiyans(path: Path) -> set[str]:
    """Return the khatiyans already fetched successfully for a village.

    Rows written with ``ok`` false are deliberately excluded so the next pass
    retries them: an empty result means the cascade lost its state, not that
    the khatiyan is empty.

    Args:
        path: The village checkpoint.

    Returns:
        Khatiyan labels already recorded as successful.
    """
    if not path.is_file():
        return set()
    done: set[str] = set()
    try:
        with gzip.open(path, "rt", encoding="utf8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A run killed mid-write can leave a partial final line.
                    continue
                if row.get("ok"):
                    done.add(row["khatiyan"])
    except (OSError, EOFError) as error:
        logger.warning("unreadable checkpoint %s: %s", path.name, error)
    return done


def cascade(session: Session, village: dict) -> dict[str, str]:
    """Walk district to village on a fresh session and select Khatiyan mode.

    Args:
        session: An opened portal session.
        village: One row of the village frame.

    Returns:
        The selected dropdown values, ready to submit.
    """
    fields = {DISTRICT: village["district_code"]}
    session.postback(DISTRICT, fields)
    fields[TAHSIL] = village["tahsil_code"]
    session.postback(TAHSIL, fields)
    if village["ri_code"]:
        fields[RI] = village["ri_code"]
        session.postback(RI, fields)
    fields[VILLAGE] = village["village_code"]
    fields[SEARCH_TYPE] = "Khatiyan"
    session.postback(VILLAGE, fields)
    if not session.options(BIND):
        session.postback(SEARCH_TYPE, fields)
    return fields


def fetch_one(village: dict, value: str, pause: float) -> tuple[list[str], int, str]:
    """Fetch a single RoR on its own session.

    Args:
        village: One row of the village frame.
        value: The padded khatiyan option value.
        pause: Seconds between requests.

    Returns:
        The tenant cells, the response size, and an error string (empty if
        the fetch succeeded).
    """
    session = Session(pause=pause)
    session.open()
    fields = cascade(session, village)
    fields[BIND] = value
    body = session.submit(fields, VIEW_ROR, "View RoR")
    cells = tenant_cells(body)
    if not cells:
        return [], len(body), "no tenant cell in response"
    return cells, len(body), ""


class Runner:
    """Fetches villages from a shared queue and appends to their checkpoints.

    Attributes:
        pause: Seconds between requests.
        per_village: Maximum khatiyans to fetch from one village.
        log_lock: Guards the structured log file.
    """

    def __init__(
        self, pause: float, per_village: int, log_path: Path, counters: dict
    ) -> None:
        self.pause = pause
        self.per_village = per_village
        self.log_path = log_path
        self.counters = counters
        self.log_lock = threading.Lock()
        self.counter_lock = threading.Lock()

    def record(self, event: dict) -> None:
        """Append one structured event to the run log.

        Args:
            event: The event fields.
        """
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with self.log_lock:
            with self.log_path.open("a", encoding="utf8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def bump(self, key: str, amount: int = 1) -> None:
        """Increment a shared counter.

        Args:
            key: Counter name.
            amount: How much to add.
        """
        with self.counter_lock:
            self.counters[key] = self.counters.get(key, 0) + amount

    def village(self, row: dict) -> None:
        """Fetch up to ``per_village`` khatiyans from one village.

        Args:
            row: One row of the village frame.
        """
        path = village_path(
            row["district_code"], row["tahsil_code"], row["village_code"]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        already = done_khatiyans(path)

        try:
            session = Session(pause=self.pause)
            session.open()
            fields = cascade(session, row)
            khatiyans = session.options(BIND)
        except PortalError as error:
            logger.warning("village %s listing failed: %s", row["village_name"], error)
            self.record(
                {
                    "event": "village_failed",
                    "district": row["district_name"],
                    "village": row["village_name"],
                    "village_code": row["village_code"],
                    "error": str(error),
                }
            )
            self.bump("village_failed")
            return

        wanted = [
            (value, label)
            for value, label in khatiyans
            if label not in already
        ][: self.per_village]
        self.record(
            {
                "event": "village_start",
                "district": row["district_name"],
                "village": row["village_name"],
                "village_code": row["village_code"],
                "n_khatiyan": len(khatiyans),
                "n_cached": len(already),
                "n_wanted": len(wanted),
            }
        )
        if not wanted:
            return

        with gzip.open(path, "at", encoding="utf8") as handle:
            for value, label in wanted:
                started = time.time()
                try:
                    cells, size, error = fetch_one(row, value, self.pause)
                except PortalError as failure:
                    cells, size, error = [], 0, str(failure)
                except Exception as failure:  # noqa: BLE001
                    # One unreadable khatiyan must not cost the rest of the
                    # village. The run is resumable, so a failure is retried.
                    cells, size, error = [], 0, f"{type(failure).__name__}: {failure}"
                elapsed = int((time.time() - started) * 1000)
                handle.write(
                    json.dumps(
                        {
                            "district_code": row["district_code"],
                            "district_name": row["district_name"],
                            "tahsil_code": row["tahsil_code"],
                            "tahsil_name": row["tahsil_name"],
                            "ri_code": row["ri_code"],
                            "ri_name": row["ri_name"],
                            "village_code": row["village_code"],
                            "village_name": row["village_name"],
                            "khatiyan": label,
                            "khatiyan_value": value,
                            "ok": bool(cells),
                            "error": error,
                            "cells": cells,
                            "n_bytes": size,
                            "elapsed_ms": elapsed,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                handle.flush()
                self.bump("ok" if cells else "failed")
                self.bump("cells", len(cells))
                self.record(
                    {
                        "event": "khatiyan",
                        "village": row["village_name"],
                        "village_code": row["village_code"],
                        "khatiyan": label,
                        "ok": bool(cells),
                        "n_cells": len(cells),
                        "elapsed_ms": elapsed,
                        "error": error,
                    }
                )


def order_villages(frame: pd.DataFrame, wanted: list[str]) -> pd.DataFrame:
    """Order the frame so priority districts come first.

    Args:
        frame: The village frame.
        wanted: District names, in the order they should be crawled.

    Returns:
        The frame, reordered, with non-priority districts appended.
    """
    names = frame["district_name"].astype(str).map(norm)
    rank = pd.Series(len(wanted), index=frame.index, dtype="int64")
    for position, district in enumerate(wanted):
        rank[names.eq(norm(district))] = position
    # Interleave villages within a district so an interrupted run still has
    # breadth across tahsils rather than a single tahsil crawled to death.
    return frame.assign(_rank=rank).sort_values(
        ["_rank", "tahsil_code", "village_code"]
    ).drop(columns="_rank")


def main() -> None:
    """Crawl RoRs into per-village checkpoints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, default=FRAME)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-village", type=int, default=40)
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument("--villages", type=int, default=None)
    parser.add_argument(
        "--districts",
        type=str,
        default=",".join(PRIORITY),
        help="district names in crawl order; the rest follow",
    )
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not arguments.frame.is_file():
        raise SystemExit("no village frame; run list_locations.py first")

    ROR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOGS / f"fetch-{stamp}.jsonl"

    frame = pd.read_parquet(arguments.frame)
    for column in frame.columns:
        if str(frame[column].dtype) == "category":
            frame[column] = frame[column].astype(str)
    frame = order_villages(frame, [d for d in arguments.districts.split(",") if d])
    if arguments.villages:
        frame = frame.head(arguments.villages)
    logger.info(
        "%d villages queued, %d workers, %d per village, log %s",
        len(frame),
        arguments.workers,
        arguments.per_village,
        log_path.name,
    )

    work: queue.Queue = queue.Queue()
    for row in frame.to_dict("records"):
        work.put(row)

    counters: dict[str, int] = {}
    runner = Runner(arguments.pause, arguments.per_village, log_path, counters)
    started = time.time()

    def consume() -> None:
        while True:
            try:
                row = work.get_nowait()
            except queue.Empty:
                return
            try:
                runner.village(row)
            except Exception as error:  # noqa: BLE001
                logger.warning("village %s: %s", row.get("village_name"), error)
                runner.bump("village_failed")
            finally:
                work.task_done()

    threads = [
        threading.Thread(target=consume, daemon=True, name=f"w{index}")
        for index in range(arguments.workers)
    ]
    for thread in threads:
        thread.start()

    try:
        while any(thread.is_alive() for thread in threads):
            # Wait on one worker rather than polling, so the progress line
            # appears on a timer instead of spinning once the queue drains.
            threads[0].join(timeout=60)
            if not any(thread.is_alive() for thread in threads):
                break
            done = counters.get("ok", 0)
            failed = counters.get("failed", 0)
            elapsed = time.time() - started
            rate = done / elapsed if elapsed else 0
            logger.info(
                "%d records, %d cells, %d failed, %d villages left, %.1f rec/min",
                done,
                counters.get("cells", 0),
                failed,
                work.qsize(),
                rate * 60,
            )
    except KeyboardInterrupt:
        logger.info("interrupted; checkpoints are on disk and a rerun resumes")

    print(
        f"\n{counters.get('ok', 0):,} records with cells, "
        f"{counters.get('cells', 0):,} tenant cells, "
        f"{counters.get('failed', 0):,} failed, "
        f"{counters.get('village_failed', 0):,} villages unreachable"
    )
    print(f"log: {log_path}")


if __name__ == "__main__":
    main()
