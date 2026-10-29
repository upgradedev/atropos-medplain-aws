"""Public comparison evidence must identify the shipped source and exact report."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from medplain import evidence_api
from medplain.api import app
from medplain.gate import canonical_hash


def report_for(source="a" * 40):
    report = {"schema_version": 1, "source_commit": source, "mode": "fixture",
              "arms": {"strands": {}, "baseline": {}}, "cases": []}
    return {**report, "report_sha256": canonical_hash(report)}


def test_public_evidence_is_anonymous_and_matches_build_and_hash(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    report = report_for()
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(evidence_api, "REPORT_PATH", path)
    monkeypatch.setattr(evidence_api, "commit", lambda: "a" * 40)
    response = TestClient(app).get("/api/evaluations")
    assert response.status_code == 200
    assert response.json() == report
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", ["missing", "corrupt", "tamper", "stale", "schema"])
def test_missing_stale_or_corrupt_evidence_never_invents_scores(tmp_path, monkeypatch, change):
    path = tmp_path / "report.json"
    report = report_for()
    if change == "tamper":
        report["arms"]["strands"]["completed"] = 999
    if change == "schema":
        report["schema_version"] = 99
        report["report_sha256"] = canonical_hash({k: v for k, v in report.items() if k != "report_sha256"})
    if change != "missing":
        path.write_text("invalid json" if change == "corrupt" else json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(evidence_api, "commit", lambda: ("b" if change == "stale" else "a") * 40)
    with pytest.raises(HTTPException) as caught:
        evidence_api.read_evaluation_report(path)
    assert caught.value.status_code == 503
    assert "No comparison scores" in caught.value.detail
