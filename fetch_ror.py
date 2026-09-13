"""Fetch Odisha Record-of-Rights documents, one khatiyan at a time.

The RoR is the only place the tenant's caste appears, and the portal renders
exactly one RoR per session: every cheaper access path was tested and fails.
Reusing a session serves one record and then returns the selection page;
re-syncing through the village or search-type dropdown does not restore it;
the ``h1`` field on the RoR page is ignored; the async UpdatePanel postback
answers with a redirect. So each record costs a fresh cascade -- about 16
seconds. The supervised crawl enumerates every village and retries incomplete
work until every distinct khatiyan option has a saved record.

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
import fcntl
import gzip
import hashlib
import json
import logging
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bhulekh import (  # noqa: E402
    BIND,
    DISTRICT,
    RI,
    ROOT,
    SEARCH_TYPE,
    TAHSIL,
    VIEW_ROR,
    VILLAGE,
    FormParser,
    PortalError,
    Session,
)
from parse_ror import split_cell  # noqa: E402
from pdf_ror import (  # noqa: E402
    PDFProvenanceError,
    PDFSourceIncompleteError,
    extract_pdf,
    identity,
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
    return unicodedata.normalize("NFC", value).replace("‌", "").replace("‍", "").strip()


class HTMLRecord(HTMLParser):
    """Capture explicit owner and identity spans, including entries without caste."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: dict[str, dict[str, str]] = {}
        self.capture: tuple[str, str] | None = None
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        void = tag in {"br", "img", "input", "hr", "meta", "link"}
        if self.capture:
            if tag == "br":
                self.parts.append("\n")
            if not void:
                self.depth += 1
            return
        identifier = dict(attrs).get("id") or ""
        match = re.fullmatch(r"(gvfront_\w+)_lbl(Name|Mouja|KhatiyanslNo)", identifier)
        if tag == "span" and match:
            self.capture = (match[1], match[2])
            self.depth = 1
            self.parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.capture and tag == "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.capture or tag in {"br", "img", "input", "hr", "meta", "link"}:
            return
        self.depth -= 1
        if self.depth == 0:
            record, field_name = self.capture
            self.records.setdefault(record, {})[field_name] = "".join(self.parts).strip()
            self.capture = None


def tenant_cells(body: str) -> list[str]:
    """Return all explicit owner-field text, including non-person land-status entries."""
    parser = HTMLRecord()
    parser.feed(body)
    return list(dict.fromkeys(row["Name"] for row in parser.records.values() if row.get("Name")))


def has_owner_text(cells: list[str]) -> bool:
    """Require a letter or number; punctuation placeholders are empty fields."""
    return any(character.isalnum() for cell in cells for character in cell)


def empty_owner_capture(row: dict) -> bool:
    """Recognize explicit HTML owner fields containing only placeholders."""
    source = row.get("source", {})
    return bool(source.get("owner_spans")) and not has_owner_text(row.get("cells", []))


