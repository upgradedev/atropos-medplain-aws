"""Deterministic clinical fact operations. No diagnosis or prescribing."""

from decimal import Decimal

from medplain.schemas import (
    ClinicalPacket,
    LabExplanation,
    MedicationInstruction,
    PatientExplanation,
)

FREQUENCIES = {
    "QD": "once a day",
    "BID": "twice a day",
    "TID": "three times a day",
    "QHS": "at bedtime",
}
UCUM_CODES = {"10^9/L": "10*9/L", "ratio": "1"}


def medication_source_line(med) -> str:
    return (
        f"{med.id} | {med.name} | {med.dose} {med.unit} | {med.frequency} | "
        f"{med.status} | {med.route} | {med.instruction}"
    )


def lab_source_line(lab) -> str:
    return f"{lab.id} | {lab.name} | {lab.value} {lab.unit} | reference {lab.low} to {lab.high} {lab.unit}"


def render_source_profile(packet: ClinicalPacket) -> str:
    """Canonical text for the supported source profile, including every narrative field."""
    return "\n".join([
        "NORTH HARBOR TEACHING HOSPITAL | SYNTHETIC TRAINING RECORD",
        f"Packet: {packet.packet_id} | Patient alias: {packet.patient}",
        f"DISCHARGE SUMMARY: {packet.summary}",
        "DISCHARGE MEDICATION RECONCILIATION",
        "ID | medication | dose | frequency | status | route | source instruction",
        *[medication_source_line(m) for m in packet.medications],
        "LABORATORY RESULTS (source laboratory reference intervals)",
        *[lab_source_line(lab) for lab in packet.labs],
        "FOLLOW-UP", *packet.follow_up, "RETURN PRECAUTIONS", *packet.red_flags,
        "Training notice: all people, encounters and results are invented.",
    ])


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError('Source fact does not match the discharge manifest')


def parse_packet(packet: ClinicalPacket) -> ClinicalPacket:
    """Cross-check the manifest against the source table AND FHIR resources.

    Supports only the published demo profile: MedicationRequest and Observation.
    Rejects ambiguous, duplicate, missing, or inconsistent facts. The public API
    selects shipped packets and does not ingest private health records.
    """
    if packet.fhir.get("resourceType") != "Bundle" or packet.fhir.get("type") != "collection":
        raise ValueError("Unsupported FHIR bundle profile")
    if packet.source_text != render_source_profile(packet):
        raise ValueError("Source narrative or clinical table differs from the supported discharge profile")
    resources = [item["resource"] for item in packet.fhir.get("entry", [])]
    medication_resources = [r for r in resources if r.get("resourceType") == "MedicationRequest"]
    lab_resources = [r for r in resources if r.get("resourceType") == "Observation"]
    if len(resources) != len(medication_resources) + len(lab_resources):
        raise ValueError("Unsupported FHIR resource type")
    ids = [r.get("id") for r in resources]
    expected_ids = [m.id for m in packet.medications] + [lab.id for lab in packet.labs]
    if len(set(ids)) != len(ids) or len(set(expected_ids)) != len(expected_ids) or set(ids) != set(expected_ids):
        raise ValueError("Missing or duplicate clinical fact identifiers")
    if len(medication_resources) != len(packet.medications) or len(lab_resources) != len(packet.labs):
        raise ValueError("Source fact count mismatch")
    by_id = {r["id"]: r for r in resources}
    source_lines = packet.source_text.splitlines()
    for med in packet.medications:
        r = by_id[med.id]
        try:
            instructions = r["dosageInstruction"]
            if len(instructions) != 1 or len(instructions[0]["doseAndRate"]) != 1:
                raise ValueError("Ambiguous medication dosage")
            instruction = instructions[0]
            quantity = instruction["doseAndRate"][0]["doseQuantity"]
            _require(r["resourceType"] == "MedicationRequest")
            _require(r["subject"]["display"] == packet.patient)
            _require(r["medicationCodeableConcept"]["text"] == med.name)
            _require(Decimal(str(quantity["value"])) == Decimal(med.dose))
            _require(quantity["unit"] == med.unit)
            _require(quantity["code"] == med.unit)
            _require(quantity["system"] == "http://unitsofmeasure.org")
            _require(len(instruction["timing"]["code"]["coding"]) == 1)
            _require(instruction["timing"]["code"]["coding"][0]["code"] == med.frequency)
            _require(instruction["route"]["text"] == med.route)
            _require(instruction["text"] == med.instruction)
            _require(r["status"] == med.status)
            _require(source_lines.count(medication_source_line(med)) == 1)
        except (KeyError, IndexError, AssertionError, TypeError) as exc:
            raise ValueError(f"Source medication mismatch: {med.id}") from exc
    for lab in packet.labs:
        r = by_id[lab.id]
        try:
            _require(r["resourceType"] == "Observation")
            _require(r["subject"]["display"] == packet.patient)
            _require(r["code"]["text"] == lab.name)
            _require(Decimal(str(r["valueQuantity"]["value"])) == Decimal(lab.value))
            _require(r["valueQuantity"]["unit"] == lab.unit)
            _require(r["valueQuantity"]["code"] == UCUM_CODES.get(lab.unit, lab.unit))
            _require(r["valueQuantity"]["system"] == "http://unitsofmeasure.org")
            _require(len(r["referenceRange"]) == 1)
            reference = r["referenceRange"][0]
            _require(Decimal(str(reference["low"]["value"])) == Decimal(lab.low))
            _require(Decimal(str(reference["high"]["value"])) == Decimal(lab.high))
            _require(reference["low"]["unit"] == reference["high"]["unit"] == lab.unit)
            _require(reference["low"]["code"] == reference["high"]["code"] == UCUM_CODES.get(lab.unit, lab.unit))
            _require(source_lines.count(lab_source_line(lab)) == 1)
        except (KeyError, IndexError, AssertionError, TypeError) as exc:
            raise ValueError(f"Source lab mismatch: {lab.id}") from exc
    if any(Decimal(lab.low) > Decimal(lab.high) for lab in packet.labs):
        raise ValueError("Reference range lower bound exceeds upper bound")
    return packet


