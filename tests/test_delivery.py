"""Independent checks for evidence verification and package provenance."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def independent_chain():
    first = {
        "sequence": 1, "session_id": "independent-fixture", "source_hash": "a" * 64,
        "artifact_hash": "b" * 64, "previous_hash": "0" * 64,
        "verdict": "refused", "discrepancies": ["dose changed"], "timestamp": "2026-10-29T00:00:00Z",
    }
    first["hash"] = hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    second = {
        "sequence": 2, "session_id": "independent-fixture", "source_hash": "a" * 64,
        "artifact_hash": "c" * 64, "previous_hash": first["hash"],
        "verdict": "verified", "discrepancies": [], "timestamp": "2026-10-29T00:00:01Z",
    }
    second["hash"] = hashlib.sha256(json.dumps(second, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return [first, second]


def test_external_chain_reader_accepts_complete_chain_and_refuses_mutation():
    reader = load_script("live_smoke")
    receipts = independent_chain()
    assert reader.verify_chain(receipts)
    changed = copy.deepcopy(receipts)
    changed[1]["artifact_hash"] = "d" * 64
    assert not reader.verify_chain(changed)
    assert not reader.verify_chain(receipts[1:])
    assert not reader.verify_chain([])


@pytest.mark.parametrize("field,value", [("sequence", 9), ("session_id", "different"), ("source_hash", "e" * 64), ("previous_hash", "f" * 64)])
def test_chain_reader_refuses_recomputed_but_unbound_chain(field, value):
    reader = load_script("live_smoke")
    receipts = independent_chain()
    receipt = receipts[1]
    receipt[field] = value
    receipt.pop("hash")
    receipt["hash"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert not reader.verify_chain(receipts)


def test_linux_package_records_exact_commit_and_artifact_hash(tmp_path):
    package = load_script("package_lambda")
    dependencies = tmp_path / "dependencies"
    source = tmp_path / "source"
    for directory in [dependencies / "strands", dependencies / "mangum", source / "medplain"]:
        directory.mkdir(parents=True)
    (source / "medplain" / "api.py").write_text("handler = None\n")
    output = tmp_path / "package.zip"
    result = package.package(dependencies, source, output, "1" * 40)
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert json.loads(output.with_suffix(".json").read_text())["commit"] == "1" * 40
    with zipfile.ZipFile(output) as archive:
        assert "medplain/api.py" in archive.namelist()
        assert json.loads(archive.read("medplain/_build.json"))["commit"] == "1" * 40
    with pytest.raises(ValueError, match="full lowercase Git SHA"):
        package.package(dependencies, source, output, "main")
