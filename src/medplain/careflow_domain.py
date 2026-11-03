"""Owned clarification tasks and provisional teach-back, separate from clinical truth.

Synthetic actors demonstrate deliberate assignment and acceptance. They are not
authenticated staff. An answer cannot edit a dossier, approve treatment, or close
a task. A distinct verified child dossier plus explicit confirmation is required.
"""

import copy
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from medplain import careflow_models as models
from medplain.careflow_store import CareflowStore, check_size
from medplain.gate import canonical_hash
from medplain.ops_domain import LeaseBusy, OpsError, OpsService, event, utc_now
from medplain.ops_store import Conflict, DailyLimit
from medplain.ops_validation import validate_case_evidence
from medplain.reconciliation_models import PROFILE

ACTORS = [
    {"id": "demo-pharmacist", "label": "Synthetic pharmacist", "kind": "pharmacist", "demo": True},
    {"id": "demo-discharge-clinician", "label": "Synthetic discharge clinician", "kind": "clinician", "demo": True},
    {"id": "demo-followup-nurse", "label": "Synthetic follow-up nurse", "kind": "nurse", "demo": True},
]
NOTICE = "Synthetic actors are demo selections, not authenticated staff. Task closure records a demo clinician confirmation, not clinical validation."
PATIENT_NOTICE = "Synthetic teach-back demonstration. Automated assessment is provisional and cannot certify understanding or authorize treatment."
MAX_ATTEMPTS = 3
MAX_COMMANDS = 128


def _request(model, value) -> dict:
    try:
        return model.model_validate(value.model_dump() if isinstance(value, BaseModel) else value).model_dump()
    except (ValidationError, ValueError, TypeError):
        raise OpsError(422, "Invalid synthetic workflow command. Check its required fields and limits.") from None


