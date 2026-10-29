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


def deployment_script(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    return load_script("deploy_aws")


def test_staging_plan_has_private_project_scope_and_content_address(monkeypatch):
    deployment = deployment_script(monkeypatch)
    plan = deployment.staging_plan("123456789012", "us-east-1", "ab" * 20, "cd" * 32)
    assert plan == {
        "staging_bucket": "atropos-medplain-deploy-123456789012-us-east-1",
        "staging_key": "releases/" + "ab" * 20 + "/" + "cd" * 32 + ".zip",
        "staging_expiry_days": 1,
        "staging_encryption": "AES256",
    }
    changed = deployment.staging_plan("123456789012", "us-east-1", "ef" * 20, "cd" * 32)
    assert changed["staging_key"] != plan["staging_key"]
    changed_artifact = deployment.staging_plan("123456789012", "us-east-1", "ab" * 20, "01" * 32)
    assert changed_artifact["staging_key"] != plan["staging_key"]


@pytest.mark.parametrize("bucket", [
    "unrelated-private-bucket",
    "atropos-medplain-deploy-999999999999-us-east-1",
    "atropos-medplain-deploy-123456789012-eu-west-1",
    "atropos-medplain-deploy-123456789012-us-east-1/elsewhere",
    "atropos-medplain-deploy-123456789012-us-east-1-",
])
def test_staging_plan_refuses_an_unrelated_bucket(monkeypatch, bucket):
    deployment = deployment_script(monkeypatch)
    with pytest.raises(ValueError, match="project's account and region prefix"):
        deployment.staging_plan("123456789012", "us-east-1", "ab" * 20, "cd" * 32, bucket)


def test_staging_plan_allows_only_a_scoped_suffix(monkeypatch):
    deployment = deployment_script(monkeypatch)
    bucket = "atropos-medplain-deploy-123456789012-us-east-1-ci"
    assert deployment.staging_plan("123456789012", "us-east-1", "ab" * 20, "cd" * 32, bucket)["staging_bucket"] == bucket
    with pytest.raises(ValueError, match="full source commit"):
        deployment.staging_plan("123456789012", "us-east-1", "main", "cd" * 32)


def test_staging_checks_ownership_before_mutating_an_existing_bucket(monkeypatch):
    import argparse

    deployment = deployment_script(monkeypatch)
    calls = []

    def existing_without_tag(args, *command, **kwargs):
        calls.append(command[:2])
        if command[:2] == ("s3api", "head-bucket"):
            return {}
        if command[:2] == ("s3api", "get-bucket-tagging"):
            return {"TagSet": [{"Key": "Project", "Value": "AnotherProject"}]}
        raise AssertionError("attempted a mutation before proving project ownership")

    monkeypatch.setattr(deployment, "cli", existing_without_tag)
    args = argparse.Namespace(account="123456789012", region="us-east-1")
    plan = deployment.staging_plan(args.account, args.region, "ab" * 20, "cd" * 32)
    with pytest.raises(ValueError, match="ownership tag"):
        deployment.stage_package(args, plan, "cd" * 32)
    assert calls == [("s3api", "head-bucket"), ("s3api", "get-bucket-tagging")]
