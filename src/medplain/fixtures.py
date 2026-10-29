"""Wholly invented discharge packets. No real patient data or clinical efficacy claim."""

from medplain.clinical import lab_source_line, medication_source_line
from medplain.schemas import ClinicalPacket, Lab, Medication


def _quantity(value: str, unit: str) -> dict:
    number = float(value) if "." in value else int(value)
    return {"value": number, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit}


def _packet(packet_id, title, subtitle, patient, summary, medications, labs, follow_up, red_flags,
            adversarial=False):
    medications = [Medication.model_validate(m) for m in medications]
    labs = [Lab.model_validate(lab) for lab in labs]
    source = "\n".join([
        "NORTH HARBOR TEACHING HOSPITAL | SYNTHETIC TRAINING RECORD",
        f"Packet: {packet_id} | Patient alias: {patient}",
        f"DISCHARGE SUMMARY: {summary}",
        "DISCHARGE MEDICATION RECONCILIATION",
        "ID | medication | dose | frequency | status | route | source instruction",
        *[medication_source_line(m) for m in medications],
        "LABORATORY RESULTS (source laboratory reference intervals)",
        *[lab_source_line(lab) for lab in labs],
        "FOLLOW-UP", *follow_up, "RETURN PRECAUTIONS", *red_flags,
        "Training notice: all people, encounters and results are invented.",
    ])
    resources = [{
        "resourceType": "MedicationRequest", "id": med.id,
        "status": med.status, "intent": "order", "subject": {"display": patient},
        "medicationCodeableConcept": {"text": med.name},
        "dosageInstruction": [{
            "text": med.instruction, "route": {"text": med.route},
            "timing": {"code": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-GTSAbbreviation",
                                           "code": med.frequency}]}},
            "doseAndRate": [{"doseQuantity": _quantity(med.dose, med.unit)}],
        }],
    } for med in medications]
    resources += [{
        "resourceType": "Observation", "id": lab.id, "status": "final",
        "subject": {"display": patient}, "code": {"text": lab.name},
        "valueQuantity": _quantity(lab.value, lab.unit),
        "referenceRange": [{"low": _quantity(lab.low, lab.unit), "high": _quantity(lab.high, lab.unit)}],
    } for lab in labs]
    return ClinicalPacket(
        packet_id=packet_id, title=title, subtitle=subtitle, patient=patient,
        source_text=source, summary=summary, medications=medications, labs=labs,
        follow_up=follow_up, red_flags=red_flags, adversarial=adversarial,
        fhir={"resourceType": "Bundle", "type": "collection",
              "entry": [{"resource": resource} for resource in resources]},
    )


