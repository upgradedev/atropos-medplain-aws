"""Bounded raw-note intake with an independent, fully consumed source ledger.

This profile accepts new invented values and names. It is not a PDF extractor,
general FHIR importer, PHI detector, clinical appropriateness check or diagnosis.
The deterministic normalizer is also the unhandicapped evaluation baseline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from medplain.clinical import UCUM_CODES, parse_packet, render_source_profile
from medplain.gate import canonical_hash
from medplain.schemas import ClinicalPacket, Lab, Medication, SourceExtraction

PROFILE = "medplain-discharge-note-v1"
SOURCE_SYSTEM = "urn:medplain:raw-note-sha256"
HEADER = "MEDPLAIN SYNTHETIC DISCHARGE V1"
MAX_SOURCE_CHARS = 8000
MAX_LINES = 25
FREQUENCIES = {
    "once daily": "QD", "twice daily": "BID", "three times daily": "TID", "at bedtime": "QHS",
    "QD": "QD", "BID": "BID", "TID": "TID", "QHS": "QHS",
}
FOLLOW_UP = (
    "Contact the clinic to review your medicines.",
    "Arrange a follow-up appointment with your care team.",
    "Ask your pharmacist to review the discharge list.",
)
RED_FLAGS = (
    "Seek urgent care for severe trouble breathing.",
    "Seek urgent care for chest pain or fainting.",
    "Contact your care team if symptoms worsen.",
)
NUMBER = r"-?(?:0|[1-9]\d{0,5})(?:\.\d{1,4})?"
NAME = r"[A-Za-z]+(?:[ -][A-Za-z]+){0,7}"
MEDICATION_LINE = re.compile(
    rf"(?P<action>Take|Stop|Do not take) (?P<name>{NAME}) (?P<dose>{NUMBER}) "
    r"(?P<unit>mg|mcg|mL) by mouth (?P<frequency>once daily|twice daily|three times daily|at bedtime|QD|BID|TID|QHS)\."
)
LAB_LINE = re.compile(
    rf"Lab (?P<name>{NAME}): (?P<value>{NUMBER}) (?P<unit>mmol/L|mg/dL|g/dL|10\^9/L|mL/min|ratio) "
    rf"\(reference (?P<low>{NUMBER}) to (?P<high>{NUMBER})\)\."
)
FORBIDDEN_NAME_WORDS = re.compile(
    r"\b(take|stop|not|no|and|or|except|instead|mg|mcg|ml|bid|tid|qhs|qd|daily|bedtime|reference|by|mouth)\b",
    re.IGNORECASE,
)

TEMPLATE = "\n".join([
    HEADER,
    "Synthetic alias: Demo-River",
    "Take Amoxicillin 500 mg by mouth three times daily.",
    "Take Levothyroxine 50 mcg by mouth once daily.",
    "Do not take Ibuprofen 200 mg by mouth twice daily.",
    "Lab Hemoglobin: 12.6 g/dL (reference 12 to 16).",
    "Lab Sodium: 138 mmol/L (reference 135 to 145).",
    "Follow-up: " + FOLLOW_UP[0],
    "Get help: " + RED_FLAGS[0],
])


class IntakeError(ValueError):
    """An unsupported or ambiguous source is refused before inference."""


@dataclass(frozen=True)
class IntakeBundle:
    packet: ClinicalPacket
    intake: dict
    expected_extraction: SourceExtraction
    ledger: tuple[dict, ...]


def source_hash(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def template() -> dict:
    return {
        "profile": PROFILE, "source_text": TEMPLATE,
        "limits": {"max_source_chars": MAX_SOURCE_CHARS, "max_lines": MAX_LINES,
                   "max_medications": 6, "max_labs": 6},
        "format_hints": [
            "Keep the exact header and a Synthetic alias: Demo-... line.",
            "Medicine: Take, Stop, or Do not take NAME DOSE mg|mcg|mL by mouth FREQUENCY.",
            "Frequencies: once daily, twice daily, three times daily, at bedtime, QD, BID, TID, QHS.",
            "Lab NAME: VALUE UNIT (reference LOW to HIGH). Units: mmol/L, mg/dL, g/dL, 10^9/L, mL/min, ratio.",
            "Use one to six unique medicines and one to six unique labs. Names contain letters, spaces or hyphens.",
            "Follow-up: " + " | ".join(FOLLOW_UP),
            "Get help: " + " | ".join(RED_FLAGS),
            "One to three unique Follow-up and Get help lines are required. Other lines are refused.",
        ],
        "privacy_notice": (
            "Submit only invented notes. Do not enter real patient information. Confirmation does not detect "
            "or remove private data. Accepted source text is stored in this anonymous demo session and sent "
            "to the configured model. A reviewer-released artifact has a public capability link. "
            "This profile checks source fidelity, not clinical appropriateness."
        ),
    }


def _invalid(line: int, reason: str) -> None:
    raise IntakeError(f"Line {line}: {reason}")


def _name(name: str, line: int) -> str:
    if len(name) > 60 or FORBIDDEN_NAME_WORDS.search(name):
        _invalid(line, "unsupported or ambiguous medicine or lab name")
    return name


def normalize_source(source_text: str) -> IntakeBundle:
    """Consume every source line and compile a canonical packet without an LLM.

    Kept independent of model output. The parser Agent must separately extract
    the typed facts from raw lines, then match this ledger at its lifecycle hook.
    Source quotes are full lines; offsets bind the exact original UTF-8 bytes.
    """
    if (not isinstance(source_text, str) or not 1 <= len(source_text) <= MAX_SOURCE_CHARS
            or any(ord(char) > 126 or (ord(char) < 32 and char not in "\r\n") for char in source_text)):
        raise IntakeError("Use a bounded plain ASCII synthetic note, without tabs or control characters.")
    chunks = source_text.splitlines(keepends=True)
    if not 3 <= len(chunks) <= MAX_LINES:
        raise IntakeError("The note exceeds the supported line count or is incomplete.")
    lines = [chunk.rstrip("\r\n") for chunk in chunks]
    if lines[0] != HEADER:
        _invalid(1, "expected the exact synthetic discharge profile header")
    alias = re.fullmatch(r"Synthetic alias: (?P<alias>Demo-[A-Za-z0-9-]{1,32})", lines[1])
    if alias is None:
        _invalid(2, "expected a Synthetic alias: Demo-... identifier")
    extracted = {"source_sha256": source_hash(source_text), "patient_alias": alias["alias"],
                 "medications": [], "labs": [], "follow_up": [], "red_flags": []}
    ledger, offset = [], 0
    medication_names, lab_names, statements = set(), set(), set()
    for index, (line, chunk) in enumerate(zip(lines, chunks), start=1):
        if len(line) > 320 or "\r" in line:
            _invalid(index, "line is too long or contains unsupported whitespace")
        span = {"source_line": index, "source_quote": line}
        item = {"line": index, "start": offset, "end": offset + len(line), "text": line}
        offset += len(chunk)
        if index <= 2:
            ledger.append({**item, "kind": "header" if index == 1 else "alias"})
            continue
        if line == "":
            ledger.append({**item, "kind": "blank"})
            continue
        if match := MEDICATION_LINE.fullmatch(line):
            data = match.groupdict()
            name = _name(data["name"], index)
            if name.casefold() in medication_names or len(extracted["medications"]) >= 6:
                _invalid(index, "duplicate medicine or medicine limit exceeded")
            if not 0 < Decimal(data["dose"]) <= 100000:
                _invalid(index, "dose must be positive and within the supported numeric bound")
            medication_names.add(name.casefold())
            extracted["medications"].append({**span, "name": name, "dose": data["dose"], "unit": data["unit"],
                                             "frequency": FREQUENCIES[data["frequency"]],
                                             "status": "active" if data["action"] == "Take" else "stopped"})
            ledger.append({**item, "kind": "medication"})
        elif match := LAB_LINE.fullmatch(line):
            data = match.groupdict()
            name = _name(data["name"], index)
            if name.casefold() in lab_names or len(extracted["labs"]) >= 6:
                _invalid(index, "duplicate lab or lab limit exceeded")
            if Decimal(data["low"]) > Decimal(data["high"]):
                _invalid(index, "reference range lower bound exceeds its upper bound")
            lab_names.add(name.casefold())
            extracted["labs"].append({**span, **{key: data[key] for key in ["name", "value", "unit", "low", "high"]}})
            ledger.append({**item, "kind": "lab"})
        elif line.startswith("Follow-up: ") or line.startswith("Get help: "):
            kind = "follow_up" if line.startswith("Follow-up: ") else "red_flags"
            statement = line.split(": ", 1)[1]
            if statement not in (FOLLOW_UP if kind == "follow_up" else RED_FLAGS):
                _invalid(index, "use one of the documented follow-up or help statements")
            if line in statements or len(extracted[kind]) >= 3:
                _invalid(index, "duplicate statement or statement limit exceeded")
            statements.add(line)
            extracted[kind].append({**span, "text": statement})
            ledger.append({**item, "kind": kind})
        else:
            _invalid(index, "unsupported, ambiguous, or unconsumed clinical line; use the displayed profile")
    if not all(extracted[key] for key in ["medications", "labs", "follow_up", "red_flags"]):
        raise IntakeError("Include at least one medicine, lab, follow-up and Get help line.")
    extraction = SourceExtraction.model_validate(extracted)
    intake = {"profile": PROFILE, "source_text": source_text, "source_sha256": extracted["source_sha256"],
              "ledger_sha256": canonical_hash(ledger)}
    packet = _compile(extraction)
    return IntakeBundle(packet, intake, extraction, tuple(ledger))


def _quantity(value: str, unit: str) -> dict:
    return {"value": float(value) if "." in value else int(value), "unit": unit,
            "code": UCUM_CODES.get(unit, unit), "system": "http://unitsofmeasure.org"}


def _compile(extraction: SourceExtraction) -> ClinicalPacket:
    medications = [Medication(
        id=f"rx-note-{index:02}", name=med.name, dose=med.dose, unit=med.unit, frequency=med.frequency,
        status=med.status, route="oral", instruction=("The source says not to take this medicine."
             if med.status == "stopped" else "Follow the dose and timing written in the source."),
    ) for index, med in enumerate(extraction.medications, start=1)]
    labs = [Lab(id=f"lab-note-{index:02}", name=lab.name, value=lab.value, unit=lab.unit,
                low=lab.low, high=lab.high, plain_label=lab.name)
            for index, lab in enumerate(extraction.labs, start=1)]
    resources = [{
        "resourceType": "MedicationRequest", "id": med.id, "status": med.status, "intent": "order",
        "subject": {"display": extraction.patient_alias}, "medicationCodeableConcept": {"text": med.name},
        "dosageInstruction": [{"text": med.instruction, "route": {"text": "oral"},
            "timing": {"code": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-GTSAbbreviation",
                                           "code": med.frequency}]}},
            "doseAndRate": [{"doseQuantity": _quantity(med.dose, med.unit)}]}],
    } for med in medications]
    resources += [{"resourceType": "Observation", "id": lab.id, "status": "final",
                   "subject": {"display": extraction.patient_alias}, "code": {"text": lab.name},
                   "valueQuantity": _quantity(lab.value, lab.unit),
                   "referenceRange": [{"low": _quantity(lab.low, lab.unit), "high": _quantity(lab.high, lab.unit)}]}
                  for lab in labs]
    packet = ClinicalPacket(
        packet_id="note-" + extraction.source_sha256[:24], title="Imported note for " + extraction.patient_alias,
        subtitle="New raw note; independent source ledger and parser Agent verification required.",
        patient=extraction.patient_alias, source_text="",
        summary="Discharge medicines and laboratory results from the submitted synthetic note.",
        medications=medications, labs=labs, follow_up=[item.text for item in extraction.follow_up],
        red_flags=[item.text for item in extraction.red_flags],
        fhir={"resourceType": "Bundle", "type": "collection",
              "identifier": {"system": SOURCE_SYSTEM, "value": extraction.source_sha256},
              "entry": [{"resource": resource} for resource in resources]},
    )
    packet.source_text = render_source_profile(packet)
    return parse_packet(packet)


def validate_intake_source(packet: ClinicalPacket, intake: dict) -> IntakeBundle:
    if not isinstance(intake, dict) or set(intake) != {"profile", "source_text", "source_sha256", "ledger_sha256"}:
        raise IntakeError("The saved intake provenance contract is invalid.")
    bundle = normalize_source(intake["source_text"])
    if bundle.intake != intake or bundle.packet.model_dump() != packet.model_dump():
        raise IntakeError("Original source, ledger or normalized packet binding changed.")
    return bundle


def validate_intake_result(packet: ClinicalPacket, result: dict, intake: dict | None) -> None:
    identifier = packet.fhir.get("identifier", {})
    imported = identifier.get("system") == SOURCE_SYSTEM
    if intake is None:
        if imported:
            raise IntakeError("The original imported source is missing.")
        return
    bundle = validate_intake_source(packet, intake)
    if result.get("status") != "verified":
        return
    expected = {
        "profile": PROFILE, "source_sha256": intake["source_sha256"], "ledger_sha256": intake["ledger_sha256"],
        "parser_verified": True, "extraction": bundle.expected_extraction.model_dump(mode="json"),
        "extraction_sha256": canonical_hash(bundle.expected_extraction.model_dump(mode="json")),
    }
    if result.get("intake_validation") != expected:
        raise IntakeError("The parser extraction is not bound to the saved original note.")
