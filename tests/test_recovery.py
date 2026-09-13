"""Regression tests for supervised recovery."""

import fcntl
import gzip
import json

import pytest

import fetch_ror


def test_salvage_discards_partial_json_and_preserves_valid_rows(tmp_path):
    path = tmp_path / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write(json.dumps({"ok": True, "khatiyan": "1", "khatiyan_value": "1"}) + "\n")
        stream.write('{"ok": true,')
    original = path.read_bytes()
    assert fetch_ror.salvage(path) == 1
    backups = list(tmp_path.glob("*.truncated-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert fetch_ror.done_khatiyans(path) == {"1"}
    assert fetch_ror.is_readable(path)


def test_writer_lock_prevents_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_ror, "ROR", tmp_path)
    with (tmp_path / ".crawl.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as error:
            fetch_ror.main()
        assert error.value.code == 2


def test_status_tracks_remaining_work(tmp_path):
    counters = {}
    runner = fetch_ror.Runner(0, 1, tmp_path / "log", counters)
    path = tmp_path / "village.jsonl.gz"
    runner.save_status(path, {"1", "2"}, {"1", "other"})
    status = json.loads(path.with_suffix(".status.json").read_text())
    assert (status["expected"], status["saved"], status["remaining"]) == (2, 1, 1)
    assert counters["remaining"] == 1


@pytest.mark.parametrize("failed", [False, True])
def test_pass_exit_status_controls_retry(tmp_path, monkeypatch, failed):
    import sys

    import pandas as pd

    frame = tmp_path / "frame.parquet"
    pd.DataFrame(
        [
            {
                "district_code": "1",
                "district_name": "test",
                "tahsil_code": "1",
                "village_code": "1",
                "village_name": "test",
            }
        ]
    ).to_parquet(frame)
    monkeypatch.setattr(fetch_ror, "ROR", tmp_path / "ror")
    monkeypatch.setattr(fetch_ror, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(sys, "argv", ["fetch", "--frame", str(frame), "--workers", "1"])
    monkeypatch.setattr(
        fetch_ror.Runner, "village", lambda self, row: self.bump("failed" if failed else "ok")
    )
    if failed:
        with pytest.raises(SystemExit) as error:
            fetch_ror.main()
        assert error.value.code == 1
    else:
        fetch_ror.main()


def test_audit_checks_composite_key_and_parser_fields(tmp_path):
    from crawl_health import inspect

    path = tmp_path / "village.jsonl.gz"
    row = {
        "district_code": "1",
        "tahsil_code": "2",
        "village_code": "3",
        "khatiyan": "1",
        "khatiyan_value": "1",
        "ok": True,
        "fetched_at": "2026-09-11T00:00:00+00:00",
        "cells": ["A ପି:B ଜା: C ବା: D"],
    }
    with gzip.open(path, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    result = inspect(path, ("1", "2", "3"))
    assert result["issues"] == {}
    assert result["counts"]["audited_relative_name_filled"] == 1
    assert inspect(path, ("1", "other", "3"))["issues"]["wrong_village_key"] == 1


def test_census_parses_every_record_and_cell(tmp_path):
    from crawl_health import inspect

    path = tmp_path / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        for index in range(25):
            stream.write(
                json.dumps(
                    {
                        "district_code": "1",
                        "tahsil_code": "2",
                        "village_code": "3",
                        "khatiyan": str(index),
                        "khatiyan_value": str(index),
                        "ok": True,
                        "fetched_at": "2026-09-11T00:00:00+00:00",
                        "cells": ["ମୌଜା : test", "A ପି:B ଜା: C ବା: D"],
                    }
                )
                + "\n"
            )
    result = inspect(path, ("1", "2", "3"))
    assert result["issues"] == {}
    assert result["counts"]["audited_records"] == 25
    assert result["counts"]["audited_cells"] == 50
    assert result["counts"]["audited_cells_without_people"] == 25
    assert result["counts"]["audited_owner_entries"] == 25


def test_census_keys_are_option_values_not_printed_labels(tmp_path):
    from crawl_health import inspect

    path = tmp_path / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        for value in ["20 ", "20\t"]:
            stream.write(
                json.dumps(
                    {
                        "district_code": "1",
                        "tahsil_code": "2",
                        "village_code": "3",
                        "khatiyan": "20",
                        "khatiyan_value": value,
                        "ok": True,
                        "fetched_at": "2026-09-11T00:00:00+00:00",
                        "cells": ["A ଜା: C"],
                    }
                )
                + "\n"
            )
    result = inspect(path, ("1", "2", "3"))
    assert result["issues"] == {}
    assert result["counts"]["unique_records"] == 2
    assert result["counts"]["labels_with_distinct_options"] == 1


def test_audit_separates_pdf_source_provenance_response_and_decoder_failures(tmp_path):
    from crawl_health import inspect

    errors = [
        "PDF source: PDFSourceIncompleteError: missing owner table",
        "PDF extraction: PDFSourceIncompleteError: undefined glyph",
        "PDF source: PDFProvenanceError: wrong village",
        "PDF extraction: ValueError: unsupported font",
        "invalid or truncated PDF response",
        "https://example.test failed: read operation timed out",
    ]
    path = tmp_path / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        for index, error in enumerate(errors):
            stream.write(
                json.dumps(
                    {
                        "district_code": "1",
                        "tahsil_code": "2",
                        "village_code": "3",
                        "khatiyan_value": str(index),
                        "ok": False,
                        "fetched_at": "2026-09-12T00:00:00+00:00",
                        "error": error,
                    }
                )
                + "\n"
            )
    result = inspect(path, ("1", "2", "3"))
    assert result["issues"] == {}
    counts = result["counts"]
    assert counts["failed_attempts"] == counts["unresolved_failed_options"] == 6
    assert counts["failed_pdf_source_attempts"] == 2
    assert counts["failed_pdf_provenance_attempts"] == 1
    assert counts["failed_pdf_response_attempts"] == 1
    assert counts["failed_pdf_extraction_attempts"] == 1
    assert counts["failed_transport_attempts"] == 1