def legacy_header_only(row: dict) -> bool:
    """Recognize legacy village headers and boundary text with no owner entries."""
    cells = row.get("cells")
    return (
        not row.get("source")
        and isinstance(cells, list)
        and bool(cells)
        and all(isinstance(cell, str) and "ମୌଜା" in cell for cell in cells)
        and not any(split_cell(cell) for cell in cells)
    )


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
        Exact, untrimmed option values already recorded as successful.
    """
    if not path.is_file():
        return set()
    done: set[str] = set()
    # A checkpoint truncated by a killed worker raises zlib.error, which is
    # neither OSError nor EOFError. Letting it escape aborted the whole
    # village instead of resuming it, so a crash during a write cost every
    # khatiyan in that village rather than the one being written. Read what
    # is readable and treat the rest as not yet fetched.
    try:
        with gzip.open(path, "rt", encoding="utf8") as handle:
            while True:
                try:
                    line = handle.readline()
                except (OSError, EOFError, zlib.error) as error:
                    logger.warning("truncated checkpoint %s: %s", path.name, error)
                    break
                if not line:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A run killed mid-write can leave a partial final line.
                    continue
                if row.get("ok") and not legacy_header_only(row) and not empty_owner_capture(row):
                    value = row.get("khatiyan_value")
                    if isinstance(value, str) and value.strip():
                        done.add(value)
    except (OSError, EOFError, zlib.error) as error:
        logger.warning("unreadable checkpoint %s: %s", path.name, error)
    return done


def salvage(path: Path) -> int:
    """Rewrite a truncated checkpoint from the records still readable.

    Appending to a corrupt gzip produces a second member that the reader can
    never reach, because it stops at the corruption in the first. Rewriting
    the file from what survives makes later appends readable again.

    Args:
        path: The village checkpoint.

    Returns:
        How many records survived.
    """
    rows: list[str] = []
    try:
        with gzip.open(path, "rt", encoding="utf8") as handle:
            while True:
                try:
                    line = handle.readline()
                except (OSError, EOFError, zlib.error):
                    break
                if not line:
                    break
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rows.append(line if line.endswith("\n") else line + "\n")
    except (OSError, EOFError, zlib.error):
        pass
    backup = path.with_name(f"{path.name}.truncated-{time.time_ns()}.bak")
    shutil.copy2(path, backup)
    temporary = path.with_suffix(".salvage.tmp")
    with gzip.open(temporary, "wt", encoding="utf8") as handle:
        handle.writelines(rows)
    temporary.replace(path)
    return len(rows)


def is_readable(path: Path) -> bool:
    """Return whether a checkpoint can be read end to end.

    Args:
        path: The village checkpoint.

    Returns:
        True when the file is absent or fully readable.
    """
    if not path.is_file():
        return True
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(65536):
                pass
    except (OSError, EOFError, zlib.error):
        return False
    return True


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


@dataclass
class FetchResult:
    """A captured record and its extraction outcome."""

    cells: list[str] = field(default_factory=list)
    n_bytes: int = 0
    error: str = ""
    source: dict = field(default_factory=lambda: {"format": "html"})


def pdf_file(village: dict, value: str) -> Path:
    """Address retained PDFs by the complete village key and exact option value."""
    digest = hashlib.sha256(value.encode("utf8")).hexdigest()
    return (
        ROR.parent
        / "pdf"
        / f"district_{village['district_code']}"
        / f"tahsil_{village['tahsil_code']}"
        / f"village_{village['village_code']}"
        / f"{digest}.pdf"
    )


def pdf_result(path: Path, data: bytes, village: dict, label: str) -> FetchResult:
    """Preserve PDF bytes before extraction and retain failures for offline retry."""
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        return FetchResult([], len(data), "invalid or truncated PDF response", {"format": "pdf"})
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise PortalError("retained PDF changed; refusing to overwrite original")
    else:
        temporary = path.with_suffix(".pdf.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    source = {
        "format": "pdf",
        "path": str(path.relative_to(HERE)),
        "sha256": digest,
        "n_bytes": len(data),
    }
    try:
        expected = {**village, "khatiyan": label}
        aliases = HERE / "raw/pdf-village-aliases.json"
        if aliases.exists():
            for alias in json.loads(aliases.read_text()):
                if all(
                    str(village.get(key)) == str(alias.get(key))
                    for key in ("district_code", "tahsil_code", "village_code", "village_name")
                ):
                    expected["pdf_village_alias"] = alias["pdf_village_name"]
        result = extract_pdf(data, expected)
    except (PDFProvenanceError, PDFSourceIncompleteError) as error:
        rejected = path.with_name(f"{path.stem}.rejected-{digest}.pdf")
        path.replace(rejected)
        source["path"] = str(rejected.relative_to(HERE))
        return FetchResult([], len(data), f"PDF source: {type(error).__name__}: {error}", source)
    except Exception as error:
        return FetchResult(
            [], len(data), f"PDF extraction: {type(error).__name__}: {error}", source
        )
    source.update({key: value for key, value in result.items() if key != "cells"})
    return FetchResult(result["cells"], len(data), "", source)


def fetch_one(village: dict, value: str, pause: float, label: str) -> FetchResult:
    """Fetch and validate one HTML or PDF RoR using its exact option identity."""
    path = pdf_file(village, value)
    if path.exists():
        return pdf_result(path, path.read_bytes(), village, label)
    session = Session(pause=pause)
    session.open()
    fields = cascade(session, village)
    if (value, label) not in session.options(BIND):
        raise PortalError("requested khatiyan option is no longer in village listing")
    fields[BIND] = value
    response = session.submit_bytes(fields, VIEW_ROR, "View RoR")
    if response.startswith(b"%PDF-"):
        return pdf_result(path, response, village, label)
    body = response.decode("utf8", "strict")
    if re.search(r"window\.open\(\s*['\"]HRoRView\.aspx\?Param=1['\"]", body):
        form = FormParser()
        form.feed(body)
        for control in (DISTRICT, TAHSIL, RI, VILLAGE, BIND):
            if control in fields and form.selected.get(control) != fields[control]:
                raise PortalError("PDF selection page does not match requested composite key")
        data = session.fetch_bytes(ROOT + "HRoRView.aspx?Param=1")
        return pdf_result(path, data, village, label)
    parser = HTMLRecord()
    parser.feed(body)
    if not parser.records:
        return FetchResult([], len(response), "HTML response has no explicit owner fields")
    for record in parser.records.values():
        if identity(record.get("Mouja", "")) != identity(village["village_name"]):
            raise PortalError("HTML village does not match requested village")
        if identity(record.get("KhatiyanslNo", "")) != identity(label):
            raise PortalError("HTML khatiyan does not match requested khatiyan")
    cells = tenant_cells(body)
    return FetchResult(
        cells,
        len(response),
        "" if has_owner_text(cells) else "HTML owner field is empty",
        {
            "format": "html",
            "owner_spans": True,
            "provenance": {"village_name": village["village_name"], "khatiyan": label},
        },
    )


class Runner:
    """Fetches villages from a shared queue and appends to their checkpoints.

    Attributes:
        pause: Seconds between requests.
        per_village: Maximum khatiyans to fetch from one village.
        log_lock: Guards the structured log file.
    """

    def __init__(
        self, pause: float, per_village: int | None, log_path: Path, counters: dict
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

    def save_status(self, path: Path, expected: set[str], saved: set[str]) -> None:
        """Record the enumerated workload and saved subset for this village."""
        status = {
            "listed_at": datetime.now(timezone.utc).isoformat(),
            "identity": "khatiyan_value",
            "expected": len(expected),
            "saved": len(expected & saved),
            "remaining": len(expected - saved),
        }
        temporary = path.with_suffix(".status.tmp")
        temporary.write_text(json.dumps(status) + "\n")
        temporary.replace(path.with_suffix(".status.json"))
        if status["remaining"]:
            self.bump("remaining", status["remaining"])

    def village(self, row: dict) -> None:
        """Fetch up to ``per_village`` khatiyans from one village.

        Args:
            row: One row of the village frame.
        """
        path = village_path(row["district_code"], row["tahsil_code"], row["village_code"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not is_readable(path):
            kept = salvage(path)
            logger.warning("salvaged %s: %d records kept", path.name, kept)
        already = done_khatiyans(path)

        try:
            session = Session(pause=self.pause)
            session.open()
            cascade(session, row)
            khatiyans = session.options(BIND)
            if not khatiyans:
                raise PortalError("village listing returned no khatiyan options")
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

        wanted = [(value, label) for value, label in khatiyans if value not in already][
            : self.per_village
        ]
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
        expected = {value for value, _ in khatiyans}
        if not wanted:
            self.save_status(path, expected, already)
            return

        with gzip.open(path, "at", encoding="utf8") as handle:
            for value, label in wanted:
                started = time.time()
                try:
                    result = fetch_one(row, value, self.pause, label)
                except PortalError as failure:
                    result = FetchResult(error=str(failure))
                except Exception as failure:  # noqa: BLE001
                    # One unreadable khatiyan must not cost the rest of the
                    # village. The run is resumable, so a failure is retried.
                    result = FetchResult(error=f"{type(failure).__name__}: {failure}")
                cells, size, error = result.cells, result.n_bytes, result.error
                successful = bool(cells) and not error
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
                            "ok": successful,
                            "error": error,
                            "cells": cells,
                            "source": result.source,
                            "n_bytes": size,
                            "elapsed_ms": elapsed,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                handle.flush()
                if successful:
                    already.add(value)
                self.bump("ok" if successful else "failed")
                self.bump("cells", len(cells))
                if not successful:
                    # Only failures are logged per khatiyan. Logging successes
                    # too put 123 MB in one file for 620k records, and would
                    # reach roughly 4 GB across the full 20.4M -- thirty times
                    # the size of the data it describes. A success is already
                    # counted in the progress line and written to the
                    # checkpoint; the failure histogram is what has diagnostic
                    # value, and that is what this keeps.
                    self.record(
                        {
                            "event": "khatiyan_failed",
                            "village": row["village_name"],
                            "village_code": row["village_code"],
                            "tahsil_code": row["tahsil_code"],
                            "khatiyan": label,
                            "elapsed_ms": elapsed,
                            "error": error,
                        }
                    )

        self.save_status(path, expected, already)


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
    return (
        frame.assign(_rank=rank)
        .sort_values(["_rank", "tahsil_code", "village_code"])
        .drop(columns="_rank")
    )


def run() -> None:
    """Crawl RoRs into per-village checkpoints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", type=Path, default=FRAME)
    parser.add_argument("--priority-file", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-village", type=int, default=None)
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
    if arguments.priority_file:
        priority = {tuple(key) for key in json.loads(arguments.priority_file.read_text())}
        columns = ["district_code", "tahsil_code", "village_code"]
        keys = frame[columns].astype(str).itertuples(index=False, name=None)
        frame = (
            frame.assign(_repair=[key not in priority for key in keys])
            .sort_values("_repair", kind="stable")
            .drop(columns="_repair")
        )
    if arguments.villages:
        frame = frame.head(arguments.villages)
    logger.info(
        "%d villages queued, %d workers, %s per village, log %s",
        len(frame),
        arguments.workers,
        arguments.per_village if arguments.per_village is not None else "all",
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
            active = next((thread for thread in threads if thread.is_alive()), None)
            if active is None:
                break
            active.join(timeout=60)
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
        raise SystemExit(130)

    print(
        f"\n{counters.get('ok', 0):,} records with cells, "
        f"{counters.get('cells', 0):,} tenant cells, "
        f"{counters.get('failed', 0):,} failed, "
        f"{counters.get('village_failed', 0):,} villages unreachable"
    )
    print(f"log: {log_path}")
    if any(counters.get(key, 0) for key in ("failed", "village_failed", "remaining")):
        raise SystemExit(1)


def main() -> None:
    """Prevent concurrent writers and report unfinished passes to Supervisor."""
    ROR.mkdir(parents=True, exist_ok=True)
    with (ROR / ".crawl.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit(2) from error
        run()


if __name__ == "__main__":
    main()
