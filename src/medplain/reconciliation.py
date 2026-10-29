"""Source-bound clarification plans for complex synthetic discharge dossiers.

This profile deliberately cannot produce a reconciled prescription. Original
instructions, including unknown prose, remain quoted and untranslated. Model
context links are proposals for review, not verified clinical errors. The legacy
clinical profile and its exact gate are not modified by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from importlib.resources import files

from medplain.gate import GENESIS, canonical_hash, verify_receipts
from medplain.clinical_references import reference_material
from medplain.reconciliation_models import Dossier, PROFILE, ReconciliationSelection

MAX_CHARACTERS = 16000
MAX_LINES = 60
MAX_MEDICATION_STATEMENTS = 12
_HASH = re.compile(r"[0-9a-f]{64}")
_NUMBER = r"\d+(?:\.\d+)?"
_FREQUENCY = r"QD|BID|TID|QHS|once daily|twice daily|three times daily|at bedtime"
_DOSE = re.compile(
    rf"(?P<name>[A-Za-z][A-Za-z0-9()/ -]{{0,90}}?)\s+(?P<dose>{_NUMBER})\s+"
    rf"(?P<unit>mg|mcg|mL)\s+(?:by mouth|orally)\s+(?P<frequency>{_FREQUENCY})\b",
    re.IGNORECASE,
)
_PHASE = re.compile(
    rf"(?P<dose>{_NUMBER})\s+(?P<unit>mg|mcg|mL)\s+(?:by mouth|orally)\s+"
    rf"(?P<frequency>{_FREQUENCY})\s+(?:on|for)\s+days?\s+(?P<start>\d+)\s*(?:-|to)\s*(?P<end>\d+)",
    re.IGNORECASE,
)
_FREQ_MAP = {"qd": "QD", "bid": "BID", "tid": "TID", "qhs": "QHS",
             "once daily": "QD", "twice daily": "BID", "three times daily": "TID", "at bedtime": "QHS"}
_TEXT = {
    "en": {
        "notice": "Clarification card, not a new prescription. These source instructions have not been reconciled. Ask the discharge team or pharmacist to confirm them.",
        "compare_instructions": "Which of these source instructions is the intended current plan, and why do they differ?",
        "confirm_intentional_change": "Can the team confirm the documented intentional change and explain it to the caregiver?",
        "confirm_taper": "Can the team confirm every taper phase, its order, its day anchor and what happens after the final phase?",
        "confirm_hold_restart": "Can the team confirm the complete hold condition, how its conditions combine, and who can authorize restarting?",
        "confirm_identity_form": "Do these names, bottles or formulations refer to the same medicine? Ask the pharmacist to confirm identity, strength and release form.",
        "confirm_pending_owner": "Who owns this pending result or follow-up, when is it due, and who will contact the patient?",
        "confirm_interaction_review": "These sources may describe exposure to a pair named in a selected product-label reference. Can the pharmacist confirm current use and review the cited label? This is not a confirmed contraindication or an instruction to stop a medicine.",
        "caregiver_question": "Please discuss the caregiver's concern alongside these exact source passages before confirming the plan.",
        "teachback": "Using the confirmed instructions, can the caregiver explain the schedule and the questions that still need an answer?",
        "proposed": "Agent-proposed context question; not a verified clinical error.",
        "baseline_proposed": "Rule-proposed context question; not a verified clinical error.",
        "unparsed": "Uncertain source wording retained for professional review.",
    },
    "el": {
        "notice": "Κάρτα διευκρινίσεων, όχι νέα συνταγή. Οι οδηγίες των πηγών δεν έχουν συμφιλιωθεί. Ζητήστε επιβεβαίωση από την ομάδα εξόδου ή τον φαρμακοποιό.",
        "compare_instructions": "Ποια από αυτές τις οδηγίες είναι το προβλεπόμενο τρέχον σχέδιο και γιατί διαφέρουν;",
        "confirm_intentional_change": "Μπορεί η ομάδα να επιβεβαιώσει την τεκμηριωμένη σκόπιμη αλλαγή και να την εξηγήσει στον φροντιστή;",
        "confirm_taper": "Μπορεί η ομάδα να επιβεβαιώσει κάθε φάση μείωσης, τη σειρά, την ημέρα έναρξης και τι ακολουθεί την τελευταία φάση;",
        "confirm_hold_restart": "Μπορεί η ομάδα να επιβεβαιώσει ολόκληρη τη συνθήκη προσωρινής διακοπής, τον συνδυασμό των όρων και ποιος εγκρίνει την επανέναρξη;",
        "confirm_identity_form": "Αφορούν αυτά τα ονόματα, φιαλίδια ή σκευάσματα το ίδιο φάρμακο; Ζητήστε από τον φαρμακοποιό επιβεβαίωση ταυτότητας, περιεκτικότητας και μορφής αποδέσμευσης.",
        "confirm_pending_owner": "Ποιος είναι υπεύθυνος για το εκκρεμές αποτέλεσμα ή την παρακολούθηση, πότε αναμένεται και ποιος θα επικοινωνήσει με τον ασθενή;",
        "confirm_interaction_review": "Οι πηγές μπορεί να περιγράφουν έκθεση σε ζεύγος που αναφέρεται σε επιλεγμένη πληροφορία προϊόντος. Μπορεί ο φαρμακοποιός να επιβεβαιώσει την τρέχουσα χρήση και να εξετάσει την πηγή; Δεν πρόκειται για επιβεβαιωμένη αντένδειξη ή οδηγία διακοπής φαρμάκου.",
        "caregiver_question": "Συζητήστε την ανησυχία του φροντιστή μαζί με αυτά τα ακριβή αποσπάσματα πριν επιβεβαιώσετε το σχέδιο.",
        "teachback": "Με τις επιβεβαιωμένες οδηγίες, μπορεί ο φροντιστής να εξηγήσει το πρόγραμμα και τις ερωτήσεις που παραμένουν;",
        "proposed": "Πρόταση ερώτησης από τον πράκτορα, όχι επιβεβαιωμένο κλινικό σφάλμα.",
        "baseline_proposed": "Πρόταση ερώτησης από κανόνες, όχι επιβεβαιωμένο κλινικό σφάλμα.",
        "unparsed": "Αβέβαιη διατύπωση της πηγής διατηρείται για επαγγελματική εξέταση.",
    },
}


def _valid_time(value: str | None) -> None:
    if value is not None:
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError) as exc:
            raise ValueError("Document timestamps must use YYYY-MM-DDTHH:MM:SSZ") from exc


def validate_dossier(raw: dict) -> dict:
    """Validate bounds, not truth or author authority; preserve original text."""
    try:
        dossier = Dossier.model_validate(raw).model_dump(mode="json")
    except (TypeError, ValueError) as exc:
        raise ValueError("Unsupported synthetic reconciliation dossier contract") from exc
    ids = [doc["document_id"] for doc in dossier["documents"]]
    if len(set(ids)) != len(ids) or "caregiver-concerns" in ids:
        raise ValueError("Document IDs must be unique and cannot use the concerns identifier")
    texts = [doc["text"] for doc in dossier["documents"]] + [dossier["caregiver_concerns"]]
    try:
        for text in texts:
            text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Source documents and caregiver concerns must be valid UTF-8 text") from exc
    if sum(len(text) for text in texts) > MAX_CHARACTERS:
        raise ValueError("The dossier exceeds the 16000 character limit")
    if sum(sum(bool(line.strip()) for line in text.splitlines()) for text in texts) > MAX_LINES:
        raise ValueError("The dossier exceeds the 60 source statement limit")
    if any(any(ord(char) < 32 and char not in "\r\n\t" for char in text) for text in texts):
        raise ValueError("The dossier contains unsupported control characters")
    for doc in dossier["documents"]:
        if not doc["text"].strip():
            raise ValueError("Each source document needs nonempty text")
        _valid_time(doc["issued_at"])
        _valid_time(doc["effective_at"])
    # Unknown narrative is retained, not rejected. This bound counts literal
    # medication records, rather than pretending to understand every sentence.
    if sum(len(_DOSE.findall(text)) for text in texts) > MAX_MEDICATION_STATEMENTS:
        raise ValueError("The dossier exceeds the 12 literal medication statement limit")
    return dossier


def examples() -> list[dict]:
    return json.loads(files("medplain").joinpath("data/reconciliation_examples.json").read_text(encoding="utf-8"))


def _name(raw: str) -> str:
    # A bounded lexical parser shared with the complete rule baseline. Original
    # names/forms stay visible; no RxNorm equivalence or first-match identity.
    value = re.split(r"\b(?:taking|take|stop|hold|held|withhold|continue|resume|replaces|home|was|is|use|using|discontinue|discontinued|start|started)\b", raw,
                     flags=re.IGNORECASE)[-1]
    value = re.sub(r"^(?:list|medicine|medication|at discharge|before admission)\s*[:,-]?\s*", "", value,
                   flags=re.IGNORECASE)
    return value.strip(" :,-")


def _clinical(text: str, kind: str) -> dict:
    meds = []
    uncertain_action = False
    for match in _DOSE.finditer(text):
        # Only this matched medicine clause controls its action. Looking back
        # across a semicolon can incorrectly transfer Stop to the next drug.
        prefix = match["name"].lower()
        action = "reported" if kind in {"home_list", "caregiver_note"} else "take"
        unsupported_negation = (re.search(r"\b(?:not|never|no longer)\b", prefix)
                                and not re.search(r"\bdo not take\b", prefix))
        if unsupported_negation:
            action, uncertain_action = "uncertain", True
        elif re.search(r"\b(?:stop|do not take|discontinue)\b", prefix):
            action = "stop"
        elif re.search(r"\b(?:hold|withhold)\b", prefix):
            action = "hold"
        name = _name(match["name"])
        if name:
            meds.append({"name": name, "dose": match["dose"], "unit": match["unit"],
                         "frequency": _FREQ_MAP[match["frequency"].lower()], "route": "oral", "action": action})
    phases = [{"sequence": index, "dose": match["dose"], "unit": match["unit"],
               "frequency": _FREQ_MAP[match["frequency"].lower()], "route": "oral",
               "start_day": match["start"], "end_day": match["end"]}
              for index, match in enumerate(_PHASE.finditer(text), start=1)]
    taper_name = re.search(r"\b(?:taper|reduce)\s+([^:]+):", text, re.I)
    if phases:
        # Phase doses must not masquerade as simultaneous fixed-dose orders.
        meds = []
    hold = re.search(r"\b(?:hold|withhold)\s+(.+?)\s+(?:while|until|if|unless)\s+(.+?)(?:;|$)", text, re.I)
    restart = re.search(r"\brestart\s+(.+)$", text, re.I)
    pending = re.search(r"\b(?:pending test|pending result|result pending)\s*:\s*([^;]+)", text, re.I)
    owner = re.search(r"\b(?:owner|responsible clinician)\s*:\s*([^;]+)", text, re.I)
    follow = re.search(r"\b(?:follow-up|follow up|due)\s*:\s*([^;]+)", text, re.I)
    return {
        "medications": meds, "taper_phases": phases,
        "taper_medicine": taper_name[1] if taper_name and phases else None,
        "terminal_action": "stop" if phases and re.search(r"\bthen stop\.?$", text, re.I) else None,
        "hold_medicine": hold[1] if hold else None,
        "condition_text": hold[2] if hold else None,
        "logic_tokens": re.findall(r"<=|>=|!=|<|>|=|\bAND\b|\bOR\b|\bNOT\b|\bunless\b|\bonly if\b", text),
        "restart_authority": restart[1] if restart else None,
        "pending_test": pending[1] if pending else None,
        "owner": owner[1] if owner else None, "follow_up": follow[1] if follow else None,
        "numeric_literals": re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?", text),
        "uncertain": (not bool(meds or phases or hold or pending or owner or follow) or uncertain_action
                      or bool(meds and not hold and re.search(r"\b(?:except|unless|as needed|PRN|only if|do not stop|do not hold|not approved|not confirmed|unconfirmed)\b", text, re.I))),
    }


def source_statements(dossier: dict) -> list[dict]:
    """Code-bound exact line/span evidence; metadata is supplied, not authenticated."""
    dossier = validate_dossier(dossier)
    docs = list(dossier["documents"])
    if dossier["caregiver_concerns"].strip():
        docs.append({"document_id": "caregiver-concerns", "kind": "caregiver_note", "issued_at": None,
                     "effective_at": None, "text": dossier["caregiver_concerns"]})
    result = []
    for doc in docs:
        digest = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
        offset = 0
        for line, piece in enumerate(doc["text"].splitlines(keepends=True), start=1):
            quote = piece.rstrip("\r\n")
            size = len(piece.encode("utf-8"))
            if quote.strip():
                result.append({
                    "statement_id": f"{doc['document_id']}:L{line}", "document_kind": doc["kind"],
                    "issued_at": doc["issued_at"], "effective_at": doc["effective_at"],
                    "citation": {"document_id": doc["document_id"], "line": line,
                                 "document_sha256": digest, "byte_start": offset,
                                 "byte_end": offset + len(quote.encode("utf-8")), "quote": quote},
                    "clinical": _clinical(quote, doc["kind"]),
                })
            offset += size
    return result


def _issue(code: str, ids: list[str], question: str, required: bool = True) -> dict:
    return {"issue_id": canonical_hash({"code": code, "statement_ids": ids})[:20], "code": code,
            "statement_ids": ids, "question_code": question, "requires_clarification": required,
            "classification": "structural_check"}


def structural_issues(statements: list[dict]) -> list[dict]:
    """Conservative structural floor, without inferring clinical intent/authority."""
    result = []
    by_name: dict[str, list[tuple[dict, dict]]] = {}
    for statement in statements:
        clinical, sid = statement["clinical"], statement["statement_id"]
        for med in clinical["medications"]:
            by_name.setdefault(med["name"].casefold(), []).append((statement, med))
        if clinical["taper_phases"]:
            result.append(_issue("taper_sequence", [sid], "confirm_taper", False))
        if clinical["condition_text"] is not None:
            result.append(_issue("conditional_hold", [sid], "confirm_hold_restart"))
        if clinical["pending_test"] is not None:
            owner, follow = (clinical["owner"] or "").lower(), (clinical["follow_up"] or "").lower()
            missing = (not owner or not follow or any(word in owner + " " + follow for word in
                       ["not assigned", "unknown", "unclear", "not named", "not scheduled", "not specified"]))
            result.append(_issue("pending_follow_up_gap" if missing else "pending_result_follow_up",
                                 [sid], "confirm_pending_owner", missing))
        # Unknown clinical prose remains visible and reviewable. Context lines
        # never become an invented medication instruction or a rejected input.
        if clinical["uncertain"] and statement["document_kind"] not in {"caregiver_note"}:
            result.append(_issue("uncertain_source_wording", [sid], "caregiver_question"))
    for name, entries in by_name.items():
        if len(entries) < 2:
            continue
        signatures = {(med["dose"], med["unit"], med["frequency"], "take" if med["action"] == "reported" else med["action"])
                      for _, med in entries}
        if len(signatures) <= 1:
            continue
        ids = list(dict.fromkeys(statement["statement_id"] for statement, _ in entries))
        changes = [statement for statement in statements
                   if statement["citation"]["quote"].startswith("Intentional change:")
                   and not statement["clinical"]["uncertain"]
                   and len([med for med in statement["clinical"]["medications"] if med["name"].casefold() == name]) >= 2]
        covered = any(signatures <= {(med["dose"], med["unit"], med["frequency"], "take" if med["action"] == "reported" else med["action"])
                                    for med in statement["clinical"]["medications"] if med["name"].casefold() == name}
                      for statement in changes)
        if covered:
            ids += [statement["statement_id"] for statement in changes if statement["statement_id"] not in ids]
            result.append(_issue("documented_intentional_change", ids, "confirm_intentional_change", False))
        else:
            code = ("stop_status_conflict" if any(med["action"] in {"stop", "hold", "uncertain"} for _, med in entries)
                    else "dose_schedule_conflict")
            result.append(_issue(code, ids, "compare_instructions"))
    # Finite label-backed review universe. Exact literal names only; no
    # ingredient substitution or comprehensive drug-interaction claim.
    for card in reference_material()["references"]["cards"]:
        for pair in card.get("selected_pairs", []):
            left, right = [by_name.get(name.casefold(), []) for name in pair]
            if not left or not right or all(med["action"] == "stop" for _, med in left + right):
                continue
            ids = list(dict.fromkeys(statement["statement_id"] for statement, _ in left + right))
            issue = _issue("selected_label_interaction_review", ids, "confirm_interaction_review")
            result.append({**issue, "classification": "selected_label_reference", "reference_ids": [card["id"]],
                           "selected_pair": pair,
                           "scope": "Possible exposure across sources; current concurrent use is not established. Pharmacist review only, not a contraindication or stop instruction."})
    return result


def baseline_selection(dossier: dict, statements: list[dict] | None = None) -> dict:
    """Complete deterministic comparator: same sources, facts and question set.

    Rules use lexical/context links and conservative clarification, not an
    unsupported-input handicap. Unknown prose remains available to both arms.
    """
    dossier = validate_dossier(dossier)
    statements = statements if statements is not None else source_statements(dossier)
    questions = []
    def add(code, ids):
        # One targeted focus can cite several caregiver observations. Do not
        # pad the comparator with duplicate questions or all-pairs noise.
        clinical_ids = {sid for sid in ids if by_id[sid]["document_kind"] != "caregiver_note"}
        for question in questions:
            prior = {sid for sid in question["statement_ids"] if by_id[sid]["document_kind"] != "caregiver_note"}
            if question["code"] == code and (prior & clinical_ids or question["statement_ids"] == ids):
                question["statement_ids"] = list(dict.fromkeys(question["statement_ids"] + ids))[:8]
                return
        if len(questions) < 12:
            questions.append({"code": code, "statement_ids": ids[:8]})
    by_id = {row["statement_id"]: row for row in statements}
    caregivers = [row for row in statements if row["document_kind"] == "caregiver_note"]
    clinical = [row for row in statements if row["clinical"]["medications"] or row["clinical"]["taper_phases"]
                or row["clinical"]["condition_text"] or row["clinical"]["pending_test"]]
    for row in caregivers:
        words = set(re.findall(r"[a-z]{4,}", row["citation"]["quote"].lower()))
        words -= {"discharge", "before", "admission", "taking", "take", "mouth", "daily", "medicine", "medicines",
                  "medication", "medications", "list", "lists", "team", "confirm", "caregiver", "home", "patient"}
        relevant = [candidate for candidate in clinical if
                    words & set(re.findall(r"[a-z]{4,}", candidate["citation"]["quote"].lower()))]
        text = row["citation"]["quote"].lower()
        focused_issues = [issue for issue in structural_issues(statements)
                          if set(issue["statement_ids"]) & {candidate["statement_id"] for candidate in relevant}]
        code = ("confirm_interaction_review" if any(issue["code"] == "selected_label_interaction_review" for issue in focused_issues)
                else "confirm_hold_restart" if re.search(r"\b(?:hold|restart|pressure|dizzy|dizziness)\b", text)
                else "confirm_taper" if re.search(r"\b(?:taper|phase|days|calendar)\b", text)
                else "confirm_pending_owner" if re.search(r"\b(?:pending|result|call|follow-up|owner)\b", text)
                else "confirm_intentional_change" if any(issue["code"] == "documented_intentional_change" for issue in focused_issues)
                else "confirm_identity_form" if re.search(r"\b(?:brand|generic|form|formulation|release|tablet|capsule|xl|long-acting)\b", text)
                else "compare_instructions" if any(issue["code"] in {"dose_schedule_conflict", "stop_status_conflict"} for issue in focused_issues)
                else "confirm_identity_form" if re.search(r"\b(?:bottle|bottles|box|boxes|label|labels)\b", text)
                else "caregiver_question")
        # Relevant quoted sources are the same material the model receives.
        # No generic all-pairs expansion or invented equivalence is performed.
        ids = [row["statement_id"]] + [candidate["statement_id"] for candidate in relevant[:3]]
        # Where lexical medicine names differ, use the complete pair of
        # identity/form source lists. This proposes review, not equivalence.
        if code == "confirm_identity_form":
            if not relevant:
                ids += [candidate["statement_id"] for candidate in clinical
                        if candidate["clinical"]["medications"] and candidate["statement_id"] not in ids][:4]
        elif code in {"compare_instructions", "confirm_intentional_change", "confirm_interaction_review"}:
            ids += [sid for issue in focused_issues for sid in issue["statement_ids"] if sid not in ids][:4]
        elif code == "confirm_hold_restart":
            ids += [candidate["statement_id"] for candidate in clinical
                    if candidate["clinical"]["condition_text"] and candidate["statement_id"] not in ids][:3]
        elif code == "confirm_pending_owner":
            ids += [candidate["statement_id"] for candidate in clinical
                    if candidate["clinical"]["pending_test"] and candidate["statement_id"] not in ids][:3]
        elif code == "confirm_taper":
            ids += [candidate["statement_id"] for candidate in clinical
                    if candidate["clinical"]["taper_phases"] and candidate["statement_id"] not in ids][:3]
        if len(ids) == 1 and clinical:
            ids.append(clinical[0]["statement_id"])
        ids = list(dict.fromkeys(ids))[:8]
        if not question_errors({"code": code, "statement_ids": ids}, statements, teachback=False):
            add(code, ids)
        else:
            add("caregiver_question", ids)
    anchor = [row["statement_id"] for row in clinical[:3]] or [statements[0]["statement_id"]]
    return {"source_hash": canonical_hash(dossier), "questions": questions,
            "teachback": [{"code": "teachback", "statement_ids": anchor}]}


def question_errors(question: dict, statements: list[dict], *, teachback: bool) -> list[str]:
    by_id = {row["statement_id"]: row for row in statements}
    ids = question["statement_ids"]
    if len(set(ids)) != len(ids) or any(sid not in by_id for sid in ids):
        return ["Question citations are missing, duplicated or outside this source"]
    rows = [by_id[sid] for sid in ids]
    code = question["code"]
    if teachback:
        return [] if code == "teachback" else ["Only teachback questions belong in the teachback section"]
    if code == "teachback":
        return ["Teachback questions belong in the separate teachback section"]
    if code == "compare_instructions" and len(rows) < 2:
        return ["Comparison requires at least two distinct cited source statements"]
    if code == "confirm_intentional_change" and not any(row["citation"]["quote"].startswith("Intentional change:") for row in rows):
        return ["An intentional-change question requires the explicitly documented change"]
    if code == "confirm_taper" and not any(row["clinical"]["taper_phases"] for row in rows):
        return ["A taper question requires cited taper phases"]
    if code == "confirm_hold_restart" and not any(row["clinical"]["condition_text"] for row in rows):
        return ["A hold question requires a cited hold condition"]
    if code == "confirm_pending_owner" and not any(row["clinical"]["pending_test"] for row in rows):
        return ["A pending-result question requires a cited pending result"]
    if code == "confirm_identity_form" and not any(row["document_kind"] == "caregiver_note" for row in rows):
        return ["An identity question requires cited caregiver context and does not assert equivalence"]
    if code == "confirm_interaction_review" and not any(
            issue["code"] == "selected_label_interaction_review" and set(issue["statement_ids"]) <= set(ids)
            for issue in structural_issues(statements)):
        return ["An interaction-review question requires both sources of a selected label-backed pair"]
    return []


def validate_selection(dossier: dict, selection: dict) -> dict:
    selection = ReconciliationSelection.model_validate(selection).model_dump(mode="json")
    if selection["source_hash"] != canonical_hash(dossier):
        raise ValueError("Plan source_hash differs from this dossier")
    statements = source_statements(dossier)
    errors, seen = [], set()
    for section in ["questions", "teachback"]:
        for index, question in enumerate(selection[section]):
            errors.extend(f"{section}[{index}]: {error}" for error in question_errors(question, statements, teachback=section == "teachback"))
            key = question["code"], tuple(question["statement_ids"])
            if key in seen:
                errors.append(f"{section}[{index}]: Duplicate question")
            seen.add(key)
    if errors:
        raise ValueError("; ".join(errors[:8]))
    return selection


def _render_question(question: dict, language: str, *, baseline: bool = False) -> dict:
    return {**question, "text": _TEXT[language][question["code"]],
            "classification": "context_proposal", "label": _TEXT[language]["baseline_proposed" if baseline else "proposed"]}


def build_artifact(dossier: dict, selection: dict, *, baseline: bool = False) -> dict:
    dossier = validate_dossier(dossier)
    selection = validate_selection(dossier, selection)
    statements = source_statements(dossier)
    material = reference_material()
    return {
        "profile": PROFILE, "source_hash": canonical_hash(dossier), "language": dossier["language"],
        "kind": "clarification_plan", "patient_regimen": None, "notice": _TEXT[dossier["language"]]["notice"],
        "source_statements": statements,
        "structural_issues": [{**issue, "question_text": _TEXT[dossier["language"]][issue["question_code"]]}
                              for issue in structural_issues(statements)],
        "proposed_questions": [_render_question(question, dossier["language"], baseline=baseline) for question in selection["questions"]],
        "teachback": [_render_question(question, dossier["language"], baseline=baseline) for question in selection["teachback"]],
        "references": {"snapshot_sha256": material["snapshot_sha256"], "scope": material["scope"],
                       "reference_ids": [row["id"] for row in material["references"]["cards"]],
                       "interaction_service": material["interaction_service"],
                       "identity_snapshot_status": material["identity_snapshot"]["status"]},
    }


def render_artifact(artifact: dict) -> str:
    parts = [artifact["notice"]]
    for row in artifact["source_statements"]:
        parts.append(f"[{row['statement_id']}] {row['citation']['quote']}")
    for issue in artifact["structural_issues"]:
        parts.append(f"[{issue['code']}] " + ", ".join(issue["statement_ids"]))
    for question in artifact["proposed_questions"] + artifact["teachback"]:
        parts.append(question["text"] + " [" + ", ".join(question["statement_ids"]) + "]")
    return "\n\n".join(parts)


def artifact_digest(artifact: dict) -> str:
    return canonical_hash({"structured": artifact, "text": render_artifact(artifact)})


def gate_errors(dossier: dict, artifact: dict, selection: dict, *, baseline: bool = False) -> list[str]:
    """Exact source, clinical atoms, conflict floor, clauses and plan binding.

    This verifies integrity and bounded question preconditions. It does not
    prove semantic relevance of agent-proposed context links or clinical truth.
    """
    try:
        expected = build_artifact(dossier, selection, baseline=baseline)
        if artifact != expected:
            return ["Clarification artifact differs from source citations, clinical atoms, conflict floor or allowed question clauses"]
    except (KeyError, TypeError, ValueError):
        return ["The clarification selection or artifact contract is invalid"]
    return []


def append_receipt(receipts: list[dict], dossier: dict, session_id: str, artifact: dict, errors: list[str]) -> dict:
    body = {"sequence": len(receipts) + 1, "session_id": session_id, "source_hash": canonical_hash(dossier),
            "artifact_hash": artifact_digest(artifact), "previous_hash": receipts[-1]["hash"] if receipts else GENESIS,
            "verdict": "refused" if errors else "verified", "discrepancies": list(errors),
            "timestamp": datetime.now(timezone.utc).isoformat()}
    receipt = {**body, "hash": canonical_hash(body)}
    receipts.append(receipt)
    return receipt


def _commit() -> str:
    return os.getenv("MEDPLAIN_COMMIT", "development")


def deterministic_baseline(dossier: dict) -> dict:
    dossier = validate_dossier(dossier)
    selection = baseline_selection(dossier)
    artifact = build_artifact(dossier, selection, baseline=True)
    errors = gate_errors(dossier, artifact, selection, baseline=True)
    receipts = []
    append_receipt(receipts, dossier, "deterministic-baseline", artifact, errors)
    return {"status": "refused" if errors else "verified", "source_profile": PROFILE,
            "source_hash": canonical_hash(dossier), "session_id": "deterministic-baseline", "model_mode": "deterministic",
            "commit": _commit(), "error": None, "artifact_kind": "clarification_plan",
            "artifact": None if errors else artifact, "artifact_hash": None if errors else artifact_digest(artifact),
            "explanation_text": None if errors else render_artifact(artifact), "patient_regimen": None,
            "trace": [], "runtime": {"orchestrator": "deterministic-rules", "selection": selection,
                                      "baseline": True, "nodes_executed": [], "tools_executed": []},
            "receipts": receipts, "correction_count": 0}


def run_reconciliation(dossier: dict, session_id: str, mode: str | None = None, stress: bool = False) -> dict:
    from medplain.reconciliation_engine import run
    return run(validate_dossier(dossier), session_id, mode=mode, stress=stress)


def validate_reconciliation_result(dossier: dict, result: dict, execution_id: str | None = None) -> None:
    dossier = validate_dossier(dossier)
    required = {"status", "source_profile", "source_hash", "session_id", "model_mode", "commit", "error",
                "artifact_kind", "artifact", "artifact_hash", "explanation_text", "patient_regimen",
                "trace", "runtime", "receipts", "correction_count"}
    if (not isinstance(result, dict) or not required <= set(result) or result.get("status") not in {"verified", "refused"}
            or result.get("source_profile") != PROFILE or result.get("source_hash") != canonical_hash(dossier)
            or result.get("artifact_kind") != "clarification_plan" or result.get("patient_regimen") is not None
            or not isinstance(result.get("session_id"), str) or not result["session_id"]
            or (execution_id is not None and result["session_id"] != execution_id)
            or result.get("model_mode") not in {"fixture", "bedrock", "deterministic"}
            or not isinstance(result.get("runtime"), dict) or not isinstance(result.get("trace"), list)
            or (result.get("error") is not None and not isinstance(result["error"], str))):
        raise ValueError("Clarification evidence is not bound to this source and execution")
    receipts = result.get("receipts")
    if (not isinstance(receipts, list) or len(receipts) > 2 or any(not isinstance(row, dict) for row in receipts)
            or (receipts and not verify_receipts(receipts))):
        raise ValueError("Clarification receipt chain is invalid")
    receipt_fields = {"sequence", "session_id", "source_hash", "artifact_hash", "previous_hash", "verdict", "discrepancies", "timestamp", "hash"}
    for receipt in receipts:
        if (set(receipt) != receipt_fields or type(receipt["sequence"]) is not int
                or receipt["source_hash"] != canonical_hash(dossier)
                or receipt["session_id"] != result["session_id"]
                or not isinstance(receipt["timestamp"], str) or not receipt["timestamp"]
                or any(not isinstance(receipt.get(key), str) or not _HASH.fullmatch(receipt[key])
                       for key in ["artifact_hash", "source_hash", "previous_hash", "hash"])
                or not isinstance(receipt["discrepancies"], list)
                or any(not isinstance(item, str) for item in receipt["discrepancies"])):
            raise ValueError("Clarification receipts differ from the saved source or execution")
    if result["status"] == "refused":
        if (any(result.get(key) is not None for key in ["artifact", "artifact_hash", "explanation_text"])
                or result.get("explanation") is not None
                or type(result.get("correction_count")) is not int
                or result["correction_count"] != max(0, len(receipts) - 1)
                or (receipts and receipts[-1]["verdict"] != "refused")):
            raise ValueError("A refused workflow attempted to release a clarification artifact")
        return
    runtime = result.get("runtime", {})
    selection = runtime.get("selection")
    baseline = result["model_mode"] == "deterministic"
    if not baseline:
        # Persisted SDK metadata is evidence of observed execution, not an
        # authenticity signature. Still reject a rehashed rule-only result
        # that lacks the actual fresh-per-invocation trajectory contract.
        required_tools = {"DischargeEvidenceIndexerAgent": {"read_reconciliation_sources"},
                          "CrossDocumentClarificationPlannerAgent": {"check_reconciliation_constraints", "lookup_reconciliation_references"},
                          "ClarificationCardPublisherAgent": {"build_clarification_card"}}
        nodes, tools = runtime.get("nodes_executed"), runtime.get("tools_executed")
        outcomes = runtime.get("invocation_outcomes")
        if (runtime.get("orchestrator") != "strands.multiagent.Graph" or runtime.get("graph_status") != "completed"
                or runtime.get("source_index_verified") is not True or not isinstance(nodes, list)
                or not 3 <= len(nodes) <= 10 or nodes[0] != "DischargeEvidenceIndexerAgent"
                or nodes[-1] != "ClarificationCardPublisherAgent" or any(node not in required_tools for node in nodes)
                or not isinstance(tools, list) or len(tools) > 30
                or not isinstance(outcomes, list) or len(outcomes) != len(nodes)):
            raise ValueError("The saved clarification result lacks a completed Strands trajectory")
        for record in tools:
            if (not isinstance(record, dict) or type(record.get("invocation_index")) is not int
                    or not 1 <= record["invocation_index"] <= len(nodes)
                    or record.get("agent") != nodes[record["invocation_index"] - 1]
                    or record.get("tool") not in required_tools[record["agent"]]
                    or type(record.get("success")) is not bool):
                raise ValueError("A tool record differs from its observed invocation")
        for index, role in enumerate(nodes, start=1):
            if outcomes[index - 1] == "repair":
                if index >= len(nodes) or nodes[index] != role or outcomes[index] != "completed":
                    raise ValueError("A repair invocation lacks its bounded completed replacement")
                continue
            if outcomes[index - 1] != "completed":
                raise ValueError("An unfinished invocation cannot publish a clarification card")
            observed = {row["tool"] for row in tools if row["invocation_index"] == index and row["success"]}
            if observed != required_tools[role]:
                raise ValueError("A completed invocation has no fresh required tool evidence")
    artifact = result.get("artifact")
    if (not isinstance(artifact, dict) or result.get("error") is not None
            or not receipts or receipts[-1]["verdict"] != "verified"
            or any(row["verdict"] != "refused" for row in receipts[:-1])
            or type(result.get("correction_count")) is not int or result["correction_count"] != len(receipts) - 1
            or gate_errors(dossier, artifact, selection, baseline=baseline)
            or result.get("artifact_hash") != artifact_digest(artifact)
            or receipts[-1]["artifact_hash"] != result["artifact_hash"]
            or result.get("explanation_text") != render_artifact(artifact)):
        raise ValueError("The saved clarification artifact failed independent publication verification")
