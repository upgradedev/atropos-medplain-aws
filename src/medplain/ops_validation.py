"""Independent source, artifact and receipt checks at durable publication boundaries.

Persistence is not a safety verdict. Workers, review decisions and public reads
must independently bind the exact saved source to the exact saved explanation.
These checks establish fidelity within the synthetic profile, not authorship or
the clinical appropriateness of the original prescription.
"""

from __future__ import annotations

import re
from typing import Any

from medplain.clinical import render_explanation
from medplain.gate import SafetyGate, canonical_hash, verify_receipts
from medplain.schemas import ClinicalPacket, PatientExplanation

_HASH = re.compile(r"[0-9a-f]{64}")
_RECEIPT_FIELDS = {
    "sequence", "session_id", "source_hash", "artifact_hash", "previous_hash",
    "verdict", "discrepancies", "timestamp", "hash",
}


def validate_result(packet: ClinicalPacket, result: dict[str, Any], execution_id: str,
                    intake_source: dict | None = None) -> str | None:
    """Return the verified artifact hash, or None for a well-formed refusal.

    Raise ValueError for inconsistent persisted evidence, including a valid
    unsigned chain that belongs to a different source or job generation. A
    refusal may have no receipts if execution failed before any candidate was
    evaluated. It must never carry patient content.
    """
    from medplain.intake import validate_intake_result

    if (not isinstance(result, dict) or result.get("status") not in {"verified", "refused"}
            or not execution_id or result.get("session_id") != execution_id):
        raise ValueError("Workflow evidence does not belong to this execution")
    validate_intake_result(packet, result, intake_source)
    if result.get("model_mode") not in {"fixture", "bedrock"}:
        raise ValueError("Unknown inference mode in workflow evidence")
    receipts = result.get("receipts")
    if not isinstance(receipts, list) or len(receipts) > 20:
        raise ValueError("Invalid receipt collection")
    source_hash = canonical_hash(packet.model_dump())
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
            raise ValueError("Invalid receipt contract")
        if (receipt.get("session_id") != execution_id or receipt.get("source_hash") != source_hash
                or not isinstance(receipt.get("discrepancies"), list)
                or any(not isinstance(item, str) for item in receipt["discrepancies"])
                or not isinstance(receipt.get("timestamp"), str) or not receipt["timestamp"]
                or any(not isinstance(receipt.get(key), str) or not _HASH.fullmatch(receipt[key])
                       for key in ("hash", "previous_hash", "artifact_hash", "source_hash"))):
            raise ValueError("Receipt is not bound to the saved source and execution")
    if receipts and not verify_receipts(receipts):
        raise ValueError("Receipt chain integrity check failed")
    if result["status"] == "refused":
        if result.get("explanation") is not None or result.get("explanation_text") is not None:
            raise ValueError("A refused workflow attempted to publish patient content")
        if receipts and receipts[-1]["verdict"] != "refused":
            raise ValueError("Refused workflow has inconsistent final evidence")
        return None
    if (not receipts or receipts[-1]["verdict"] != "verified" or result.get("error") is not None
            or any(receipt["verdict"] != "refused" for receipt in receipts[:-1])):
        raise ValueError("Verified workflow has no unique final publication verdict")
    if (type(result.get("correction_count")) is not int
            or result["correction_count"] != len(receipts) - 1):
        raise ValueError("Correction count differs from the saved receipt history")
    try:
        candidate = PatientExplanation.model_validate(result.get("explanation"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid patient artifact contract") from exc
    text = render_explanation(candidate)
    artifact_hash = canonical_hash({"structured": candidate.model_dump(), "text": text})
    if (not SafetyGate(packet).validate(candidate).passed or result.get("explanation_text") != text
            or receipts[-1]["artifact_hash"] != artifact_hash):
        raise ValueError("Saved artifact differs from the source-derived patient explanation")
    return artifact_hash