def get_packets() -> list[ClinicalPacket]:
    """Fresh instances isolate each session and fault injection from all other visits."""
    return [
        _packet(
            "home-after-pneumonia", "Home after pneumonia", "Antibiotic, liquid medicine and lab ranges",
            "Jordan (synthetic)",
            "You were treated for a lung infection. Your discharge team says you can recover at home. "
            "Follow the medicine list and contact your care team if recovery does not go as planned.",
            [
                {"id": "rx-doxy", "name": "Doxycycline", "dose": "100", "unit": "mg", "frequency": "BID",
                 "instruction": "Complete the 5-day course listed on your discharge prescription."},
                {"id": "rx-liquid", "name": "Guaifenesin liquid", "dose": "10", "unit": "mL", "frequency": "TID",
                 "instruction": "Use the supplied measuring cup. Follow the 3-day discharge plan."},
            ],
            [
                {"id": "lab-wbc", "name": "White blood cell count", "value": "11.2", "unit": "10^9/L",
                 "low": "4.0", "high": "11.0", "plain_label": "Infection-fighting blood cells"},
                {"id": "lab-na", "name": "Sodium", "value": "138", "unit": "mmol/L", "low": "135",
                 "high": "145", "plain_label": "Sodium in your blood"},
            ],
            ["Call your primary care office within 48 hours to arrange the follow-up in your discharge plan."],
            ["Get urgent help for trouble breathing, new chest pain, confusion or blue lips.",
             "Contact the discharge team if fever returns or you cannot keep fluids down."],
        ),
        _packet(
            "heart-medicine-review", "A medicine was stopped", "Keep a stop order and flag a potassium review",
            "Alex (synthetic)",
            "You were treated for fluid buildup. The source discharge plan lists your home medicines "
            "and stops a potassium supplement. A pharmacist should review the full list with you.",
            [
                {"id": "rx-furo", "name": "Furosemide", "dose": "20", "unit": "mg", "frequency": "BID",
                 "instruction": "Follow the morning and afternoon schedule in the discharge plan."},
                {"id": "rx-lisin", "name": "Lisinopril", "dose": "5", "unit": "mg", "frequency": "QD",
                 "instruction": "Take as listed by your discharge team."},
                {"id": "rx-potassium", "name": "Potassium chloride", "dose": "10", "unit": "mL",
                 "frequency": "BID", "status": "stopped",
                 "instruction": "This medicine was stopped at discharge. Do not restart without the care team."},
            ],
            [
                {"id": "lab-k", "name": "Potassium", "value": "4.8", "unit": "mmol/L", "low": "3.5",
                 "high": "5.1", "plain_label": "Potassium in your blood"},
                {"id": "lab-creat", "name": "Creatinine", "value": "1.4", "unit": "mg/dL", "low": "0.7",
                 "high": "1.3", "plain_label": "A kidney blood test"},
            ],
            ["The source plan asks for a kidney and potassium blood test in 3 days.",
             "Call the discharge pharmacist if your home bottles differ from this list."],
            ["Get urgent help for severe shortness of breath, fainting or chest pain.",
             "Call the care team about worsening swelling or trouble taking your medicines."],
        ),
        _packet(
            "dosage-mutation-stress", "Catch a tenfold dose change", "Deliberate fault injection, refused then corrected",
            "Sam (synthetic)",
            "Your discharge plan includes a blood thinner and a thyroid medicine. "
            "Keep the source dose and bedtime schedule. Ask your pharmacist to confirm the stopped pain medicine.",
            [
                {"id": "rx-warfarin", "name": "Warfarin", "dose": "2.5", "unit": "mg", "frequency": "QHS",
                 "instruction": "Follow this discharge dose until the anticoagulation clinic reviews your result."},
                {"id": "rx-thyroid", "name": "Levothyroxine", "dose": "50", "unit": "mcg", "frequency": "QD",
                 "instruction": "Follow the discharge plan for when to take this medicine."},
                {"id": "rx-ibu", "name": "Ibuprofen", "dose": "200", "unit": "mg", "frequency": "TID",
                 "status": "stopped",
                 "instruction": "This medicine was stopped at discharge because the team requested a bleeding-risk review."},
            ],
            [
                {"id": "lab-inr", "name": "INR", "value": "2.4", "unit": "ratio", "low": "0.8", "high": "1.2",
                 "plain_label": "A blood-clotting test"},
                {"id": "lab-hgb", "name": "Hemoglobin", "value": "11.0", "unit": "g/dL", "low": "12.0",
                 "high": "16.0", "plain_label": "Oxygen-carrying protein in your blood"},
            ],
            ["The source plan says the anticoagulation clinic will review your INR in 2 days.",
             "The source lab range is not your treatment target. Ask your clinic about your personal target."],
            ["Get urgent help for bleeding that will not stop, black stools, vomiting blood or a severe headache.",
             "Call your clinic if you miss a blood-thinner dose. Do not guess a replacement dose."],
            adversarial=True,
        ),
    ]


def get_packet(packet_id: str) -> ClinicalPacket:
    for packet in get_packets():
        if packet.packet_id == packet_id:
            return packet
    raise KeyError(packet_id)
