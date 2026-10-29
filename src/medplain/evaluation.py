"""Paired synthetic evaluation with the official optional Strands Evals SDK.

The gold clinical facts below are literal annotations of held-out notes, not
outputs from the normalizer or renderer being measured. This is engineering
evidence for a bounded grammar, not clinical or comprehension validation.
"""

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from medplain.clinical import build_explanation, render_explanation
from medplain.gate import ReceiptChain, SafetyGate, canonical_hash, verify_receipts
from medplain.schemas import PatientExplanation

CANONICAL_URL = "https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws"
EVALS_VERSION = "1.7.0"
CORPUS_VERSION = "raw-synthetic-heldout-v1"
RULER_VERSION = "paired-critical-facts-v1"
PARSER = "LabAndDischargeParserAgent"
VERIFIER = "DrugAndContraindicationVerifierAgent"
EXPLAINER = "PatientAdvocateExplainerAgent"
REQUIRED_TOOLS = {
    PARSER: ["lab_reference_range_evaluator"],
    VERIFIER: ["medication_dosage_frequency_parity_checker", "drug_interaction_checker"],
    EXPLAINER: ["grounded_explanation_builder"],
}


def _gold(alias, medications, labs, follow_up, red_flags):
    """Literal annotation shorthand; never calls the implementation under evaluation."""
    return {"patient_alias": alias,
            "medications": [dict(zip(("source_line", "name", "dose", "unit", "frequency", "status"), row))
                            for row in medications],
            "labs": [dict(zip(("source_line", "name", "value", "unit", "low", "high"), row)) for row in labs],
            "follow_up": [{"source_line": line, "text": text} for line, text in follow_up],
            "red_flags": [{"source_line": line, "text": text} for line, text in red_flags]}


