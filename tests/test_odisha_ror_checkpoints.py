"""`read_checkpoints` must match the layout `fetch_ror` writes."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "parse_ror.py"


def _module():
    if not SCRIPT.exists():
        pytest.skip("odisha_ror scripts not present")
    spec = importlib.util.spec_from_file_location("parse_ror", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["parse_ror"] = module
    spec.loader.exec_module(module)
    return module


def _checkpoint(root: Path, district: str, tahsil: str, village: str) -> None:
    """Write one checkpoint exactly where `fetch_ror.village_path` puts it."""
    path = root / f"district_{district}" / f"tahsil_{tahsil}" / f"village_{village}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf8") as handle:
        handle.write(json.dumps({"village_code": village, "khatiyan": "1"}) + "\n")


def test_read_checkpoints_finds_files_under_the_tahsil_level(tmp_path):
    """The tahsil directory is part of the checkpoint key, not decoration.

    `village_path` places checkpoints at district/tahsil/village because
    village_code repeats across tahsils; its docstring records that omitting
    the tahsil merged 322 of 476 villages into shared files. A reader globbing
    district/village therefore matches nothing and returns zero records
    silently, which is indistinguishable from an unfetched corpus.
    """
    root = tmp_path / "ror"
    _checkpoint(root, "24", "1", "105")
    _checkpoint(root, "24", "2", "105")  # same village code, different tahsil
    _checkpoint(root, "24", "3", "77")

    records = _module().read_checkpoints(root)

    assert len(records) == 3, "one record per checkpoint, tahsils kept distinct"


def test_read_checkpoints_returns_empty_for_an_empty_tree(tmp_path):
    """Zero records must mean zero files, so the failure above is diagnosable."""
    root = tmp_path / "ror"
    root.mkdir()
    assert _module().read_checkpoints(root) == []
