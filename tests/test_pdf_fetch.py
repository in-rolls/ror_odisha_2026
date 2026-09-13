"""Exercise PDF fetches and exact option identities without network access."""

import gzip
import hashlib
import html
import json

import pytest

import fetch_ror
from bhulekh import BIND, DISTRICT, RI, ROOT, TAHSIL, VILLAGE, PortalError
from parse_ror import build, split_cell
from pdf_ror import PDFProvenanceError


@pytest.fixture
def village():
    return {
        "district_code": "1",
        "district_name": "district",
        "tahsil_code": "2",
        "tahsil_name": "tahsil",
        "ri_code": "3",
        "ri_name": "ri",
        "village_code": "4",
        "village_name": "ଗ୍ରାମ",
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_ror, "HERE", tmp_path)
    monkeypatch.setattr(fetch_ror, "ROR", tmp_path / "raw/ror")
    return tmp_path


def test_pdf_popup_keeps_session_and_untrimmed_option(monkeypatch, village, isolated):
    value = "20\t                         "
    fields = {DISTRICT: "1", TAHSIL: "2", RI: "3", VILLAGE: "4"}
    submitted = []
    downloaded = []
    body = (
        "".join(
            f'<select name="{control}">'
            f'<option selected value="{html.escape(v)}">x</option></select>'
            for control, v in {**fields, BIND: value}.items()
        )
        + "<script>window.open('HRoRView.aspx?Param=1','_blank')</script>"
    )
    binary = b"%PDF-1.3\n\x80\xff\n%%EOF"

    class Session:
        def __init__(self, **kwargs):
            pass

        def open(self):
            pass

        def options(self, control):
            return [(value, "20")]

        def submit_bytes(self, selected, *args):
            submitted.append(dict(selected))
            return body.encode()

        def fetch_bytes(self, url):
            downloaded.append(url)
            return binary

    monkeypatch.setattr(fetch_ror, "Session", Session)
    monkeypatch.setattr(fetch_ror, "cascade", lambda session, row: dict(fields))
    monkeypatch.setattr(
        fetch_ror,
        "extract_pdf",
        lambda data, expected: {
            "cells": ["A ଜା: C"],
            "provenance": {"village_name": "ଗ୍ରାମ", "khatiyan": "20"},
            "page_count": 1,
            "extractor_version": 1,
        },
    )
    result = fetch_ror.fetch_one(village, value, 0, "20")
    assert result.cells == ["A ଜା: C"]
    assert submitted[0][BIND] == value
    assert downloaded == [ROOT + "HRoRView.aspx?Param=1"]
    path = isolated / result.source["path"]
    assert path.read_bytes() == binary
    assert result.source["sha256"] == hashlib.sha256(binary).hexdigest()
    fetch_ror.fetch_one(village, value, 0, "20")
    assert len(downloaded) == 1
    body = body.replace('value="4"', 'value="other"')
    path.unlink()
    with pytest.raises(PortalError, match="composite key"):
        fetch_ror.fetch_one(village, value, 0, "20")


def test_invalid_pdf_is_retryable_and_wrong_pdf_is_preserved(monkeypatch, village, isolated):
    path = fetch_ror.pdf_file(village, "20")
    result = fetch_ror.pdf_result(path, b"<html>unavailable</html>", village, "20")
    assert result.error and not result.cells and not path.exists()

    def wrong(data, expected):
        raise PDFProvenanceError("wrong village")

    monkeypatch.setattr(fetch_ror, "extract_pdf", wrong)
    binary = b"%PDF-1.3\noriginal\n%%EOF"
    result = fetch_ror.pdf_result(path, binary, village, "20")
    assert result.error and not path.exists()
    assert (isolated / result.source["path"]).read_bytes() == binary


def test_identical_printed_labels_do_not_skip_distinct_options(monkeypatch, village, isolated):
    path = fetch_ror.village_path("1", "2", "4")
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt") as stream:
        stream.write(json.dumps({"ok": True, "khatiyan": "20", "khatiyan_value": "20 "}) + "\n")

    class Session:
        def __init__(self, **kwargs):
            pass

        def open(self):
            pass

        def options(self, control):
            return [("20 ", "20"), ("20\t", "20")]

    monkeypatch.setattr(fetch_ror, "Session", Session)
    monkeypatch.setattr(fetch_ror, "cascade", lambda *args: {})
    calls = []

    def fetch(row, value, pause, label):
        calls.append(value)
        return fetch_ror.FetchResult(["A ଜା: C"], 100)

    monkeypatch.setattr(fetch_ror, "fetch_one", fetch)
    runner = fetch_ror.Runner(0, None, isolated / "events.jsonl", {})
    runner.village(village)
    assert calls == ["20\t"]
    assert fetch_ror.done_khatiyans(path) == {"20 ", "20\t"}
    status = json.loads(path.with_suffix(".status.json").read_text())
    assert status["identity"] == "khatiyan_value"
    assert status["expected"] == status["saved"] == 2
    assert status["remaining"] == 0