def corpus() -> list[dict]:
    """Six valid notes and four invalid notes, independent of shipped demo packets."""
    cases = [
        {"id": "decimal-bid-stop", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Elm
Take Metoprolol 12.5 mg by mouth twice daily.
Take Levothyroxine 75 mcg by mouth once daily.
Stop Ibuprofen 200 mg by mouth three times daily.
Lab Potassium: 3.4 mmol/L (reference 3.5 to 5.1).
Follow-up: Ask your pharmacist to review the discharge list.
Get help: Contact your care team if symptoms worsen.""",
         "gold": _gold("Demo-Elm", [(3, "Metoprolol", "12.5", "mg", "BID", "active"),
                                   (4, "Levothyroxine", "75", "mcg", "QD", "active"),
                                   (5, "Ibuprofen", "200", "mg", "TID", "stopped")],
                       [(6, "Potassium", "3.4", "mmol/L", "3.5", "5.1")],
                       [(7, "Ask your pharmacist to review the discharge list.")],
                       [(8, "Contact your care team if symptoms worsen.")])},
        {"id": "liquid-qhs-negation", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Cedar
Take Lactulose 7.5 mL by mouth at bedtime.
Take Amlodipine 2.5 mg by mouth once daily.
Do not take Naproxen 250 mg by mouth twice daily.
Lab Creatinine: 1.3 mg/dL (reference 0.7 to 1.2).
Follow-up: Arrange a follow-up appointment with your care team.
Get help: Seek urgent care for chest pain or fainting.""",
         "gold": _gold("Demo-Cedar", [(3, "Lactulose", "7.5", "mL", "QHS", "active"),
                                     (4, "Amlodipine", "2.5", "mg", "QD", "active"),
                                     (5, "Naproxen", "250", "mg", "BID", "stopped")],
                       [(6, "Creatinine", "1.3", "mg/dL", "0.7", "1.2")],
                       [(7, "Arrange a follow-up appointment with your care team.")],
                       [(8, "Seek urgent care for chest pain or fainting.")])},
        {"id": "mcg-tid-followup", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Birch
Take Folic acid 400 mcg by mouth once daily.
Take Dexamethasone 0.5 mg by mouth three times daily.
Lab Hemoglobin: 11.8 g/dL (reference 12 to 16).
Follow-up: Contact the clinic to review your medicines.
Follow-up: Ask your pharmacist to review the discharge list.
Get help: Seek urgent care for severe trouble breathing.""",
         "gold": _gold("Demo-Birch", [(3, "Folic acid", "400", "mcg", "QD", "active"),
                                     (4, "Dexamethasone", "0.5", "mg", "TID", "active")],
                       [(5, "Hemoglobin", "11.8", "g/dL", "12", "16")],
                       [(6, "Contact the clinic to review your medicines."),
                        (7, "Ask your pharmacist to review the discharge list.")],
                       [(8, "Seek urgent care for severe trouble breathing.")])},
        {"id": "frequency-codes", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Maple
Take Potassium chloride 5 mL by mouth TID.
Take Carvedilol 6.25 mg by mouth BID.
Do not take Diphenhydramine 25 mg by mouth QHS.
Lab White cell count: 10.5 10^9/L (reference 4 to 11).
Follow-up: Contact the clinic to review your medicines.
Get help: Seek urgent care for chest pain or fainting.""",
         "gold": _gold("Demo-Maple", [(3, "Potassium chloride", "5", "mL", "TID", "active"),
                                     (4, "Carvedilol", "6.25", "mg", "BID", "active"),
                                     (5, "Diphenhydramine", "25", "mg", "QHS", "stopped")],
                       [(6, "White cell count", "10.5", "10^9/L", "4", "11")],
                       [(7, "Contact the clinic to review your medicines.")],
                       [(8, "Seek urgent care for chest pain or fainting.")])},
        {"id": "small-decimal-stopped", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Willow
Take Digoxin 0.125 mg by mouth at bedtime.
Take Vitamin D 0.5 mL by mouth once daily.
Stop Levothyroxine 25 mcg by mouth QD.
Lab Estimated GFR: 45 mL/min (reference 60 to 120).
Follow-up: Arrange a follow-up appointment with your care team.
Get help: Contact your care team if symptoms worsen.""",
         "gold": _gold("Demo-Willow", [(3, "Digoxin", "0.125", "mg", "QHS", "active"),
                                      (4, "Vitamin D", "0.5", "mL", "QD", "active"),
                                      (5, "Levothyroxine", "25", "mcg", "QD", "stopped")],
                       [(6, "Estimated GFR", "45", "mL/min", "60", "120")],
                       [(7, "Arrange a follow-up appointment with your care team.")],
                       [(8, "Contact your care team if symptoms worsen.")])},
        {"id": "multiple-labs-precautions", "source_text": """MEDPLAIN SYNTHETIC DISCHARGE V1
Synthetic alias: Demo-Aspen
Take Amoxicillin 250 mg by mouth three times daily.
Lab Sodium: 140 mmol/L (reference 135 to 145).
Lab INR: 1.1 ratio (reference 0.8 to 1.2).
Follow-up: Ask your pharmacist to review the discharge list.
Get help: Seek urgent care for severe trouble breathing.
Get help: Contact your care team if symptoms worsen.""",
         "gold": _gold("Demo-Aspen", [(3, "Amoxicillin", "250", "mg", "TID", "active")],
                       [(4, "Sodium", "140", "mmol/L", "135", "145"),
                        (5, "INR", "1.1", "ratio", "0.8", "1.2")],
                       [(6, "Ask your pharmacist to review the discharge list.")],
                       [(7, "Seek urgent care for severe trouble breathing."),
                        (8, "Contact your care team if symptoms worsen.")])},
    ]
    # Negative notes deliberately differ from the valid annotation; they are
    # input refusals, not fabricated model failures or omitted samples.
    invalid = [
        ("ambiguous-frequency", cases[0]["source_text"].replace("twice daily", "morning or evening")),
        ("unknown-unit", cases[0]["source_text"].replace("12.5 mg", "12.5 g")),
        ("unknown-instruction", cases[0]["source_text"] + "\nIncrease the dose whenever you feel tired."),
        ("conflicting-duplicate", cases[0]["source_text"] + "\nTake Metoprolol 25 mg by mouth once daily."),
    ]
    for case in cases:
        case["expected_disposition"] = "verified"
    cases.extend({"id": name, "source_text": text, "gold": None, "expected_disposition": "refused"}
                 for name, text in invalid)
    return cases


def expected_extraction(case: dict) -> dict | None:
    if case["gold"] is None:
        return None
    gold = copy.deepcopy(case["gold"])
    lines = case["source_text"].splitlines()
    for key in ("medications", "labs", "follow_up", "red_flags"):
        for item in gold[key]:
            item["source_quote"] = lines[item["source_line"] - 1]
    return {"source_sha256": hashlib.sha256(case["source_text"].encode()).hexdigest(), **gold}


def critical_facts(explanation: dict | None) -> dict | None:
    if not explanation:
        return None
    try:
        return {"medications": [{key: item[key] for key in ("name", "dose", "unit", "frequency", "status")}
                                 for item in explanation["medications"]],
                "labs": [{key: item[key] for key in ("name", "value", "unit", "low", "high")}
                         for item in explanation["labs"]],
                "follow_up": explanation["follow_up"], "red_flags": explanation["red_flags"]}
    except (KeyError, TypeError):
        return None


def expected_facts(case: dict) -> dict | None:
    if case["gold"] is None:
        return None
    gold = case["gold"]
    return {"medications": [{k: v for k, v in row.items() if k != "source_line"} for row in gold["medications"]],
            "labs": [{k: v for k, v in row.items() if k != "source_line"} for row in gold["labs"]],
            "follow_up": [row["text"] for row in gold["follow_up"]],
            "red_flags": [row["text"] for row in gold["red_flags"]]}


MUTATIONS = [
    ("decimal-dose", "medications", 0, "dose", "125"),
    ("mg-to-mcg", "medications", 0, "unit", "mcg"),
    ("mcg-to-mg", "medications", 1, "unit", "mg"),
    ("bid-to-tid", "medications", 0, "frequency", "TID"),
    ("tid-to-bid", "medications", 2, "frequency", "BID"),
    ("stop-negation", "medications", 2, "status", "active"),
    ("lab-value", "labs", 0, "value", "4.3"),
    ("lab-unit", "labs", 0, "unit", "mg/dL"),
    ("lab-range", "labs", 0, "low", "2.5"),
]


def gate_challenges(packet, case: dict) -> list[dict]:
    changes = MUTATIONS if case["id"] == "decimal-bid-stop" else (
        [("ml-to-mg", "medications", 0, "unit", "mg"),
         ("qhs-to-qd", "medications", 0, "frequency", "QD")]
        if case["id"] == "liquid-qhs-negation" else [])
    results = []
    gate = SafetyGate(packet)
    for name, group, index, field, value in changes:
        altered = packet.model_copy(deep=True)
        before = getattr(altered, group)[index].model_copy(deep=True)
        setattr(getattr(altered, group)[index], field, value)
        if group == "medications":
            med = altered.medications[index]
            med.instruction = med.instruction.replace(f"{before.dose} {before.unit}", f"{med.dose} {med.unit}")
            phrases = {"QD": "once daily", "BID": "twice daily", "TID": "three times daily", "QHS": "at bedtime"}
            if field == "frequency":
                med.instruction = med.instruction.replace(phrases[before.frequency], phrases[med.frequency])
                med.instruction = med.instruction.replace(before.frequency, med.frequency)
            if field == "status":
                med.instruction = "Follow the dose and timing written in the source."
        # Render from the altered structured values, so text and fields agree.
        # The original discharge/FHIR source remains the gate's authority.
        candidate = build_explanation(altered)
        verdict = gate.validate(candidate)
        results.append({"id": name, "field": f"{group}[{index}].{field}", "mutated_value": value,
                        "false_accept": verdict.passed, "discrepancies": verdict.discrepancies,
                        "coherent_artifact_hash": canonical_hash({"structured": candidate.model_dump(),
                                                                   "text": render_explanation(candidate)})})
    return results


def trajectory_valid(outcome: dict, expected_refusal=False) -> bool:
    if expected_refusal:
        return outcome["kind"] == "input_refusal" and not outcome["nodes"] and not outcome["tools"]
    if outcome["status"] != "verified":
        return False
    nodes = outcome["nodes"]
    if not all(isinstance(outcome[key], list) for key in ("nodes", "tools", "receipts")):
        return False
    collapsed = [n for i, n in enumerate(nodes) if i == 0 or nodes[i - 1] != n]
    expected = [PARSER, VERIFIER, EXPLAINER] + [VERIFIER, EXPLAINER] * outcome["correction_count"]
    if collapsed != expected:
        return False
    for role, tools in REQUIRED_TOOLS.items():
        for tool in tools:
            if sum(t.get("agent") == role and t.get("tool") == tool and t.get("success") is True
                   for t in outcome["tools"]) < nodes.count(role):
                return False
    verdicts = [r["verdict"] for r in outcome["receipts"]]
    return verdicts == ["refused"] * outcome["correction_count"] + ["verified"]


def score_outcome(case: dict, outcome: dict, arm: str) -> dict:
    valid = (outcome["status"] == "verified" and outcome["independent_publication_check"] if case["gold"] is not None
             else outcome["kind"] == "input_refusal")
    extraction = case["gold"] is not None and outcome["extraction"] == expected_extraction(case)
    fidelity = case["gold"] is not None and critical_facts(outcome["explanation"]) == expected_facts(case)
    return {"validity": valid, "extraction_exact": bool(extraction), "field_fidelity": bool(fidelity),
            "trajectory": trajectory_valid(outcome, case["gold"] is None) if arm == "strands" else None,
            "gate_mutations": len(outcome["gate_challenges"]),
            "gate_false_accepts": sum(row["false_accept"] for row in outcome["gate_challenges"])}


def _blank(mode: str) -> dict:
    return {"status": "error", "kind": "runtime_error", "model_mode": mode, "error": None,
            "extraction": None, "explanation": None, "explanation_text": None, "artifact_hash": None,
            "nodes": [], "tools": [], "trace": [], "receipts": [], "correction_count": 0,
            "gate_challenges": [], "client_latency_ms": None, "worker_duration_ms": None,
            "inference_usage": None, "independent_publication_check": False, "execution_id": None}


def _from_result(result: dict, outcome: dict) -> dict:
    runtime = result.get("runtime", {})
    outcome.update(status=result["status"], kind="verified" if result["status"] == "verified" else "model_refusal",
                   error=result.get("error"), extraction=result.get("intake_validation", {}).get("extraction"),
                   explanation=result.get("explanation"), explanation_text=result.get("explanation_text"),
                   nodes=runtime.get("nodes_executed", []), tools=runtime.get("tools_executed", []),
                   trace=result.get("trace", []), receipts=result.get("receipts", []),
                   correction_count=result.get("correction_count", 0))
    outcome["execution_id"] = result.get("session_id")
    if outcome["explanation"]:
        outcome["artifact_hash"] = canonical_hash({"structured": outcome["explanation"],
                                                   "text": outcome["explanation_text"]})
    return outcome


def publication_valid(packet, outcome: dict) -> bool:
    if outcome["status"] != "verified":
        return False
    try:
        candidate = PatientExplanation.model_validate(outcome["explanation"])
        receipts = outcome["receipts"]
        return (SafetyGate(packet).validate(candidate).passed
                and outcome["explanation_text"] == render_explanation(candidate)
                and verify_receipts(receipts) and receipts[-1]["verdict"] == "verified"
                and receipts[-1]["artifact_hash"] == outcome["artifact_hash"]
                and all(r["source_hash"] == canonical_hash(packet.model_dump())
                        and r["session_id"] == outcome["execution_id"] for r in receipts))
    except (ValueError, KeyError, TypeError):
        return False


def local_outcome(case: dict, arm: str, stress=False) -> dict:
    from medplain.intake import IntakeError, normalize_source

    started = time.monotonic()
    outcome = _blank("deterministic" if arm == "baseline" else "fixture")
    try:
        bundle = normalize_source(case["source_text"])
        outcome["gate_challenges"] = gate_challenges(bundle.packet, case)
        execution_id = "evaluation:" + uuid4().hex
        if arm == "strands":
            from medplain.engine import run_packet

            _from_result(run_packet(bundle.packet, execution_id, model_mode="fixture", stress=stress,
                                    intake_source=bundle.intake), outcome)
        else:
            candidate = build_explanation(bundle.packet)
            gate = SafetyGate(bundle.packet)
            chain = ReceiptChain(execution_id, bundle.packet)
            if stress:
                altered = bundle.packet.model_copy(deep=True)
                from decimal import Decimal

                altered.medications[0].dose = str(Decimal(altered.medications[0].dose) * 10)
                changed = build_explanation(altered)
                chain.append(changed, gate.validate(changed))
                outcome["correction_count"] = 1
                # A competent deterministic baseline simply rebuilds from the
                # original normalized source after the same forced mutation.
                candidate = build_explanation(bundle.packet)
            verdict = gate.validate(candidate)
            chain.append(candidate, verdict)
            outcome.update(status="verified" if verdict.passed else "refused",
                           kind="verified" if verdict.passed else "gate_refusal",
                           extraction=bundle.expected_extraction.model_dump(),
                           explanation=candidate.model_dump() if verdict.passed else None,
                           explanation_text=render_explanation(candidate) if verdict.passed else None,
                           artifact_hash=chain.receipts[-1]["artifact_hash"], receipts=chain.receipts)
            outcome["execution_id"] = execution_id
        outcome["independent_publication_check"] = publication_valid(bundle.packet, outcome)
    except IntakeError as exc:
        outcome.update(status="refused", kind="input_refusal", error=str(exc))
    except Exception as exc:
        outcome.update(status="error", kind="runtime_error", error=type(exc).__name__)
    outcome["client_latency_ms"] = (time.monotonic() - started) * 1000
    return outcome


class RemoteRunner:
    """One bounded import attempt per case; polling is read-only. No AWS keys."""

    def __init__(self, url: str, expected_commit: str):
        if url.rstrip("/") != CANONICAL_URL or not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
            raise ValueError("Live evaluation requires the canonical URL and exact commit")
        self.url, self.token, self.jobs = CANONICAL_URL, None, 0
        status, health = self.request("/api/health")
        if status != 200 or health.get("commit") != expected_commit or health.get("model_mode") != "bedrock":
            raise ValueError("Live health does not match the expected Bedrock release")
        status, session = self.request("/api/ops/session", {"prepared_demo": False})
        if status != 201:
            raise ValueError("Could not start an explicit empty evaluation session")
        self.token = session["session_token"]
        self.commit = expected_commit

    def request(self, path: str, body=None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Medplain-Session"] = self.token
        req = urllib.request.Request(self.url + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def run(self, case: dict) -> dict:
        started = time.monotonic()
        outcome = _blank("bedrock")
        if self.jobs >= 8:
            outcome.update(kind="budget_not_executed", error="Eight-job budget reached; case retained as not executed.")
            return outcome
        submitted = False
        try:
            status, response = self.request("/api/ops/cases/import", {
                "source_text": case["source_text"], "synthetic_confirmed": True,
                "facility_id": "demo-athens", "idempotency_key": "eval-" + uuid4().hex})
            outcome["http_status"] = status
            if status == 422:
                outcome.update(status="refused", kind="input_refusal", error=response.get("detail"))
                outcome["failure_response"] = response
            elif status != 202:
                self.jobs += 1  # Conservatively reserve an indeterminate server response.
                outcome.update(error=response.get("detail", "Import request was not accepted"))
                outcome["failure_response"] = response
            else:
                self.jobs += 1
                submitted = True
                case_id = response["case"]["id"]
                outcome["case_id"] = case_id
                deadline = time.monotonic() + 240
                while True:
                    poll_status, detail = self.request("/api/ops/cases/" + case_id)
                    if poll_status != 200:
                        raise ValueError("Accepted case could not be read")
                    if detail["status"] not in {"queued", "processing"}:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Accepted job did not reach a terminal state")
                    time.sleep(1)
                outcome["worker_duration_ms"] = detail.get("duration_ms")
                outcome["case_status"] = detail["status"]
                if detail["status"] == "failed":
                    outcome["failure_response"] = detail
                if detail.get("result") is not None:
                    if (detail["result"].get("commit") != self.commit
                            or detail["result"].get("model_mode") != "bedrock"):
                        raise ValueError("Worker source commit mismatch")
                    _from_result(detail["result"], outcome)
                else:
                    outcome.update(kind="model_refusal", status="refused", error=detail.get("error"))
        except Exception as exc:
            if not submitted:
                # An interrupted import response may still have created a job.
                self.jobs += 1
            outcome.update(status="error", kind="runtime_error", error=type(exc).__name__)
        try:
            from medplain.intake import normalize_source

            bundle = normalize_source(case["source_text"])
            outcome["gate_challenges"] = gate_challenges(bundle.packet, case)
            outcome["independent_publication_check"] = publication_valid(bundle.packet, outcome)
        except ValueError:
            pass
        outcome["client_latency_ms"] = (time.monotonic() - started) * 1000
        return outcome


def _evaluators():
    from strands_evals.evaluators.evaluator import Evaluator
    from strands_evals.types.evaluation import EvaluationOutput

    class CriticalFactsEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            case = evaluation_case.input["case"]
            arm = evaluation_case.input["arm"]
            scores = score_outcome(case, evaluation_case.actual_output, arm)
            passed = scores["validity"] and (case["gold"] is None or
                                            scores["extraction_exact"] and scores["field_fidelity"])
            return [EvaluationOutput(score=float(passed), test_pass=passed,
                                     reason="Literal held-out facts and expected disposition", metadata=scores)]

    class ToolNodeTrajectoryEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            if evaluation_case.input["arm"] == "baseline":
                return [EvaluationOutput(score=0.0, test_pass=True, label="not_applicable",
                                         reason="Deterministic arm has no agent trajectory")]
            case = evaluation_case.input["case"]
            passed = trajectory_valid(evaluation_case.actual_output, case["gold"] is None)
            return [EvaluationOutput(score=float(passed), test_pass=passed,
                                     reason="Actual node order, fresh successful tools and receipt verdicts")]

    class CoherentMutationEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            probes = evaluation_case.actual_output["gate_challenges"]
            if not probes:
                return [EvaluationOutput(score=0.0, test_pass=True, label="not_applicable",
                                         reason="No shared-gate mutation assigned to this note")]
            count = sum(row["false_accept"] for row in probes)
            return [EvaluationOutput(score=float(count == 0), test_pass=count == 0,
                                     reason=f"{count} false accepts among {len(probes)} coherent mutations")]

    return [CriticalFactsEvaluator(), ToolNodeTrajectoryEvaluator(), CoherentMutationEvaluator()]


def aggregate(cases: list[dict], arm: str, mode: str) -> dict:
    outcomes = [row["outcomes"][arm] for row in cases]
    scores = [row["scores"][arm] for row in cases]
    eligible = sum(row["expected"]["gold"] is not None for row in cases)
    times = sorted(o["client_latency_ms"] for o in outcomes if o["client_latency_ms"] is not None)
    return {"case_count": len(cases), "verified_count": sum(o["status"] == "verified" for o in outcomes),
            "refused_count": sum(o["status"] == "refused" for o in outcomes),
            "runtime_error_count": sum(o["status"] == "error" for o in outcomes),
            "validity_passed": sum(s["validity"] for s in scores),
            "extraction_exact_count": sum(s["extraction_exact"] for s in scores),
            "extraction_eligible_count": eligible,
            "field_fidelity_passed": sum(s["field_fidelity"] for s in scores),
            "field_fidelity_eligible_count": eligible,
            "gate_false_accept_count": sum(s["gate_false_accepts"] for s in scores),
            "gate_mutation_count": sum(s["gate_mutations"] for s in scores),
            "corrected_count": sum(o["correction_count"] > 0 for o in outcomes),
            "trajectory_passed": sum(s["trajectory"] is True for s in scores),
            "trajectory_eligible_count": len(cases) if arm == "strands" else 0,
            "latency_ms": {"count": len(times), "median": statistics.median(times) if times else None,
                           "p95": times[max(0, math.ceil(len(times) * .95) - 1)] if times else None,
                           "scope": "CI/local client wall time; includes HTTP polling in Bedrock mode. "
                                    "Fixture latency is not Bedrock performance; worker durations remain per case."},
            "inference_usage": {"measured": False, "tokens": None,
                                "reason": "No complete cross-node inference usage collector is installed."},
            "provider_mode": "deterministic" if arm == "baseline" else mode}


def run_evaluation(source_commit: str, mode="fixture", judge_url=None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit) or mode not in {"fixture", "bedrock"}:
        raise ValueError("Exact source commit and fixture or bedrock mode are required")
    from strands_evals import Case, Experiment

    if importlib.metadata.version("strands-agents-evals") != EVALS_VERSION:
        raise ValueError("Use the pinned official Strands Evals optional dependency")
    harness_commit = os.getenv("GITHUB_SHA")
    if not harness_commit:
        try:
            harness_commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                     cwd=Path(__file__).resolve().parents[2]).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            harness_commit = None
    if mode == "bedrock" and harness_commit != source_commit:
        raise ValueError("Check out the exact deployed commit before comparing its deterministic baseline")
    heldout = corpus()
    remote = RemoteRunner(judge_url or "", source_commit) if mode == "bedrock" else None
    measured = {}
    eval_cases = [Case(name=f"{arm}:{case['id']}", input={"case": case, "arm": arm},
                       expected_output={"disposition": case["expected_disposition"], "facts": expected_facts(case)})
                  for case in heldout for arm in ("strands", "baseline")]

    def task(eval_case):
        case, arm = eval_case.input["case"], eval_case.input["arm"]
        outcome = remote.run(case) if arm == "strands" and remote else local_outcome(case, arm)
        measured[(case["id"], arm)] = outcome
        return {"output": outcome, "trajectory": outcome["nodes"]}

    evaluators = _evaluators()
    official = Experiment(cases=eval_cases, evaluators=evaluators).run_evaluations(task)
    expected_keys = {(case["id"], arm) for case in heldout for arm in ("strands", "baseline")}
    if set(measured) != expected_keys:
        raise RuntimeError("Official experiment did not execute every case and arm; no measurement report is valid")
    if (len(official.cases) != len(eval_cases) * len(evaluators)
            or len(official.test_passes) != len(official.cases)):
        raise RuntimeError("Official evaluator report is incomplete; no empty or partial score is valid")
    rows = [{"id": case["id"], "source_text": case["source_text"], "expected": case,
             "outcomes": {arm: measured[(case["id"], arm)] for arm in ("strands", "baseline")},
             "scores": {arm: score_outcome(case, measured[(case["id"], arm)], arm)
                        for arm in ("strands", "baseline")}} for case in heldout]
    drills = []
    if mode == "fixture":
        # Same source and same forced corruption on both competent paths.
        for arm in ("strands", "baseline"):
            outcome = local_outcome(heldout[0], arm, stress=True)
            drills.append({"arm": arm, "case_id": heldout[0]["id"], "scope": "forced offline correction drill",
                           "outcome": outcome, "trajectory_valid": trajectory_valid(outcome) if arm == "strands" else None})
    ruler = {"version": RULER_VERSION, "required_tools": REQUIRED_TOOLS, "mutations": MUTATIONS,
             "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "validity": "All expected valid and refused samples retained; runtime errors are not valid refusals.",
             "facts": "Exact annotated medication/lab/follow-up/precaution fields; no medical judgment.",
             "trajectory": "Node order, successful fresh required tools per invocation and receipt verdict sequence."}
    report = {"schema_version": 1, "source_commit": source_commit, "harness_commit": harness_commit,
              "generated_at": datetime.now(timezone.utc).isoformat(), "mode": mode,
              "framework": {"package": "strands-agents-evals", "version": EVALS_VERSION,
                            "sdk_version": importlib.metadata.version("strands-agents"),
                            "source": "https://github.com/strands-agents/evals/tree/v1.7.0",
                            "case_experiment_executed": True},
              "corpus": {"version": CORPUS_VERSION, "sha256": canonical_hash(heldout), "count": len(heldout)},
              "ci": {"run_id": os.getenv("GITHUB_RUN_ID"), "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
                     "repository": os.getenv("GITHUB_REPOSITORY"),
                     "run_url": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                                 if os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_RUN_ID") else None)},
              "source_files": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                               for name in ("engine.py", "intake.py", "gate.py", "clinical.py", "schemas.py")},
              "ruler": {**ruler, "sha256": canonical_hash(ruler)},
              "scope": "Paired held-out synthetic notes within one strict input grammar. "
                       "A competent deterministic baseline shares normalization, renderer and strict safety gate.",
              "arms": {arm: aggregate(rows, arm, mode) for arm in ("strands", "baseline")},
              "cases": rows, "correction_drills": drills,
              "official_evaluation": official.model_dump(mode="json"),
              "limitations": ["Fixture models replay plans and do not measure Bedrock extraction capability or cost.",
                              "Both arms share normalization and clinical gate; mutation checks do not prove agent superiority.",
                              "No measured comprehension, clinical appropriateness or patient outcome benefit.",
                              "Latency comparisons include different work; no speedup or token-savings claim.",
                              "LLM judges were not run; deterministic clinical checks are authoritative.",
                              "Remote raw intake does not expose fault injection; correction drills are offline only."]}
    report["measurement_status"] = {
        "execution": "complete", "outcome_validity": "all_expected" if all(
            c["validity_passed"] == c["case_count"] for c in report["arms"].values()) else "contains_failures",
        "deterministic_safety": "failed" if any(c["gate_false_accept_count"] for c in report["arms"].values()) else "passed"}
    report["report_sha256"] = canonical_hash(report)
    return report


def write_report(report: dict, path: Path):
    if report["report_sha256"] != canonical_hash({k: v for k, v in report.items() if k != "report_sha256"}):
        raise ValueError("Report integrity check failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
