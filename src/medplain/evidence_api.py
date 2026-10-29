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