def test_pdf_owner_without_caste_survives_parser(village):
    assert split_cell("A ପି:B", owner_block=True)[0]["name"] == "A"
    frame = build(
        [
            {
                **village,
                "khatiyan": "20",
                "khatiyan_value": "20 ",
                "ok": True,
                "cells": ["A ପି:B"],
                "source": {"format": "pdf"},
                "fetched_at": "2026-09-11T00:00:00+00:00",
            }
        ]
    )
    assert len(frame) == 1
    assert frame.iloc[0].name_or == "A"
    assert frame.iloc[0].caste_or == ""


def test_html_owner_field_without_caste_is_preserved_and_headers_excluded():
    body = '<span id="gvfront_ctl02_lblMouja">ଗ୍ରାମ</span>'
    body += '<span id="gvfront_ctl02_lblName">A ପି:B<br/>unlabelled text</span>'
    body += '<span id="gvfront_ctl03_lblName">land-status description</span>'
    assert fetch_ror.tenant_cells(body) == ["A ପି:B\nunlabelled text", "land-status description"]
    assert fetch_ror.tenant_cells("<td>ମୌଜା : village</td>") == []


def test_legacy_headers_do_not_make_an_option_complete(tmp_path):
    path = tmp_path / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write(
            json.dumps({"ok": True, "khatiyan_value": "1 ", "cells": ["ମୌଜା : village"]}) + "\n"
        )
    assert fetch_ror.done_khatiyans(path) == set()


def test_boundary_text_cannot_satisfy_legacy_completion():
    row = {"cells": ["ମୌଜା : village", "ଉ:ସରହଦ ମୌଜା: boundary"]}
    assert fetch_ror.legacy_header_only(row)
    assert not fetch_ror.legacy_header_only({**row, "source": {"owner_spans": True}})
    assert not fetch_ror.legacy_header_only({"cells": ["A ଜା: C ବା: ମୌଜା village"]})


def test_long_odia_labels_and_printed_separator_variants():
    person = split_cell("A ପିତା- B ଜାତି- C ବାସସ୍ଥାନ- D")[0]
    assert person == {
        "name": "A",
        "relation": "father",
        "relative_name": "B",
        "caste": "C",
        "residence": "D",
    }
    assert split_cell("A ସ୍ବାମୀ: B ଜା: C ବା: D")[0]["relation"] == "husband"
    assert split_cell("ମୌଜା : village") == []


def test_odia_initial_is_not_a_father_marker():
    person = split_cell("ପି. A ପି:ପି. B ଜା: C ବା: D")[0]
    assert person["name"] == "ପି. A"
    assert person["relative_name"] == "ପି. B"
    assert split_cell("ଜା. A ପି: B ଜା: C")[0]["name"] == "ଜା. A"


@pytest.mark.parametrize("owner", [".", " , ; -- ", "…", "", "A", "ସରକାର"])
def test_html_punctuation_placeholders_stay_retryable(monkeypatch, village, isolated, owner):
    class Session:
        def __init__(self, **kwargs):
            pass

        def open(self):
            pass

        def options(self, control):
            return [("20 ", "20")]

        def submit_bytes(self, *args):
            return (
                '<span id="gvfront_ctl02_lblMouja">ଗ୍ରାମ</span>'
                '<span id="gvfront_ctl02_lblKhatiyanslNo">20</span>'
                f'<span id="gvfront_ctl02_lblName">{owner}</span>'
            ).encode()

    monkeypatch.setattr(fetch_ror, "Session", Session)
    monkeypatch.setattr(fetch_ror, "cascade", lambda *args: {})
    result = fetch_ror.fetch_one(village, "20 ", 0, "20")
    substantive = owner in {"A", "ସରକାର"}
    assert bool(result.error) is not substantive
    path = isolated / "village.jsonl.gz"
    with gzip.open(path, "wt") as stream:
        stream.write(
            json.dumps(
                {
                    "ok": True,
                    "khatiyan_value": "20 ",
                    "cells": result.cells,
                    "source": result.source,
                }
            )
            + "\n"
        )
    assert fetch_ror.done_khatiyans(path) == ({"20 "} if substantive else set())

    counters = {}
    runner = fetch_ror.Runner(0, None, isolated / "events.jsonl", counters)
    runner.village(village)
    with gzip.open(fetch_ror.village_path("1", "2", "4"), "rt") as stream:
        saved = json.loads(next(stream))
    assert saved["ok"] is substantive
    assert counters.get("ok", 0) == int(substantive)
    assert counters.get("failed", 0) == int(not substantive)
