"""A separate paired ruler for synthetic reconciliation and clarification.

Literal gold annotations live outside every model input. The complete baseline
receives the same dossier, reference facts and safety rules. A tie is publishable.
This module does not modify the original clinical or evaluation contracts.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

CANONICAL_URL = "https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws"
CORPUS_VERSION = "reconciliation-synthetic-v1"
RULER_VERSION = "reconciliation-questions-atoms-v1"
EVALS_VERSION = "1.7.0"
PROFILE = "medplain-reconciliation-v1"
MAX_JOBS = 30
MAX_RUN_SECONDS = 20 * 60
SELECTED_PAIR_UNIVERSE = (
    ("warfarin", "ibuprofen"), ("warfarin", "naproxen"),
    ("apixaban", "ibuprofen"), ("apixaban", "naproxen"),
)
FAMILIES = (
    "dose_stop_conflict", "intentional_change", "taper_phase",
    "hold_restart_logic", "caregiver_identity_form", "pending_followup_owner",
)
TARGETED_CODES = frozenset({
    "compare_instructions", "confirm_intentional_change", "confirm_taper",
    "confirm_hold_restart", "confirm_identity_form", "confirm_pending_owner",
    "confirm_interaction_review",
})
NODES = ("DischargeEvidenceIndexerAgent", "CrossDocumentClarificationPlannerAgent",
         "ClarificationCardPublisherAgent")
REQUIRED_TOOLS = {
    NODES[0]: ("read_reconciliation_sources",),
    NODES[1]: ("check_reconciliation_constraints", "lookup_reconciliation_references"),
    NODES[2]: ("build_clarification_card",),
}
CORPUS_PATH = Path(__file__).parent / "data" / "reconciliation_corpus.json"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def corpus(path: Path = CORPUS_PATH) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases", [])
    if (document.get("schema_version") != 1 or document.get("version") != CORPUS_VERSION
            or len(cases) != 30 or len({c.get("id") for c in cases}) != 30
            or Counter(c.get("family") for c in cases) != Counter({family: 5 for family in FAMILIES})):
        raise ValueError("The complete independent six-family corpus is required")
    for case in cases:
        if (not isinstance(case.get("dossier"), dict) or case["dossier"].get("profile") != PROFILE
                or set(case["dossier"]) != {
                    "profile", "patient_alias", "language", "caregiver_concerns", "documents"}
                or set(case.get("gold", {})) != {"clinical_by_statement", "structural_issues", "questions",
                                                 "selected_interactions", "interaction_control"}):
            raise ValueError("Gold annotations must be separate from the exact dossier input")
        source = source_records(case["dossier"])
        if set(case["gold"]["clinical_by_statement"]) != set(source):
            raise ValueError("Independent atom annotations must cover every source statement")
        for target in case["gold"]["structural_issues"] + case["gold"]["questions"]:
            required = set(target["required_statement_ids"])
            allowed = set(target["allowed_statement_ids"])
            if (not required or not required.issubset(allowed) or not allowed.issubset(source)
                    or not target["codes"]):
                raise ValueError("Every gold focus requires explicit source anchors and allowed context")
    return cases


def source_records(dossier: dict) -> dict[str, dict]:
    """Enumerate physical source lines independently of the application parser."""
    records = {}
    documents = list(dossier["documents"])
    if dossier["caregiver_concerns"]:
        documents.append({"document_id": "caregiver-concerns", "text": dossier["caregiver_concerns"]})
    for doc in documents:
        offset = 0
        for line, piece in enumerate(doc["text"].splitlines(keepends=True), start=1):
            quote = piece.rstrip("\r\n")
            start = offset
            offset += len(piece.encode("utf-8"))
            if not quote.strip():
                continue
            key = f"{doc['document_id']}:L{line}"
            if key in records:
                raise ValueError("Duplicate independent source statement")
            records[key] = {"document_id": doc["document_id"], "line": line,
                            "quote": quote, "byte_start": start,
                            "byte_end": start + len(quote.encode("utf-8")),
                            "document_sha256": hashlib.sha256(doc["text"].encode()).hexdigest()}
    return records


def match_focus(predictions: list[dict], targets: list[dict]) -> dict:
    """Maximum one-to-one matches: duplicate/unfocused proposals remain false positives.

    Gold permits explicitly annotated optional context and equivalent question
    codes, not missing required anchors. A broad all-document citation is not a
    substitute for identifying the relevant relationship.
    """
    adjacency = []
    for prediction in predictions:
        ids = prediction.get("statement_ids", [])
        valid = (isinstance(ids, list) and all(isinstance(item, str) for item in ids)
                 and len(ids) == len(set(ids)))
        selected = set(ids) if valid else set()
        adjacency.append([
            index for index, target in enumerate(targets)
            if valid and prediction.get("code") in target["codes"]
            and set(target["required_statement_ids"]).issubset(selected)
            and selected.issubset(target["allowed_statement_ids"])
        ])
    assigned = {}

    def assign(prediction, visited):
        for target in adjacency[prediction]:
            if target in visited:
                continue
            visited.add(target)
            if target not in assigned or assign(assigned[target], visited):
                assigned[target] = prediction
                return True
        return False

    for index in range(len(predictions)):
        assign(index, set())
    true_positive = len(assigned)
    return {"true_positive": true_positive, "false_positive": len(predictions) - true_positive,
            "false_negative": len(targets) - true_positive,
            "predicted": len(predictions), "expected": len(targets),
            "exact": true_positive == len(predictions) == len(targets)}


def source_scores(case: dict, artifact: dict | None) -> dict:
    expected = source_records(case["dossier"])
    statements = artifact.get("source_statements", []) if isinstance(artifact, dict) else []
    statements = statements if isinstance(statements, list) else []
    actual = {row["statement_id"]: row for row in statements
              if isinstance(row, dict) and isinstance(row.get("statement_id"), str)}
    coverage = len(actual) == len(statements) and set(actual) == set(expected)
    citations = bool(coverage)
    for key, reference in expected.items():
        citation = actual.get(key, {}).get("citation", {})
        citation = citation if isinstance(citation, dict) else {}
        citations = citations and type(citation.get("line")) is int and citation == reference
    atoms = coverage and all(actual[key].get("clinical") == gold
                             for key, gold in case["gold"]["clinical_by_statement"].items())
    return {"source_coverage": bool(coverage), "source_citations": bool(citations),
            "supported_atoms_exact": bool(atoms), "expected_source_statements": len(expected),
            "observed_source_statements": len(statements)}


def score_outcome(case: dict, outcome: dict) -> dict:
    result = outcome.get("result") or {}
    result = result if isinstance(result, dict) else {}
    artifact = result.get("artifact")
    artifact = artifact if isinstance(artifact, dict) else None
    questions = artifact.get("proposed_questions", []) if artifact else []
    questions = questions if isinstance(questions, list) else []
    focused = [row for row in questions if isinstance(row, dict) and row.get("code") in TARGETED_CODES]
    structural = artifact.get("structural_issues", []) if artifact else []
    structural = [row for row in structural if isinstance(row, dict)] if isinstance(structural, list) else []
    interactions = [row for row in structural if row.get("code") == "selected_label_interaction_review"]
    selected_targets = [{"codes": ["selected_label_interaction_review"],
                         "required_statement_ids": row["statement_ids"],
                         "allowed_statement_ids": row["statement_ids"]}
                        for row in case["gold"]["selected_interactions"]]
    interaction_match = match_focus(interactions, selected_targets)
    reference_matches = all(any(set(target["statement_ids"]) == set(row.get("statement_ids", []))
                                and isinstance(row.get("reference_ids"), list)
                                and set(target["reference_ids"]).issubset(row["reference_ids"])
                                for row in interactions)
                            for target in case["gold"]["selected_interactions"])
    valid = outcome.get("independent_validation") is True and outcome.get("status") == "verified"
    question_match = match_focus(focused, case["gold"]["questions"])
    structural_match = match_focus(structural, case["gold"]["structural_issues"])
    if not valid:
        for matching in [question_match, structural_match, interaction_match]:
            matching.update(true_positive=0, false_positive=matching["predicted"],
                            false_negative=matching["expected"], exact=False)
    return {
        "valid_outcome": valid,
        **source_scores(case, artifact),
        "question_focus": question_match,
        "structural_focus": structural_match,
        "proper_withholding": bool(valid and artifact and artifact.get("kind") == "clarification_plan"
                                   and "patient_regimen" in artifact and artifact["patient_regimen"] is None),
        "generic_questions": len(questions) - len(focused),
        "fresh_agent_trajectory": bool(valid and trajectory_valid(result)) if outcome["model_mode"] != "deterministic" else None,
        "selected_interaction_review": {**interaction_match, "reference_ids_match": reference_matches},
    }


def trajectory_valid(result: dict) -> bool:
    runtime = result.get("runtime", {})
    if not isinstance(runtime, dict):
        return False
    nodes, tools = runtime.get("nodes_executed", []), runtime.get("tools_executed", [])
    if (result.get("status") != "verified" or not isinstance(nodes, list) or not isinstance(tools, list)
            or any(not isinstance(name, str) for name in nodes)):
        return False
    corrections = result.get("correction_count")
    if type(corrections) is not int or corrections not in {0, 1}:
        return False
    collapsed = [name for index, name in enumerate(nodes) if index == 0 or nodes[index - 1] != name]
    if collapsed != list(NODES) + list(NODES[1:]) * corrections:
        return False
    fresh = {index: set() for index in range(1, len(nodes) + 1)}
    for tool in tools:
        if not isinstance(tool, dict):
            return False
        index = tool.get("invocation_index")
        if type(index) is not int or index not in fresh or tool.get("agent") != nodes[index - 1]:
            return False
        if tool.get("success") is True:
            fresh[index].add(tool.get("tool"))
    if any(name not in REQUIRED_TOOLS or not set(REQUIRED_TOOLS[name]).issubset(fresh[index])
           for index, name in enumerate(nodes, start=1)):
        return False
    receipts = result.get("receipts")
    return (isinstance(receipts, list) and all(isinstance(row, dict) for row in receipts)
            and [row.get("verdict") for row in receipts] == ["refused"] * corrections + ["verified"])


def blank_outcome(mode: str) -> dict:
    return {"status": "error", "kind": "runtime_error", "model_mode": mode, "result": None,
            "independent_validation": False, "error": None, "case_id": None, "case_status": None,
            "patient_url": None, "client_latency_ms": None, "worker_duration_ms": None}


def local_outcome(case: dict, arm: str, record=None) -> dict:
    from medplain.reconciliation import (
        deterministic_baseline, run_reconciliation, validate_dossier, validate_reconciliation_result,
    )

    started = time.monotonic()
    outcome = blank_outcome("fixture" if arm == "strands" else "deterministic")
    execution_id = "reconciliation-evaluation:" + uuid4().hex
    # No annotation, question target, gold atom or family label crosses this boundary.
    dossier = copy.deepcopy(case["dossier"])
    try:
        validated = validate_dossier(dossier)
        result = (run_reconciliation(validated, execution_id, mode="fixture") if arm == "strands"
                  else deterministic_baseline(validated))
        outcome["result"] = result
        if record:
            record(case, arm, outcome, "returned_before_validation")
        outcome["status"] = result.get("status", "error")
        outcome["kind"] = "verified" if outcome["status"] == "verified" else "workflow_refused"
        validate_reconciliation_result(validated, result, execution_id if arm == "strands" else None)
        outcome["independent_validation"] = True
    except Exception as exc:
        # Preserve returned evidence, but never mislabel a runtime/contract failure as abstention.
        outcome.update(status="error", kind="runtime_error", error=type(exc).__name__)
    outcome["client_latency_ms"] = (time.monotonic() - started) * 1000
    return outcome


class CheckpointJournal:
    """A bounded partial artifact exists before any potentially paid command."""

    def __init__(self, path: Path | None, cases: list[dict], source_commit: str, mode: str):
        self.path = path
        self.lock = threading.RLock()
        self.rows = {case["id"]: {"id": case["id"], "family": case["family"], "dossier": case["dossier"],
                                 "outcomes": {arm: {**blank_outcome(mode if arm == "strands" else "deterministic"),
                                                    "kind": "not_executed", "status": "not_executed"}
                                              for arm in ("strands", "baseline")}}
                     for case in cases}
        self.document = {"schema_version": 1, "source_commit": source_commit, "mode": mode,
                         "corpus_version": CORPUS_VERSION, "corpus_sha256": digest(cases),
                         "ruler_version": RULER_VERSION, "execution": "partial",
                         "job_reservations": 0, "job_cap": MAX_JOBS, "stage": "before_first_command"}
        self.write()

    def record(self, case: dict, arm: str, outcome: dict, stage: str):
        with self.lock:
            self.rows[case["id"]]["outcomes"][arm] = copy.deepcopy(outcome)
            self.document["stage"] = stage
            self.write()

    def reserved(self, count: int):
        with self.lock:
            self.document["job_reservations"] = count
            self.document["stage"] = "command_reserved_before_post"
            self.write()

    def write(self):
        if self.path is None:
            return
        body = {**self.document, "cases": list(self.rows.values())}
        body["checkpoint_sha256"] = digest(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")
        temporary.replace(self.path)


class RemoteRunner:
    """Canonical public API only, fixed global reservations and explicit empty sessions.

    No POST is retried after an indeterminate response. A new anonymous session
    does not reset this runner's global allowance or the server's daily allowance.
    """

    def __init__(self, url: str, source_commit: str, journal: CheckpointJournal):
        if url.rstrip("/") != CANONICAL_URL or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("The exact canonical public URL and source commit are required")
        self.url, self.commit, self.journal = CANONICAL_URL, source_commit, journal
        self.token = None
        self.jobs = 0
        self.session_commands = 0
        self.session_count = 0
        self.setup_error = False
        self.allowance_exhausted = False
        self.lock = threading.RLock()
        self.deadline = time.monotonic() + MAX_RUN_SECONDS
        status, health = self.request("/api/health")
        if status != 200 or health.get("commit") != source_commit or health.get("model_mode") != "bedrock":
            raise ValueError("Public health must match the exact deployed Bedrock source")

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["X-Medplain-Session"] = self.token
        request = urllib.request.Request(self.url + path, headers=headers,
                                         data=canonical(payload) if payload is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def new_session(self):
        self.session_count += 1
        try:
            if self.session_count > 2:
                raise ValueError("The fixed two-session scope ended")
            status, created = self.request("/api/ops/session", {"prepared_demo": False})
            if status != 201 or created.get("role") != "operator" or created.get("demo") is not True:
                raise ValueError("An explicitly empty synthetic operator session is required")
            self.token = created["session_token"]
            status, boot = self.request("/api/ops/bootstrap")
            if (status != 200 or boot["runtime"]["commit"] != self.commit
                    or boot["limits"]["max_cases"] != 20 or boot["collection"]["initialized"] is not True
                    or boot["collection"]["prepared_count"] != 0 or boot["collection"]["live_count"] != 0):
                raise ValueError("The source-bound empty session contract changed")
            self.session_commands = 0
        except Exception:
            self.token = None
            self.setup_error = True
            raise

    def run(self, case: dict) -> dict:
        with self.lock:
            return self._run(case)

    def _run(self, case: dict) -> dict:
        from medplain.reconciliation import validate_dossier, validate_reconciliation_result

        started = time.monotonic()
        outcome = blank_outcome("bedrock")
        if (self.jobs >= MAX_JOBS or time.monotonic() >= self.deadline
                or self.setup_error or self.allowance_exhausted):
            outcome.update(status="not_executed", kind="budget_not_executed",
                           error="Fixed job or wall-time allowance ended; no command submitted")
            self.journal.record(case, "strands", outcome, "not_executed_allowance")
            return outcome
        try:
            dossier = validate_dossier(copy.deepcopy(case["dossier"]))
            if not self.token or self.session_commands >= 20:
                self.new_session()
            self.jobs += 1
            self.session_commands += 1
            self.journal.reserved(self.jobs)
            payload = {"dossier": dossier, "synthetic_confirmed": True, "facility_id": "demo-athens",
                       "idempotency_key": uuid4().hex}
            outcome["command_fingerprint"] = digest(payload)
            self.journal.record(case, "strands", outcome, "reserved_before_post")
            status, accepted = self.request("/api/ops/cases/reconcile", payload)
            outcome["http_status"] = status
            outcome["accepted_response"] = accepted
            self.journal.record(case, "strands", outcome, "response_before_assertions")
            if status == 429:
                self.allowance_exhausted = True
            if status != 202 or accepted.get("created") is not True:
                raise ValueError("A new reconciliation command was not accepted")
            case_id = accepted["case"]["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id):
                raise ValueError("Invalid returned case identifier")
            outcome["case_id"] = case_id
            self.journal.record(case, "strands", outcome, "accepted_before_poll")
            deadline = min(time.monotonic() + 240, self.deadline)
            while True:
                status, detail = self.request("/api/ops/cases/" + case_id)
                outcome["last_case_response"] = detail
                if status != 200:
                    raise ValueError("Accepted reconciliation case could not be read")
                if detail.get("status") not in {"queued", "processing"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Accepted reconciliation job did not complete within its bound")
                time.sleep(1)
            outcome.update(result=detail.get("result"), worker_duration_ms=detail.get("duration_ms"),
                           case_status=detail.get("status"), patient_url=detail.get("patient_url"))
            self.journal.record(case, "strands", outcome, "terminal_before_validation")
            result = detail.get("result")
            if not isinstance(result, dict):
                raise ValueError("Terminal job has no auditable workflow result")
            if (detail.get("source") != dossier or detail.get("source_hash") != digest(dossier)
                    or detail.get("source_profile") != PROFILE
                    or result.get("commit") != self.commit or result.get("model_mode") != "bedrock"):
                raise ValueError("The source or actual worker commit differs from the submitted dossier")
            execution_id = f"{case_id}:{detail['generation']}"
            validate_reconciliation_result(dossier, result, execution_id)
            outcome.update(status=result["status"], kind="verified" if result["status"] == "verified"
                           else "workflow_refused", independent_validation=True)
            if result["status"] == "verified" and (detail.get("status") != "review_required"
                                                   or detail.get("patient_url") is not None
                                                   or detail.get("review") is not None):
                raise ValueError("Benchmark artifacts must remain awaiting review with no patient capability")
        except Exception as exc:
            outcome.update(status="error", kind="runtime_error", independent_validation=False,
                           error=type(exc).__name__)
        outcome["client_latency_ms"] = (time.monotonic() - started) * 1000
        self.journal.record(case, "strands", outcome, "case_complete_or_failed")
        return outcome


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def aggregate(rows: list[dict], arm: str) -> dict:
    outcomes = [row["outcomes"][arm] for row in rows]
    scores = [row["scores"][arm] for row in rows]
    result = {"case_count": len(rows),
              "verified_count": sum(row["status"] == "verified" for row in outcomes),
              "workflow_refused_count": sum(row["status"] == "refused" for row in outcomes),
              "runtime_error_count": sum(row["status"] == "error" for row in outcomes),
              "not_executed_count": sum(row["status"] == "not_executed" for row in outcomes)}
    for key in ["valid_outcome", "source_coverage", "source_citations", "supported_atoms_exact",
                "proper_withholding"]:
        result[key + "_count"] = sum(row[key] is True for row in scores)
    result["fresh_trajectory_count"] = sum(row["fresh_agent_trajectory"] is True for row in scores)
    result["trajectory_eligible_count"] = len(rows) if arm == "strands" else 0
    for key in ["question_focus", "structural_focus", "selected_interaction_review"]:
        totals = {name: sum(row[key][name] for row in scores)
                  for name in ["true_positive", "false_positive", "false_negative", "predicted", "expected"]}
        precision = _ratio(totals["true_positive"], totals["predicted"])
        recall = _ratio(totals["true_positive"], totals["expected"])
        totals.update(precision=precision, recall=recall,
                      f1=(2 * precision * recall / (precision + recall)
                          if precision is not None and recall is not None and precision + recall else
                          0.0 if totals["predicted"] or totals["expected"] else None),
                      exact_case_count=sum(row[key]["exact"] for row in scores))
        result[key] = totals
    result["selected_interaction_review"].update(
        reference_bound_positive_cases=sum(row["selected_interaction_review"]["reference_ids_match"]
                                           and bool(case["expected"]["gold"]["selected_interactions"])
                                           for row, case in zip(scores, rows, strict=True)),
        universe=[list(pair) for pair in SELECTED_PAIR_UNIVERSE],
        exercised_pairs=sorted({tuple(pair["pair"]) for row in rows
                                for pair in row["expected"]["gold"]["selected_interactions"]}),
        explicit_all_stopped_controls=sum(row["expected"]["gold"]["interaction_control"] == "all_stopped"
                                         for row in rows),
        scope="Selected label-reference review flags only, not all critical interactions or clinical clearance.")
    times = sorted(row["client_latency_ms"] for row in outcomes if row["client_latency_ms"] is not None)
    result["client_latency_ms"] = {
        "count": len(times), "median": statistics.median(times) if times else None,
        "p95": times[max(0, math.ceil(len(times) * .95) - 1)] if times else None,
        "scope": "Different work per arm; not a model speedup, cost or clinical-benefit comparison.",
    }
    result["inference_usage"] = {"measured": False, "tokens": None, "cost": None,
                                 "reason": "No complete cross-node usage collector is installed."}
    return result


def _evaluators():
    from strands_evals.evaluators.evaluator import Evaluator
    from strands_evals.types.evaluation import EvaluationOutput

    class SourceAndWithholdingEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            score = score_outcome(evaluation_case.input["case"], evaluation_case.actual_output)
            passed = all(score[key] for key in ["valid_outcome", "source_coverage", "source_citations",
                                               "supported_atoms_exact", "proper_withholding"])
            return [EvaluationOutput(score=float(passed), test_pass=passed,
                                     reason="Independent literal source atoms, full citations and no patient regimen",
                                     metadata=score)]

    class ContextFocusEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            score = score_outcome(evaluation_case.input["case"], evaluation_case.actual_output)["question_focus"]
            return [EvaluationOutput(score=float(score["exact"]), test_pass=score["exact"],
                                     reason="One-to-one targeted context matching; generic questions do not count",
                                     metadata=score)]

    class FreshTrajectoryEvaluator(Evaluator):
        def evaluate(self, evaluation_case):
            if evaluation_case.input["arm"] == "baseline":
                return [EvaluationOutput(score=0.0, test_pass=True, label="not_applicable",
                                         reason="The complete deterministic arm has no agent trajectory")]
            passed = trajectory_valid(evaluation_case.actual_output.get("result") or {})
            return [EvaluationOutput(score=float(passed), test_pass=passed,
                                     reason="Recorded nodes, fresh successful tools and receipt verdict sequence")]

    return [SourceAndWithholdingEvaluator(), ContextFocusEvaluator(), FreshTrajectoryEvaluator()]


def run_evaluation(source_commit: str, mode: str = "fixture", judge_url: str | None = None,
                   expected_corpus_sha256: str | None = None, checkpoint: Path | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit) or mode not in {"fixture", "bedrock"}:
        raise ValueError("Exact source commit and fixture or bedrock mode are required")
    from medplain.clinical_references import reference_material
    from strands_evals import Case, Experiment

    if importlib.metadata.version("strands-agents-evals") != EVALS_VERSION:
        raise ValueError("Use the pinned official Strands Evals optional dependency")
    cases = corpus()
    corpus_hash = digest(cases)
    if expected_corpus_sha256 is not None and expected_corpus_sha256 != corpus_hash:
        raise ValueError("The frozen reconciliation corpus changed")
    harness_commit = os.getenv("GITHUB_SHA")
    if not harness_commit:
        try:
            harness_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            harness_commit = None
    if mode == "bedrock" and (harness_commit != source_commit or expected_corpus_sha256 is None):
        raise ValueError("Bedrock measurement requires exact harness/source and explicitly frozen corpus hash")
    journal = CheckpointJournal(checkpoint, cases, source_commit, mode)
    remote = RemoteRunner(judge_url or "", source_commit, journal) if mode == "bedrock" else None
    measured = {}
    result_lock = threading.RLock()
    eval_cases = [Case(name=f"{arm}:{case['id']}", input={"case": case, "arm": arm},
                       expected_output={"annotation": case["gold"], "patient_regimen": None})
                  for case in cases for arm in ("strands", "baseline")]

    def task(eval_case):
        case, arm = eval_case.input["case"], eval_case.input["arm"]
        outcome = remote.run(case) if arm == "strands" and remote else local_outcome(case, arm, journal.record)
        journal.record(case, arm, outcome, "task_complete_before_scoring")
        with result_lock:
            measured[(case["id"], arm)] = outcome
        return {"output": outcome,
                "trajectory": (outcome.get("result") or {}).get("runtime", {}).get("nodes_executed", [])}

    evaluators = _evaluators()
    official = Experiment(cases=eval_cases, evaluators=evaluators).run_evaluations(task)
    expected_keys = {(case["id"], arm) for case in cases for arm in ("strands", "baseline")}
    if (set(measured) != expected_keys or len(official.cases) != len(eval_cases) * len(evaluators)
            or len(official.test_passes) != len(official.cases)):
        raise RuntimeError("The complete official experiment did not execute; retain partial checkpoint evidence")
    rows = [{"id": case["id"], "family": case["family"], "expected": case,
             "outcomes": {arm: measured[(case["id"], arm)] for arm in ("strands", "baseline")},
             "scores": {arm: score_outcome(case, measured[(case["id"], arm)]) for arm in ("strands", "baseline")}}
            for case in cases]
    ruler = {"version": RULER_VERSION, "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "required_tools": REQUIRED_TOOLS, "targeted_codes": sorted(TARGETED_CODES),
             "focus": "Maximum one-to-one matches with all required citations and only annotated optional context.",
             "generic_questions": "Generic caregiver questions and universal teachback do not score as targeted focus.",
             "source": "Every physical nonempty raw line, exact UTF-8 span/citation and independently annotated clinical dict.",
             "selected_interaction_universe": [list(pair) for pair in SELECTED_PAIR_UNIVERSE]}
    references = reference_material()
    report = {
        "schema_version": 1, "profile": PROFILE, "source_commit": source_commit, "harness_commit": harness_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(), "mode": mode,
        "framework": {"package": "strands-agents-evals", "version": EVALS_VERSION,
                      "sdk_version": importlib.metadata.version("strands-agents"), "case_experiment_executed": True},
        "corpus": {"version": CORPUS_VERSION, "sha256": corpus_hash, "count": len(cases),
                   "file_sha256": hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest(),
                   "family_counts": dict(Counter(row["family"] for row in cases))},
        "ruler": {**ruler, "sha256": digest(ruler)},
        "references": {"snapshot_sha256": references["snapshot_sha256"], "scope": references["scope"],
                       "interaction_service": references["interaction_service"]},
        "scope": "Thirty paired synthetic clarification dossiers; semantic proposals are not clinical truth or prescribing.",
        "budget": {"job_cap": MAX_JOBS, "job_reservations": remote.jobs if remote else 0,
                   "session_count": remote.session_count if remote else 0, "run_seconds_cap": MAX_RUN_SECONDS,
                   "scope": "New reconciliation measurement only; sessions never reset the shared daily allowance."},
        "source_files": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                         for name in ["reconciliation.py", "reconciliation_models.py", "reconciliation_engine.py",
                                      "reconciliation_evaluation.py", "clinical_references.py", "gate.py",
                                      "clinical.py", "evaluation.py"]},
        "ci": {"run_id": os.getenv("GITHUB_RUN_ID"), "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
               "repository": os.getenv("GITHUB_REPOSITORY")},
        "arms": {arm: aggregate(rows, arm) for arm in ("strands", "baseline")}, "cases": rows,
        "official_evaluation": official.model_dump(mode="json"),
        "limitations": [
            "Engineering-authored synthetic annotations are not clinician validation or clinical outcome evidence.",
            "Semantic context links remain proposals. Citations and integrity do not prove clinical interpretation.",
            "Fixture planner replay uses baseline rules; fixture semantic scores do not establish real model advantage.",
            "Both complete arms use the same sources, literal scanner, structural floor, references and strict gate.",
            "The four selected anticoagulant/NSAID reference pairs are not the universe of critical interactions.",
            "No unrestricted prescription is produced. All artifacts are clarification cards with patient_regimen null.",
            "Token use, cost, comprehension and health benefit are unmeasured. Latencies include different work.",
            "The old clinical gate, original corpus/ruler and original negative/positive reports remain separate.",
        ],
    }
    report["measurement_status"] = {
        "execution": "complete", "outcome_validity": "all_expected" if all(
            arm["valid_outcome_count"] == arm["case_count"] for arm in report["arms"].values()) else "contains_failures",
        "semantic_agent_advantage": "not_inferred; compare paired precision/recall and retained raw cases",
    }
    report["report_sha256"] = digest(report)
    return report


def write_report(report: dict, path: Path):
    if report.get("report_sha256") != digest({key: value for key, value in report.items() if key != "report_sha256"}):
        raise ValueError("Reconciliation report integrity check failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