def evaluate_labs(packet: ClinicalPacket) -> list[dict]:
    """Classify against the source laboratory's ranges, never a general range."""
    results = []
    for lab in packet.labs:
        value, low, high = map(Decimal, [lab.value, lab.low, lab.high])
        status = "low" if value < low else "high" if value > high else "within range"
        results.append({**lab.model_dump(), "status": status})
    return results


def build_explanation(packet: ClinicalPacket) -> PatientExplanation:
    """A constrained clinical renderer supplies all publishable patient facts.

    Models may call tools, propose and route. They cannot add patient-facing
    advice. Fixed reviewed language is an intentional scope limit of this demo.
    """
    medications = []
    for med in packet.medications:
        action = "STOP" if med.status == "stopped" else "Take"
        medications.append(MedicationInstruction(
            medication_id=med.id,
            name=med.name,
            dose=med.dose,
            unit=med.unit,
            frequency=med.frequency,
            status=med.status,
            text=(
                f"{action} {med.name}: {med.dose} {med.unit}, {FREQUENCIES[med.frequency]} "
                f"({med.frequency}). {med.instruction}"
            ),
        ))
    labs = [LabExplanation(
        lab_id=lab["id"], name=lab["name"], value=lab["value"], unit=lab["unit"],
        low=lab["low"], high=lab["high"], status=lab["status"],
        text=(f"{lab['plain_label']}: {lab['value']} {lab['unit']}. "
              f"The source lab's reference range is {lab['low']} to {lab['high']} {lab['unit']}. "
              f"This result is {lab['status']}. Your care team can explain what it means for you."),
    ) for lab in evaluate_labs(packet)]
    return PatientExplanation(
        summary=packet.summary, medications=medications, labs=labs,
        follow_up=list(packet.follow_up), red_flags=list(packet.red_flags),
    )


def render_explanation(explanation: PatientExplanation) -> str:
    return "\n\n".join([
        explanation.summary,
        *[m.text for m in explanation.medications],
        *[lab.text for lab in explanation.labs],
        "Follow-up\n" + "\n".join(explanation.follow_up),
        "When to get help\n" + "\n".join(explanation.red_flags),
    ])


def check_medication_parity(packet: ClinicalPacket, explanation: PatientExplanation) -> list[str]:
    expected = {m.medication_id: m for m in build_explanation(packet).medications}
    actual = {m.medication_id: m for m in explanation.medications}
    errors = []
    if len(actual) != len(explanation.medications):
        errors.append("Duplicate medication identifiers in proposed explanation")
    for med_id in sorted(expected.keys() | actual.keys()):
        if med_id not in expected or med_id not in actual:
            errors.append(f"Medication {med_id} missing or added")
            continue
        left, right = expected[med_id].model_dump(), actual[med_id].model_dump()
        for field in left:
            if left[field] != right[field]:
                errors.append(f"Medication {med_id} {field}: expected {left[field]!r}; got {right[field]!r}")
    return errors


def check_interactions(packet: ClinicalPacket) -> list[dict]:
    """Small, explicitly non-exhaustive demonstration matrix, no clinical rules engine.

    Entries are grounded in the cited official medicine guidance. We report a review flag even
    for a stopped medicine to demonstrate the importance of stop directives.
    Absence from this matrix never means a combination is safe.
    """
    names = {med.name.casefold(): med for med in packet.medications}
    pairs = [
        ("lisinopril", "potassium chloride", "Potassium monitoring review",
         "The source includes a medicine that can raise potassium and a stopped potassium supplement. "
         "Keep the source stop instruction and ask the pharmacist to review the list.",
         "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=7885537f-88b2-4d39-bcd2-cad9b2e5fbe5"),
        ("warfarin", "ibuprofen", "Bleeding-risk review",
         "These medicines can increase bleeding risk together. The source says to stop ibuprofen. "
         "Ask the pharmacist to confirm your discharge list; do not change it using this demo.",
         "https://www.nhs.uk/medicines/warfarin/"),
    ]
    found = []
    for left, right, title, message, source in pairs:
        if left in names and right in names:
            found.append({"medications": [names[left].name, names[right].name],
                          "title": title, "message": message, "source": source,
                          "severity": "pharmacist review", "scope": "Curated demo pair; not exhaustive"})
    return found