def _text(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise OpsError(422, "Synthetic text must be valid UTF-8.") from None
    if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise OpsError(422, "Enter nonempty synthetic text without control characters.")
    return value


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prompt(result: dict) -> dict:
    from medplain.careflow_teachback import make_teachback_prompt

    return make_teachback_prompt(result)


def _validate_assessment(case: dict, attempt: dict, result: dict):
    from medplain.careflow_teachback import validate_teachback_assessment

    validate_teachback_assessment(case["source"], case["result"], attempt["prompt"],
                                 attempt["response_text"], result, execution_id=attempt["execution_id"])


class CareflowService:
    def __init__(self, store):
        self.store = store
        self.rows = CareflowStore(store)
        self.ops = OpsService(store)

    def _session(self, pk, session, roles=None):
        latest = self.store.get(pk, "SESSION")
        if latest is None or latest["session_id"] != session.get("session_id"):
            raise OpsError(401, "This synthetic session expired. Start a new session.")
        if roles:
            self.ops.require(latest, roles)
        return latest

    def actors(self, pk: str, session: dict) -> dict:
        self._session(pk, session)
        selected = self.rows.actor(pk) or {}
        return {"actors": copy.deepcopy(ACTORS), "selected_actor_id": selected.get("actor_id"),
                "actor_version": selected.get("version", 0), "demo_notice": NOTICE}

    def select_actor(self, pk: str, session: dict, actor_id: str, expected_actor_version: int) -> dict:
        data = _request(models.ActorRequest, {"actor_id": actor_id, "expected_actor_version": expected_actor_version})
        latest = self._session(pk, session, {"operator", "reviewer", "admin"})
        try:
            self.rows.select_actor(pk, {"actor_id": data["actor_id"], "version": data["expected_actor_version"] + 1,
                                        "ttl": latest["ttl"]}, data["expected_actor_version"])
        except Conflict:
            raise OpsError(409, "The synthetic actor selection changed. Refresh before selecting again.") from None
        return self.actors(pk, latest)

    def _actor(self, pk: str, actor_id: str):
        selected = self.rows.actor(pk)
        if selected is None or selected["actor_id"] != actor_id:
            raise OpsError(403, "Explicitly select this synthetic actor before acting on its assignment.")

    def _case(self, pk: str, case_id: str) -> dict:
        case = self.ops.get_case(pk, case_id)
        if case.get("source_profile") != PROFILE or case["status"] not in {"review_required", "released"}:
            raise OpsError(409, "Careflow requires a completed, verified reconciliation case.")
        try:
            verified = validate_case_evidence(case)
            if verified is None or verified != case["artifact_hash"]:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise OpsError(409, "The saved source and artifact failed independent verification.") from None
        self._flow(case)
        return case

    def _flow(self, case: dict) -> dict:
        binding = {key: case[key] for key in ("source_hash", "artifact_hash", "generation")}
        existing = case.get("careflow")
        if existing is not None:
            if (not isinstance(existing, dict) or not isinstance(existing.get("requests"), dict)
                    or not isinstance(existing.get("teachback"), dict)
                    or not isinstance(existing["teachback"].get("attempts"), list)
                    or len(existing["teachback"]["attempts"]) > MAX_ATTEMPTS
                    or any(not isinstance(a, dict) for a in existing["teachback"]["attempts"])):
                raise OpsError(409, "The saved synthetic workflow has an invalid bounded structure.")
            if any(existing.get(key) != value for key, value in binding.items()):
                raise OpsError(409, "This workflow belongs to a different source or processing generation.")
        artifact = case["result"]["artifact"]
        citations = {s["statement_id"]: s["citation"] for s in artifact["source_statements"]}
        tasks = []
        for section in ("structural_issues", "proposed_questions", "teachback"):
            for index, question in enumerate(artifact[section]):
                code = question.get("question_code", question.get("code"))
                ids = question["statement_ids"]
                identity = {**binding, "section": section, "index": index, "code": code, "statement_ids": ids}
                tasks.append({"id": "cf-" + canonical_hash(identity)[:24], "section": section,
                              "code": code, "statement_ids": ids, "citations": [citations[s] for s in ids],
                              "prompt": question.get("question_text", question.get("text")), "status": "open",
                              "assignee_id": None, "due_at": None, "accepted_at": None,
                              "answer": None, "revision": None, "closure": None, "events": []})
        if existing is not None:
            immutable = ("id", "section", "code", "statement_ids", "citations", "prompt")
            if (not isinstance(existing.get("tasks"), list) or len(existing["tasks"]) != len(tasks)
                    or any(not isinstance(t, dict) for t in existing["tasks"])
                    or any(canonical_hash({k: saved.get(k) for k in immutable})
                           != canonical_hash({k: original[k] for k in immutable})
                           for saved, original in zip(existing["tasks"], tasks))):
                raise OpsError(409, "Saved workflow tasks differ from the independently verified artifact.")
            return existing
        flow = {**binding, "tasks": tasks, "requests": {}, "teachback": {"enabled": False, "attempts": []}}
        case["careflow"] = flow
        return flow

    def _revision(self, pk: str, parent: dict, revision_id: str) -> dict:
        child = self._case(pk, revision_id)
        if (child.get("parent_case_id") != parent["id"] or child["id"] == parent["id"]
                or child["source_hash"] == parent["source_hash"]
                or child["source"]["patient_alias"] != parent["source"]["patient_alias"]
                or child["facility_id"] != parent["facility_id"]):
            raise OpsError(409, "Choose a separately verified direct source revision for this same synthetic patient and desk.")
        return child

    def _view(self, pk: str, case: dict) -> dict:
        flow = self._flow(case)
        selected = self.rows.actor(pk) or {}
        revisions = []
        for row in self.store.cases(pk):
            if row.get("parent_case_id") != case["id"]:
                continue
            try:
                child = self._revision(pk, case, row["id"])
            except OpsError:
                continue
            revisions.append({"case_id": child["id"], "case_version": child["version"],
                              **{key: child[key] for key in ("source_hash", "artifact_hash", "status", "generation")},
                              "patient_alias": child["source"]["patient_alias"]})
        return {"case_id": case["id"], "case_version": case["version"], "source_hash": case["source_hash"],
                "artifact_hash": case["artifact_hash"], "generation": case["generation"], "demo": True,
                "notice": NOTICE, "selected_actor_id": selected.get("actor_id"),
                "tasks": copy.deepcopy(flow["tasks"]), "candidate_revisions": revisions,
                "teachback": {"enabled": flow["teachback"]["enabled"], "prompt": _prompt(case["result"]),
                              "attempts": [self._attempt_view(a, private=True) for a in flow["teachback"]["attempts"]]}}

    def view(self, pk: str, session: dict, case_id: str) -> dict:
        self._session(pk, session)
        return self._view(pk, self._case(pk, case_id))

    def _command(self, pk, session, case_id, model, raw, action, roles):
        data = _request(model, raw)
        self._session(pk, session, roles)
        case = self._case(pk, case_id)
        if "actor_id" in data:
            self._actor(pk, data["actor_id"])
        fingerprint = canonical_hash({"action": action, "request": data})
        key = _sha(data["idempotency_key"])
        prior = case["careflow"]["requests"].get(key)
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise OpsError(409, "This workflow idempotency key identifies another command.")
            return case, data, key, fingerprint, True
        if (case["version"] != data["expected_version"] or case["source_hash"] != data["source_hash"]
                or case["artifact_hash"] != data["artifact_hash"]):
            raise OpsError(409, "Use the current case version and exact source and artifact hashes.")
        if len(case["careflow"]["requests"]) >= MAX_COMMANDS:
            raise OpsError(429, "This synthetic workflow reached its command limit.")
        return case, data, key, fingerprint, False

    @staticmethod
    def _task(case, data, statuses=None):
        task = next((t for t in case["careflow"]["tasks"] if t["id"] == data["task_id"]), None)
        if task is None:
            raise OpsError(404, "This task is absent from the exact published artifact.")
        if statuses is not None and task["status"] not in statuses:
            raise OpsError(409, "This task is not at the required workflow stage.")
        if "actor_id" in data and task["assignee_id"] != data["actor_id"]:
            raise OpsError(403, "Only the explicitly selected assigned synthetic actor can perform this action.")
        return task

    def _save(self, pk, case, data, key, fingerprint, action, task=None, revision_guard=None):
        case["careflow"]["requests"][key] = {"fingerprint": fingerprint, "action": action}
        row = {**event("careflow_" + action, "Synthetic workflow " + action + " recorded; original clinical evidence is unchanged."),
               "actor_id": data.get("actor_id")}
        if task is not None:
            task["events"].append(row)
        case["events"].append(row)
        case.update(version=case["version"] + 1, updated_at=utc_now())
        check_size(case)
        try:
            if revision_guard is not None:
                self.rows.close(pk, case, data["expected_version"], revision_guard)
            else:
                self.store.replace(pk, case, data["expected_version"])
        except Conflict:
            latest = self.ops.get_case(pk, case["id"])
            prior = latest.get("careflow", {}).get("requests", {}).get(key)
            if not prior or prior["fingerprint"] != fingerprint:
                raise OpsError(409, "The case changed. Refresh before acting again.") from None
            case = self._case(pk, case["id"])
        return self._view(pk, case)

    def assign(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.AssignRequest, request, "assign", {"operator", "reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        task = self._task(case, data, {"open", "assigned"})
        try:
            due = datetime.fromisoformat(data["due_at"].replace("Z", "+00:00"))
            if due.tzinfo is None or not time.time() < due.timestamp() < case["ttl"]:
                raise ValueError()
        except (ValueError, OverflowError):
            raise OpsError(422, "Due time needs an explicit timezone and must precede this synthetic session's expiry.") from None
        task.update(status="assigned", assignee_id=data["assignee_id"], due_at=due.astimezone(timezone.utc).isoformat())
        return self._save(pk, case, data, key, fp, "assigned", task)

    def accept(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.AcceptRequest, request, "accept", {"reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        task = self._task(case, data, {"assigned"})
        task.update(status="accepted", accepted_at=utc_now())
        return self._save(pk, case, data, key, fp, "accepted", task)

    def answer(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.AnswerRequest, request, "answer", {"reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        task = self._task(case, data, {"accepted"})
        task.update(status="answered", answer={"text": _text(data["answer_text"]), "actor_id": data["actor_id"],
                                               "at": utc_now(), "authoritative": False})
        return self._save(pk, case, data, key, fp, "answered", task)

    def link_revision(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.LinkRevisionRequest, request, "link_revision", {"reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        task = self._task(case, data, {"answered", "revision_linked"})
        revision = self._revision(pk, case, data["revision_case_id"])
        task.update(status="revision_linked", revision={"case_id": revision["id"],
                    **{k: revision[k] for k in ("source_hash", "artifact_hash", "generation")}})
        return self._save(pk, case, data, key, fp, "revision_linked", task)

    def close(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.CloseRequest, request, "close", {"reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        task = self._task(case, data, {"revision_linked"})
        revision = self._revision(pk, case, task["revision"]["case_id"])
        if (data["clinician_confirmed"] is not True or data["revision_source_hash"] != revision["source_hash"]
                or data["revision_artifact_hash"] != revision["artifact_hash"]
                or task["revision"] != {"case_id": revision["id"], **{k: revision[k] for k in ("source_hash", "artifact_hash", "generation")}}):
            raise OpsError(409, "Closure requires explicit clinician confirmation of the exact independently verified linked revision.")
        task.update(status="closed", closure={"actor_id": data["actor_id"], "at": utc_now(),
                    "revision_case_id": revision["id"], "revision_source_hash": revision["source_hash"],
                    "revision_artifact_hash": revision["artifact_hash"]})
        return self._save(pk, case, data, key, fp, "closed", task, revision_guard=revision)

    def enable_teachback(self, pk, session, case_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.EnableTeachbackRequest, request, "enable_teachback", {"reviewer", "admin"})
        if case["status"] != "released" or not case["patient_url"]:
            raise OpsError(409, "Release the exact clarification card before enabling patient responses.")
        share_id = case["patient_url"].rsplit("/", 1)[-1]
        self.ops.patient(share_id)
        # Derivable only with the private hashed owner-token key. Replays return
        # the same capability; no raw write capability is persisted or read back.
        capability = hmac.new(pk.encode(), ("careflow:" + case_id + ":" + key).encode(), hashlib.sha256).hexdigest()
        cap_hash = _sha(capability)
        tb = case["careflow"]["teachback"]
        if replay:
            if tb.get("capability_hash") != cap_hash:
                raise OpsError(409, "This response capability was replaced. Use the newest private response link.")
        else:
            tb.update(enabled=True, capability_hash=cap_hash)
            case["careflow"]["requests"][key] = {"fingerprint": fp, "action": "enable_teachback"}
            expected = case["version"]
            case.update(version=expected + 1, updated_at=utc_now())
            case["events"].append(event("careflow_response_enabled", "A separate private response capability was issued; the read-only share gained no write authority."))
            mapping = {"owner_key": pk, "case_id": case_id, "share_id": share_id, "source_hash": case["source_hash"],
                       "artifact_hash": case["artifact_hash"], "generation": case["generation"], "ttl": case["ttl"]}
            try:
                self.rows.enable(pk, case, expected, "CFWRITE#" + cap_hash, mapping)
            except Conflict:
                latest = self._case(pk, case_id)
                prior = latest.get("careflow", {}).get("requests", {}).get(key)
                if not prior or prior["fingerprint"] != fp or latest["careflow"]["teachback"].get("capability_hash") != cap_hash:
                    raise OpsError(409, "This case changed. Refresh before enabling responses.") from None
                case = latest
        return {"careflow": self._view(pk, case), "response_capability": capability,
                "response_url": case["patient_url"] + "#teachback=" + capability}

    def _capability(self, share_id, capability):
        if (not isinstance(capability, str) or not re.fullmatch(r"[0-9a-f]{64}", capability)
                or not isinstance(share_id, str) or not 32 <= len(share_id) <= 128):
            raise OpsError(403, "A separate private response capability is required.")
        digest = _sha(capability)
        mapping = self.store.get("CFWRITE#" + digest, "CAPABILITY")
        if not mapping or mapping["share_id"] != share_id:
            raise OpsError(403, "This response capability is unavailable or belongs to another share.")
        pk = mapping["owner_key"]
        if self.store.get(pk, "SESSION") is None:
            raise OpsError(403, "This synthetic response capability expired.")
        case = self._case(pk, mapping["case_id"])
        if (case["status"] != "released" or case["patient_url"].rsplit("/", 1)[-1] != share_id
                or any(case[k] != mapping[k] for k in ("source_hash", "artifact_hash", "generation"))
                or case["careflow"]["teachback"].get("capability_hash") != digest):
            raise OpsError(403, "This response capability no longer matches the released artifact.")
        self.ops.patient(share_id)
        return pk, case

    @staticmethod
    def _attempt_view(attempt, *, private):
        result = {key: copy.deepcopy(attempt.get(key)) for key in
                  ("id", "status", "created_at", "response_text", "response_sha256", "error", "review")}
        assessment = attempt.get("assessment")
        if assessment is not None:
            # Explicit allowlist: private native diagnostics and retrieval payload
            # never travel to a response-capability reader.
            result["assessment"] = copy.deepcopy(assessment if private else {k: assessment[k] for k in
                ("status", "profile", "source_hash", "artifact_hash", "prompt_id", "prompt_sha256",
                 "response_sha256", "assessment", "assessment_sha256", "follow_up", "requires_reviewer", "patient_regimen")})
        else:
            result["assessment"] = None
        if not private and result["review"]:
            result["review"] = {k: result["review"][k] for k in ("decision", "at")}
        return result

    def _patient_view(self, case):
        for attempt in case["careflow"]["teachback"]["attempts"]:
            if attempt.get("assessment") is not None:
                try:
                    _validate_assessment(case, attempt, attempt["assessment"])
                except (ValueError, KeyError, TypeError):
                    raise OpsError(409, "The saved provisional assessment failed independent verification.") from None
        return {"case_id": case["id"], "case_version": case["version"], "source_hash": case["source_hash"],
                "artifact_hash": case["artifact_hash"], "prompt": _prompt(case["result"]),
                "attempts": [self._attempt_view(a, private=False) for a in case["careflow"]["teachback"]["attempts"]],
                "demo": True, "notice": PATIENT_NOTICE}

    def patient_view(self, share_id, capability):
        _, case = self._capability(share_id, capability)
        return self._patient_view(case)

    def submit_response(self, share_id, capability, request):
        data = _request(models.ResponseRequest, request)
        pk, case = self._capability(share_id, capability)
        _text(data["response_text"])
        if data["synthetic_confirmed"] is not True:
            raise OpsError(422, "Explicitly confirm the response is wholly invented. This is not PHI detection.")
        prompt = _prompt(case["result"])
        if data["prompt_id"] != prompt["prompt_id"]:
            raise OpsError(409, "This response must address the exact released teach-back prompt.")
        tb = case["careflow"]["teachback"]
        key, fp = _sha(data["idempotency_key"]), canonical_hash(data)
        prior = next((a for a in tb["attempts"] if a["request_key"] == key), None)
        if prior:
            if prior["request_fingerprint"] != fp:
                raise OpsError(409, "This response idempotency key identifies different content.")
            return self._patient_view(case), False, prior["execution_id"], self._message(pk, case, prior["id"])
        if case["version"] != data["expected_version"]:
            raise OpsError(409, "The response history changed. Refresh before submitting.")
        if len(tb["attempts"]) >= MAX_ATTEMPTS:
            raise OpsError(429, "This source has reached its limit of three synthetic response attempts.")
        if tb["attempts"] and (tb["attempts"][-1]["status"] != "reviewed"
                               or (tb["attempts"][-1].get("review") or {}).get("decision") != "follow_up"):
            raise OpsError(409, "A synthetic reviewer must explicitly request follow-up before another response assessment.")
        attempt_id = uuid4().hex
        attempt = {"id": attempt_id, "status": "queued", "created_at": utc_now(), "prompt": prompt,
                   "response_text": data["response_text"], "response_sha256": _sha(data["response_text"]),
                   "request_key": key, "request_fingerprint": fp, "assessment": None, "error": None,
                   "review": None, "lease_owner": None, "lease_until": 0,
                   "execution_id": f"{case['id']}:teachback:{attempt_id}"}
        tb["attempts"].append(attempt)
        case.update(version=case["version"] + 1, updated_at=utc_now())
        case["events"].append(event("careflow_teachback_queued", "A synthetic patient response and assessment outbox committed together; no understanding or regimen was approved."))
        message = self._message(pk, case, attempt_id)
        outbox = {k: v for k, v in message.items() if k != "session_key"}
        outbox.update(sk=f"OUTBOX#CARE#{case['id']}#{attempt_id}", ttl=case["ttl"], sent_at=None)
        check_size(case)
        try:
            self.store.replace(pk, case, data["expected_version"], outbox=outbox)
        except (Conflict, DailyLimit) as exc:
            _, latest = self._capability(share_id, capability)
            prior = next((a for a in latest["careflow"]["teachback"]["attempts"] if a["request_key"] == key), None)
            if prior and prior["request_fingerprint"] == fp:
                return self._patient_view(latest), False, prior["execution_id"], self._message(pk, latest, prior["id"])
            if isinstance(exc, DailyLimit):
                raise OpsError(429, "Daily synthetic job allowance reached; resets at 00:00 UTC.") from None
            raise OpsError(409, "The response history changed. Refresh before submitting.") from None
        return self._patient_view(case), True, attempt["execution_id"], message

    @staticmethod
    def _message(pk, case, attempt_id):
        return {"session_key": pk, "case_id": case["id"], "generation": case["generation"],
                "attempt_id": attempt_id, "job_kind": "careflow_teachback"}

    def claim_teachback(self, pk, case_id, attempt_id, generation):
        if type(generation) is not int:
            return None
        case = self._case(pk, case_id)
        if case["generation"] != generation or case["status"] != "released":
            return None
        attempt = next((a for a in case["careflow"]["teachback"]["attempts"] if a["id"] == attempt_id), None)
        if attempt is None or attempt["status"] not in {"queued", "processing"}:
            return None
        if attempt["status"] == "processing" and attempt["lease_until"] > time.time():
            raise LeaseBusy()
        expected = case["version"]
        lease = uuid4().hex
        attempt.update(status="processing", lease_owner=lease, lease_until=int(time.time()) + 210)
        case.update(version=expected + 1, updated_at=utc_now())
        case["events"].append(event("careflow_teachback_claimed", "A fenced worker lease claimed the queued provisional assessment."))
        try:
            self.store.replace(pk, case, expected)
        except Conflict:
            raise LeaseBusy() from None
        return {"case_id": case_id, "generation": generation, "attempt_id": attempt_id, "lease_owner": lease,
                "source": copy.deepcopy(case["source"]), "result": copy.deepcopy(case["result"]),
                "prompt": copy.deepcopy(attempt["prompt"]), "response_text": attempt["response_text"],
                "execution_id": attempt["execution_id"], "source_hash": case["source_hash"], "artifact_hash": case["artifact_hash"]}

    def complete_teachback(self, pk, claim, result, error=None):
        # Other task commands can advance the shared case version during an
        # assessment. Re-read and CAS a bounded number of times, preserving them.
        for _ in range(3):
            case = self._case(pk, claim["case_id"])
            if case["generation"] != claim["generation"]:
                return False
            attempt = next((a for a in case["careflow"]["teachback"]["attempts"] if a["id"] == claim["attempt_id"]), None)
            if (attempt is None or attempt["status"] != "processing" or case["source_hash"] != claim["source_hash"]
                    or case["artifact_hash"] != claim["artifact_hash"]):
                return False
            if attempt["lease_owner"] != claim["lease_owner"] or attempt["lease_until"] <= time.time():
                raise LeaseBusy()
            safe_error = None
            if error is not None:
                safe_error = "assessment_unavailable"
            else:
                try:
                    _validate_assessment(case, attempt, result)
                except (ValueError, KeyError, TypeError):
                    safe_error = "assessment_integrity_failed"
            if safe_error is not None:
                attempt.update(status="failed", assessment=None, error=safe_error)
            else:
                attempt.update(status="assessed" if result["status"] == "provisional" else "failed",
                               assessment=copy.deepcopy(result), error=None if result["status"] == "provisional" else "assessment_refused")
            attempt.update(lease_owner=None, lease_until=0)
            expected = case["version"]
            case.update(version=expected + 1, updated_at=utc_now())
            case["events"].append(event("careflow_teachback_completed", "Provisional assessment completed or was withheld; clinical tasks and the released artifact are unchanged."))
            check_size(case)
            try:
                self.store.replace(pk, case, expected)
                return True
            except Conflict:
                continue
        raise LeaseBusy()

    def review_teachback(self, pk, session, case_id, attempt_id, request):
        case, data, key, fp, replay = self._command(pk, session, case_id, models.TeachbackReviewRequest, request,
                                                   "review_teachback:" + attempt_id, {"reviewer", "admin"})
        if replay:
            return self._view(pk, case)
        attempt = next((a for a in case["careflow"]["teachback"]["attempts"] if a["id"] == attempt_id), None)
        if attempt is None:
            raise OpsError(404, "This teach-back attempt is unavailable.")
        if attempt["status"] not in {"assessed", "failed"}:
            raise OpsError(409, "Only a completed provisional assessment or retained failure can be reviewed.")
        if attempt["status"] == "failed" and data["decision"] != "follow_up":
            raise OpsError(409, "A failed assessment can only receive an explicit follow-up request, never acknowledgement as usable evidence.")
        if attempt["assessment"] is not None:
            try:
                _validate_assessment(case, attempt, attempt["assessment"])
            except (ValueError, KeyError, TypeError):
                raise OpsError(409, "The saved provisional assessment failed independent verification.") from None
        if data["comment"]:
            _text(data["comment"])
        attempt.update(status="reviewed", review={"decision": data["decision"], "actor_id": data["actor_id"],
                                                  "at": utc_now(), "comment": data["comment"]})
        return self._save(pk, case, data, key, fp, "teachback_reviewed")
