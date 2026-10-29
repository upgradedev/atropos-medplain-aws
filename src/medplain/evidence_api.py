"""Read-only, source-bound comparison evidence produced by CI."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from medplain.gate import canonical_hash
from medplain.ops_domain import commit

router = APIRouter()
REPORT_PATH = Path(__file__).parent / "data" / "evaluation_report.json"
RECONCILIATION_REPORT_PATH = Path(__file__).parent / "data" / "reconciliation_report.json"


def read_evaluation_report(path: Path = REPORT_PATH) -> dict:
    """Never replace missing, stale or corrupt measurements with sample scores."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Expected an evaluation report")
        source = report.get("source_commit", "")
        if (report.get("schema_version") != 1 or not isinstance(source, str)
                or not re.fullmatch(r"[0-9a-f]{40}", source)
                or report.get("mode") not in {"fixture", "bedrock"}
                or not isinstance(report.get("arms"), dict)
                or not isinstance(report.get("cases"), list)):
            raise ValueError("Unsupported evaluation report")
        digest = report.get("report_sha256")
        payload = {key: value for key, value in report.items() if key != "report_sha256"}
        if not isinstance(digest, str) or digest != canonical_hash(payload):
            raise ValueError("Evaluation report integrity mismatch")
        deployed = commit()
        if re.fullmatch(r"[0-9a-f]{40}", deployed) and source != deployed:
            raise ValueError("Evaluation report is for a different source commit")
        return report
    except (OSError, ValueError, TypeError):
        raise HTTPException(
            503, "Evaluation evidence is unavailable for this build. No comparison scores are being displayed.",
        ) from None


@router.get("/api/evaluations")
def evaluations():
    return read_evaluation_report(REPORT_PATH)


@router.get("/api/reconciliation-evaluations")
def reconciliation_evaluations():
    report = read_evaluation_report(RECONCILIATION_REPORT_PATH)
    if report.get("profile") != "medplain-reconciliation-v1" or report.get("mode") != "fixture":
        raise HTTPException(503, "The source-bound offline reconciliation report is unavailable.")
    return report


@router.get("/api/reconciliation-evaluations/live")
def live_reconciliation_evaluations():
    from medplain.live_evaluation import SK, record_key, validate_manifest
    from medplain.ops_store import StoreUnavailable, get_store

    source = commit()
    try:
        record = get_store().get(record_key(source), SK)
        if record is None:
            return {"status": "not_measured", "source_commit": source,
                    "reason": "No actual Bedrock reconciliation measurement has been published for this exact source. No scores are substituted."}
        return validate_manifest(record["manifest"], source)
    except (StoreUnavailable, ValueError, TypeError, KeyError):
        raise HTTPException(503, "Live evaluation evidence is unavailable. No comparison scores are substituted.") from None
