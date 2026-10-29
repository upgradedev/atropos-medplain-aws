"""Read-only publication of checked, source-bound Bedrock measurement metadata.

There is no HTTP write route. A release operator publishes this small manifest
only after checking the complete public report; raw cases remain in that report.
The source-specific record never substitutes for a missing measurement.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit

from medplain.gate import canonical_hash

PROFILE = "medplain-reconciliation-v1"
SK = "RECONCILIATION"
_HASH = re.compile(r"[0-9a-f]{64}")


def record_key(source_commit: str) -> str:
    return "EVALUATION#" + source_commit


def validate_manifest(value: dict, source_commit: str) -> dict:
    """Check display integrity and identity, without recalculating report scores."""
    if (not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False).encode()) > 65536
            or value.get("schema_version") != 1 or value.get("status") != "published"
            or value.get("profile") != PROFILE or value.get("mode") != "bedrock"
            or value.get("source_commit") != source_commit or value.get("harness_commit") != source_commit
            or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
            or not isinstance(value.get("arms"), dict) or set(value["arms"]) != {"strands", "baseline"}):
        raise ValueError("Unsupported or stale live evaluation manifest")
    for field in ["report_sha256", "corpus_sha256", "ruler_sha256", "manifest_sha256"]:
        if not isinstance(value.get(field), str) or not _HASH.fullmatch(value[field]):
            raise ValueError("Missing evaluation content binding")
    if canonical_hash({k: v for k, v in value.items() if k != "manifest_sha256"}) != value["manifest_sha256"]:
        raise ValueError("Live evaluation manifest integrity mismatch")
    url = urlsplit(value.get("report_url", ""))
    if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
            or not re.fullmatch(r"/upgradedev/atropos-medplain-aws/releases/download/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.json", url.path)):
        raise ValueError("Raw report URL is outside the public project release")
    for arm in value["arms"].values():
        if not isinstance(arm, dict) or type(arm.get("case_count")) is not int or arm["case_count"] != 30:
            raise ValueError("This measurement requires all thirty corpus rows, including failures")
        for field in ["verified_count", "workflow_refused_count", "runtime_error_count", "not_executed_count",
                      "source_coverage_count", "source_citations_count", "supported_atoms_exact_count",
                      "proper_withholding_count"]:
            if type(arm.get(field)) is not int or not 0 <= arm[field] <= arm["case_count"]:
                raise ValueError("Missing or invalid measurement denominator")
        for field in ["question_focus", "structural_focus", "selected_interaction_review"]:
            focus = arm.get(field)
            if not isinstance(focus, dict):
                raise ValueError("Missing independently scored focus measurement")
            for metric in ["precision", "recall", "f1"]:
                number = focus.get(metric)
                if number is not None and (type(number) not in {int, float} or not math.isfinite(number)
                                           or not 0 <= number <= 1):
                    raise ValueError("Invalid focus metric")
    return value
