"""Fail-closed clinical publication gate and locally verifiable hash-chain receipts."""

import hashlib
import json
from datetime import datetime, timezone

from medplain.clinical import build_explanation, check_medication_parity, parse_packet, render_explanation
from medplain.schemas import ClinicalPacket, GateVerdict, PatientExplanation

GENESIS = "0" * 64


def canonical_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SafetyGate:
    """Require every supported fact AND every word of constrained clinical prose.

    An exact match per record prevents swapping equal-looking numbers between
    medicines. Exact narrative comparison prevents negation, spelled-out dose
    changes, instructions hidden in prose, and extra unsupported advice. This
    is fidelity to this bounded input profile, not clinical validation.
    """

    def __init__(self, packet: ClinicalPacket):
        self.packet = parse_packet(packet.model_copy(deep=True))
        self.expected = build_explanation(self.packet)

    def validate(self, explanation: PatientExplanation) -> GateVerdict:
        errors = check_medication_parity(self.packet, explanation)
        expected_labs = {lab.lab_id: lab.model_dump() for lab in self.expected.labs}
        actual_labs = {lab.lab_id: lab.model_dump() for lab in explanation.labs}
        if len(actual_labs) != len(explanation.labs):
            errors.append("Duplicate lab identifiers")
        if expected_labs != actual_labs:
            errors.append("Lab value, unit, reference range, label or explanation changed")
        for field in ["summary", "follow_up", "red_flags"]:
            if getattr(self.expected, field) != getattr(explanation, field):
                errors.append(f"Unsupported change to patient {field}")
        # Bind ordering too, so receipts identify the exact structured artifact and text.
        if self.expected.model_dump() != explanation.model_dump() and not errors:
            errors.append("Clinical fact ordering changed")
        return GateVerdict(passed=not errors, discrepancies=errors)


class ReceiptChain:
    """Tamper-evident within one request. No signature, ledger or identity proof."""

    def __init__(self, session_id: str, packet: ClinicalPacket):
        self.session_id = session_id
        self.source_hash = canonical_hash(packet.model_dump())
        self.receipts: list[dict] = []

    def append(self, explanation: PatientExplanation, verdict: GateVerdict) -> dict:
        body = {
            "sequence": len(self.receipts) + 1,
            "session_id": self.session_id,
            "source_hash": self.source_hash,
            "artifact_hash": canonical_hash({"structured": explanation.model_dump(),
                                              "text": render_explanation(explanation)}),
            "previous_hash": self.receipts[-1]["hash"] if self.receipts else GENESIS,
            "verdict": "verified" if verdict.passed else "refused",
            "discrepancies": list(verdict.discrepancies),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        receipt = {**body, "hash": canonical_hash(body)}
        self.receipts.append(receipt)
        return receipt


def verify_receipts(receipts: list[dict]) -> bool:
    """Detect edited/reordered/mixed-session receipts; retain last hash externally.

    A rewritten entire chain or truncated suffix needs an external trusted final
    hash to detect. These unsigned receipts do not claim authenticity.
    """
    if not receipts:
        return False
    previous = GENESIS
    session = receipts[0].get("session_id")
    source_hash = receipts[0].get("source_hash")
    try:
        for index, receipt in enumerate(receipts, start=1):
            body = {key: value for key, value in receipt.items() if key != "hash"}
            if (receipt["sequence"] != index or receipt["previous_hash"] != previous
                    or receipt["session_id"] != session or receipt["source_hash"] != source_hash
                    or receipt["verdict"] not in {"refused", "verified"}
                    or bool(receipt["discrepancies"]) != (receipt["verdict"] == "refused")
                    or canonical_hash(body) != receipt["hash"]):
                return False
            previous = receipt["hash"]
    except (KeyError, TypeError, ValueError):
        return False
    return True
