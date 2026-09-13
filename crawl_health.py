"""Audit Odisha checkpoints every 30 minutes, reusing unchanged file summaries."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import time
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from parse_ror import split_cell

HERE = Path(__file__).resolve().parent


def failure_kind(error: str) -> str:
    """Separate source defects and provenance failures from PDF decoder failures."""
    if "PDFProvenanceError" in error:
        return "pdf_provenance"
    if "PDFSourceIncompleteError" in error or error.startswith("PDF source:"):
        return "pdf_source"
    if error == "invalid or truncated PDF response":
        return "pdf_response"
    if error.startswith("PDF extraction:"):
        return "pdf_extraction"
    if error == "no tenant cell in response":
        return "no_cells"
    if any(
        word in error.lower()
        for word in ("timeout", "timed out", "resolve", "nodename", "reset", "urlopen")
    ):
        return "transport"
    return "other"


def inspect(path: Path, key: tuple[str, str, str]) -> dict:
    counts = Counter()
    issues = Counter()
    successful = set()
    failed = set()
    labels = {}
    pdf_dependencies = {}
    alias_path = HERE / "raw/pdf-village-aliases.json"
    aliases = json.loads(alias_path.read_text()) if alias_path.exists() else []
    latest = ""
    try:
        with gzip.open(path, "rt", encoding="utf8") as stream:
            for line in stream:
                row = json.loads(line)
                actual = tuple(
                    str(row.get(field, ""))
                    for field in ("district_code", "tahsil_code", "village_code")
                )
                if actual != key:
                    issues["wrong_village_key"] += 1
                latest = max(latest, row.get("fetched_at", ""))
                if not row.get("fetched_at"):
                    issues["missing_timestamp"] += 1
                khatiyan = row.get("khatiyan_value")
                if not isinstance(khatiyan, str) or not khatiyan.strip():
                    issues["missing_option_identity"] += 1
                    khatiyan = ("missing", row.get("khatiyan"))
                if not row.get("ok"):
                    if row.get("extraction_invalidated"):
                        counts["legacy_incomplete_captures"] += 1
                        failed.add(khatiyan)
                        continue
                    counts["failed_attempts"] += 1
                    failed.add(khatiyan)
                    error = row.get("error", "")
                    kind = failure_kind(error)
                    counts[f"failed_{kind}_attempts"] += 1
                    continue
                labels.setdefault(row.get("khatiyan"), set()).add(khatiyan)
                if khatiyan in successful:
                    issues["duplicate_success"] += 1
                successful.add(khatiyan)
                counts["records"] += 1
                source = row.get("source", {})
                is_pdf = source.get("format") == "pdf"
                counts["pdf_records" if is_pdf else "html_records"] += 1
                if is_pdf:
                    try:
                        pdf = (HERE / source["path"]).resolve()
                        if not pdf.is_relative_to((HERE / "raw/pdf").resolve()):
                            raise ValueError("PDF path outside raw PDF directory")
                        blob = pdf.read_bytes()
                        stat = pdf.stat()
                        pdf_dependencies[source["path"]] = [stat.st_size, stat.st_mtime_ns]
                        if hashlib.sha256(blob).hexdigest() != source["sha256"]:
                            issues["pdf_checksum_mismatch"] += 1
                        if (
                            len(blob) != source["n_bytes"]
                            or not blob.startswith(b"%PDF-")
                            or not blob.rstrip().endswith(b"%%EOF")
                        ):
                            issues["invalid_pdf_payload"] += 1
                        from pdf_ror import identity

                        provenance = source.get("provenance", {})
                        village_names = {identity(row.get("village_name", ""))}
                        for alias in aliases:
                            if all(
                                str(row.get(key)) == str(alias.get(key))
                                for key in (
                                    "district_code",
                                    "tahsil_code",
                                    "village_code",
                                    "village_name",
                                )
                            ):
                                village_names.add(identity(alias["pdf_village_name"]))
                        if identity(
                            provenance.get("village_name", "")
                        ) not in village_names or identity(
                            provenance.get("khatiyan", "")
                        ) != identity(
                            row.get("khatiyan", "")
                        ):
                            issues["wrong_pdf_provenance"] += 1
                    except (OSError, KeyError, ValueError):
                        issues["missing_or_invalid_pdf_source"] += 1
                cells = row.get("cells")
                if (
                    not isinstance(cells, list)
                    or not cells
                    or not all(isinstance(cell, str) and cell.strip() for cell in cells)
                ):
                    issues["invalid_success_payload"] += 1
                    continue
                counts["cells"] += len(cells)
                counts["audited_records"] += 1
                record_people = 0
                for cell in cells:
                    counts["audited_cells"] += 1
                    try:
                        people = split_cell(
                            cell, owner_block=is_pdf or source.get("owner_spans", False)
                        )
                    except (ValueError, TypeError, KeyError):
                        issues["parse_error"] += 1
                        continue
                    record_people += len(people)
                    counts["audited_cells_without_people"] += not people
                    for person in people:
                        counts["audited_owner_entries"] += 1
                        for field in ("name", "caste", "relative_name", "residence"):
                            counts[f"audited_{field}_filled"] += bool(person.get(field))
                counts["audited_records_without_owner_entries"] += not record_people
    except (EOFError, OSError, zlib.error, json.JSONDecodeError, UnicodeError):
        issues["unreadable_tail"] += 1
    counts["unique_records"] = len(successful)
    counts["unresolved_failed_options"] = len(failed - successful)
    counts["recovered_options"] = len(failed & successful)
    counts["labels_with_distinct_options"] = sum(len(values) > 1 for values in labels.values())
    return {
        "counts": dict(counts),
        "issues": dict(issues),
        "latest_record": latest,
        "pdf_dependencies": pdf_dependencies,
    }


def audit() -> None:
    started = time.monotonic()
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    cache_path = logs / "crawl-health-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cache = {key: value for key, value in cache.items() if value.get("version") == 9}
    report_path = logs / "crawl-health.json"
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}
    frame = pd.read_parquet(HERE / "raw/villages.parquet")
    columns = ["district_code", "tahsil_code", "village_code"]
    expected = set(frame[columns].astype(str).itertuples(index=False, name=None))
    counts = Counter()
    issues = Counter()
    problems = {}
    completed = enumerated = legacy_status = 0
    covered = set()
    newest = 0.0
    latest = ""
    updated = {}
    for path in (HERE / "raw/ror").glob("district_*/tahsil_*/village_*.jsonl.gz"):
        relative = str(path.relative_to(HERE / "raw/ror"))
        key = (
            path.parent.parent.name.removeprefix("district_"),
            path.parent.name.removeprefix("tahsil_"),
            path.name.removeprefix("village_").removesuffix(".jsonl.gz"),
        )
        stat = path.stat()
        stamp = [stat.st_size, stat.st_mtime_ns]
        entry = cache.get(relative)
        dependencies_changed = False
        if entry:
            for relative_pdf, old_stamp in entry["result"].get("pdf_dependencies", {}).items():
                try:
                    pdf_stat = (HERE / relative_pdf).stat()
                    dependencies_changed |= [pdf_stat.st_size, pdf_stat.st_mtime_ns] != old_stamp
                except OSError:
                    dependencies_changed = True
        if not entry or entry["stamp"] != stamp or dependencies_changed:
            entry = {"version": 9, "stamp": stamp, "result": inspect(path, key)}
        updated[relative] = entry
        result = entry["result"]
        file_issues = Counter(result["issues"])
        if key not in expected:
            file_issues["village_not_in_frame"] += 1
        if file_issues["unreadable_tail"] and time.time() - stat.st_mtime < 300:
            counts["recently_open_tails"] += file_issues.pop("unreadable_tail")
        counts.update(result["counts"])
        issues.update(file_issues)
        if file_issues:
            problems[relative] = dict(file_issues)
        if result["counts"].get("unique_records"):
            covered.add(key)
        status_path = path.with_suffix(".status.json")
        if status_path.exists():
            status = json.loads(status_path.read_text())
            enumerated += 1
            if status.get("identity") != "khatiyan_value":
                legacy_status += 1
            else:
                completed += status["remaining"] == 0 and not file_issues
        newest = max(newest, stat.st_mtime)
        latest = max(latest, result["latest_record"])
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eligible_villages": len(expected),
        "duplicate_frame_keys": len(frame) - len(expected),
        "villages_with_records": len(covered & expected),
        "villages_with_completion_status": enumerated,
        "confirmed_complete_villages": completed,
        "legacy_completion_statuses_to_revalidate": legacy_status,
        "counts": dict(counts),
        "issues": dict(issues),
        "problem_files": problems,
        "latest_record": latest,
        "seconds_since_checkpoint_write": round(time.time() - newest),
        "free_disk_gb": round(shutil.disk_usage(HERE).free / 1e9, 2),
        "data_free_disk_gb": round(shutil.disk_usage(HERE / "raw").free / 1e9, 2),
        "audit_seconds": round(time.monotonic() - started, 1),
        "parse_scope": "all saved successful records and every cell",
        "audit_mode": "census",
        "new_records_since_previous_audit": (
            counts["unique_records"] - previous["counts"]["unique_records"] if previous else None
        ),
    }
    warnings = []
    if issues:
        warnings.append(f"Checkpoint integrity issues: {dict(issues)}")
    if report["seconds_since_checkpoint_write"] > 1800:
        warnings.append("No checkpoint writes for at least 30 minutes")
    if counts["audited_records_without_owner_entries"]:
        warnings.append(
            f"{counts['audited_records_without_owner_entries']:,} records yield no owner entries; "
            "owner extraction completeness is unresolved"
        )
    if min(report["free_disk_gb"], report["data_free_disk_gb"]) < 5:
        warnings.append("Less than 5 GB free disk space")
    if previous and report["new_records_since_previous_audit"] == 0:
        warnings.append("No new records since previous audit")
    report["warnings"] = warnings
    for path, value in ((cache_path, updated), (report_path, report)):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value) + "\n")
        temporary.replace(path)
    summary = {key: value for key, value in report.items() if key != "problem_files"}
    with (logs / "crawl-health-history.jsonl").open("a") as stream:
        stream.write(json.dumps(summary) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        audit()
        if not args.watch:
            break
        time.sleep(1800)
