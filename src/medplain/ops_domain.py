"""Synthetic case lifecycle with owner-scoped capabilities and fenced execution."""

import base64
import copy
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from medplain.clinical import parse_packet
from medplain.fixtures import get_packet, get_packets
from medplain.gate import canonical_hash
from medplain.ops_store import Conflict, DailyLimit, daily_allowance
from medplain.schemas import ClinicalPacket

ROLES = ["operator", "reviewer", "auditor", "admin"]
STATUSES = ["queued", "processing", "review_required", "released", "failed", "rejected"]
FACILITIES = [
    {"id": "demo-athens", "name": "Synthetic Athens discharge desk", "city": "Athens",
     "lat": 37.9838, "lon": 23.7275, "synthetic": True},
    {"id": "demo-thessaloniki", "name": "Synthetic Thessaloniki discharge desk", "city": "Thessaloniki",
     "lat": 40.6401, "lon": 22.9444, "synthetic": True},
    {"id": "demo-patras", "name": "Synthetic Patras discharge desk", "city": "Patras",
     "lat": 38.2466, "lon": 21.7346, "synthetic": True},
]
SUMMARY_KEYS = ["id", "packet_id", "packet_title", "facility_id", "status", "version", "generation",
                "created_at", "updated_at", "duration_ms", "correction_count", "source_hash",
                "artifact_hash", "patient_url", "error"]


class OpsError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class LeaseBusy(Exception):
    """Redelivery must wait while another attempt holds the execution lease."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def commit() -> str:
    build = Path(__file__).parent / "_build.json"
    if build.exists():
        return json.loads(build.read_text())["commit"]
    return os.getenv("MEDPLAIN_COMMIT") or "development"


def event(kind: str, message: str) -> dict:
    return {"id": uuid4().hex, "type": kind, "at": utc_now(), "message": message}


def summary(case: dict) -> dict:
    dossier = case.get("source_profile") == "medplain-reconciliation-v1"
    artifact = (case.get("result") or {}).get("artifact") or {}
    return {**{key: case[key] for key in SUMMARY_KEYS},
            "origin": case.get("origin", "unknown"), "prepared": case.get("prepared"),
            "source_kind": "reconciliation" if dossier else "imported_note" if case.get("intake") is not None else "sample",
            "source_sha256": case["intake"]["source_sha256"] if case.get("intake") else None,
            "source_profile": case.get("source_profile"), "parent_case_id": case.get("parent_case_id"),
            "artifact_kind": "clarification_plan" if dossier else "patient_explanation",
            "structural_issue_count": len(artifact.get("structural_issues", [])) if dossier else None}


def detail(case: dict) -> dict:
    return {**summary(case), "source": case["source"], "result": case["result"],
            "events": case["events"], "review": case["review"],
            "prepared_history": case.get("prepared_history", []), "intake": case.get("intake")}


def outbox_for(case: dict) -> dict:
    return {"sk": f"OUTBOX#{case['id']}#{case['generation']}", "case_id": case["id"],
            "generation": case["generation"], "ttl": case["ttl"], "sent_at": None}


def _key(token: str) -> str:
    return "SESSION#" + hashlib.sha256(token.encode()).hexdigest()


class OpsService:
    def __init__(self, store):
        self.store = store

    def new_session(self, prepared_demo: bool | None = None) -> dict:
        # Validate packaged evidence before accepting a populated-session request.
        from medplain.ops_seed import load_bundle

        bundle = load_bundle() if prepared_demo is True else None
        token = secrets.token_urlsafe(32)
        ttl = int(time.time()) + 86400
        session = {"session_id": uuid4().hex, "role": "operator", "roles": ROLES.copy(),
                   "expires_at": datetime.fromtimestamp(ttl, timezone.utc).isoformat(),
                   "demo": True, "ttl": ttl, "case_count": 0,
                   "collection": {"mode": "empty", "initialized": prepared_demo is not None,
                                  "seed_version": None}}
        pk = _key(token)
        self.store.put_new(pk, "SESSION", session)
        if bundle is not None:
            self.seed_demo(pk, session, bundle)
            session = self.store.get(pk, "SESSION")
        return {key: value for key, value in {**session, "session_token": token}.items()
                if key not in {"ttl", "case_count"}}

    def authenticate(self, token: str | None) -> tuple[str, dict]:
        if not token or not 32 <= len(token) <= 128:
            raise OpsError(401, "Start a new anonymous synthetic session.")
        pk = _key(token)
        session = self.store.get(pk, "SESSION")
        if not session:
            raise OpsError(401, "This synthetic session expired. Start a new session.")
        return pk, session

    def role(self, pk: str, role: str) -> dict:
        if role not in ROLES:
            raise OpsError(422, "Choose an available synthetic demo role.")
        self.store.set_role(pk, role)
        return {"session_id": self.store.get(pk, "SESSION")["session_id"], "role": role,
                "roles": ROLES.copy(), "demo": True}

    def require(self, session: dict, roles: set[str]):
        if session["role"] not in roles:
            raise OpsError(403, "This action is unavailable in the selected synthetic demo role.")

    def bootstrap(self, session: dict, pk: str | None = None) -> dict:
        from medplain.reconciliation_engine import configured_model_ids

        rows = self.store.cases(pk) if pk else []
        collection = {"mode": "empty", "initialized": False, "seed_version": None,
                      **session.get("collection", {}),
                      "prepared_count": sum(c.get("origin") == "prepared_demo" for c in rows),
                      "live_count": sum(c.get("origin") == "live_run" for c in rows)}
        return {"session": {key: session[key] for key in ["session_id", "role", "roles", "demo"]},
                "collection": collection,
                "workspaces": [{"id": "all", "name": "All synthetic desks"}]
                + [{"id": f["id"], "name": f["name"]} for f in FACILITIES],
                "facilities": FACILITIES,
                "packets": [{"id": p.packet_id, "title": p.title, "summary": p.subtitle,
                             "stress_available": p.adversarial} for p in get_packets()],
                "runtime": {"model_mode": os.getenv("MEDPLAIN_MODEL_MODE", "fixture"),
                            "reconciliation_model_ids": configured_model_ids(),
                            "transport_mode": self.store.transport_mode,
                            "commit": commit(), "storage": self.store.storage},
                "limits": {"max_cases": 20, "daily_jobs": daily_allowance()[1], "daily_reset": "00:00 UTC"}}

    def seed_demo(self, pk: str, session: dict, bundle: dict | None = None) -> dict:
        from medplain.ops_seed import SCENARIOS, check_seed_size, load_bundle

        self.require(session, {"operator", "admin"})
        latest = self.store.get(pk, "SESSION")
        if latest is None:
            raise OpsError(401, "This synthetic session expired. Start a new session.")
        if latest.get("collection", {}).get("seed_version"):
            return self.bootstrap(latest, pk)
        if latest["case_count"]:
            raise OpsError(409, "Prepared examples can only be loaded into an empty synthetic session.")
        bundle = bundle if bundle is not None else load_bundle()
        cases, shares = [], []
        now = utc_now()
        for facility_id, status, name in SCENARIOS:
            exemplar = bundle["exemplars"][name]
            result = copy.deepcopy(exemplar["result"])
            source = copy.deepcopy(exemplar["source"])
            prepared = {"version": bundle["version"], "prepared_at": bundle["prepared_at"],
                        "source_commit": bundle["source_commit"],
                        "original_execution_id": exemplar["execution_id"],
                        "original_artifact_hash": exemplar["artifact_hash"]}
            case = {"id": uuid4().hex, "packet_id": source["packet_id"], "packet_title": source["title"],
                    "facility_id": facility_id, "status": status, "version": 1, "generation": 1,
                    "created_at": now, "updated_at": now, "duration_ms": None,
                    "correction_count": result.get("correction_count", 0),
                    "source_hash": canonical_hash(source), "artifact_hash": exemplar["artifact_hash"],
                    "patient_url": None, "error": None, "source": source, "result": result,
                    "events": [event("prepared_demo", "Prepared synthetic example copied from a CI offline SDK run; no live job ran for this copy.")],
                    "review": None, "ttl": latest["ttl"], "stress": source["adversarial"],
                    "lease_owner": None, "lease_until": 0, "execution_id": exemplar["execution_id"],
                    "retry_requests": {}, "origin": "prepared_demo", "prepared": prepared,
                    "prepared_history": []}
            if status == "failed":
                case["error"] = ("Prepared failure illustration from an intentionally interrupted offline fixture run. "
                                 "No live provider outage occurred.")
            if status == "rejected":
                case["events"].append(event("prepared_demo", "Prepared rejection scenario for exploring retry; no staff decision or live processing history is implied."))
            if status == "released":
                shares.append(self.make_share(case))
            cases.append(case)
        check_seed_size(cases, shares)
        collection = {"mode": "prepared_demo", "initialized": True, "seed_version": bundle["version"]}
        try:
            self.store.seed(pk, cases, shares, collection)
        except Conflict:
            latest = self.store.get(pk, "SESSION")
            if latest and latest.get("collection", {}).get("seed_version"):
                return self.bootstrap(latest, pk)
            raise OpsError(409, "This session changed. Prepared examples require an empty session.") from None
        return self.bootstrap(self.store.get(pk, "SESSION"), pk)

    def get_case(self, pk: str, case_id: str) -> dict:
        case = self.store.get(pk, "CASE#" + case_id)
        if case is None:
            raise OpsError(404, "This case is unavailable in your synthetic session.")
        return case

    def import_case(self, pk: str, session: dict, source_text: str, synthetic_confirmed: bool,
                    facility_id: str, idempotency_key: str) -> tuple[dict, bool, str]:
        from medplain.intake import IntakeError, normalize_source

        self.require(session, {"operator", "admin"})
        if synthetic_confirmed is not True:
            raise OpsError(422, "Explicitly confirm that the submitted note is wholly invented.")
        try:
            bundle = normalize_source(source_text)
        except IntakeError as exc:
            raise OpsError(422, str(exc)) from None
        return self.create(pk, session, bundle.packet.packet_id, facility_id, False, idempotency_key,
                           _compiled_packet=bundle.packet, _intake=bundle.intake)

    def reconcile_case(self, pk: str, session: dict, dossier: dict, synthetic_confirmed: bool,
                       facility_id: str, idempotency_key: str, parent_case_id: str | None = None,
                       stress: bool = False) -> tuple[dict, bool, str]:
        """Queue a new immutable dossier or revision, without replacing old evidence."""
        from medplain.reconciliation import PROFILE, validate_dossier

        self.require(session, {"operator", "admin"})
        if synthetic_confirmed is not True or type(stress) is not bool:
            raise OpsError(422, "Explicitly confirm that all source documents are invented.")
        if facility_id not in {f["id"] for f in FACILITIES}:
            raise OpsError(422, "Choose one of the synthetic discharge desks.")
        if parent_case_id is not None:
            parent = self.get_case(pk, parent_case_id)
            if parent.get("source_profile") != PROFILE:
                raise OpsError(422, "Source revisions require an existing reconciliation dossier.")
        try:
            source = validate_dossier(dossier)
        except (ValueError, TypeError):
            raise OpsError(422, "Invalid synthetic dossier. Check document identifiers, kinds, dates and source limits.") from None
        source_hash = canonical_hash(source)
        fingerprint = canonical_hash({"source_profile": PROFILE, "source_hash": source_hash,
                                      "facility_id": facility_id, "stress": stress,
                                      "parent_case_id": parent_case_id})
        idem_sk = "IDEM#CREATE#" + hashlib.sha256(idempotency_key.encode()).hexdigest()

        def existing():
            prior = self.store.get(pk, idem_sk)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise OpsError(409, "This idempotency key already identifies a different request.")
                case = self.get_case(pk, prior["case_id"])
                return case, False, f"{case['id']}:1"
            return None

        replay = existing()
        if replay:
            return replay
        if session["case_count"] >= 20:
            raise OpsError(429, "This session has reached its limit of 20 synthetic cases.")
        now = utc_now()
        case = {"id": uuid4().hex, "packet_id": "dossier-" + source_hash[:24],
                "packet_title": "Clarification for " + source["patient_alias"],
                "facility_id": facility_id, "status": "queued", "version": 1, "generation": 1,
                "created_at": now, "updated_at": now, "duration_ms": None, "correction_count": 0,
                "source_hash": source_hash, "artifact_hash": None, "patient_url": None, "error": None,
                "source": source, "source_profile": PROFILE, "parent_case_id": parent_case_id,
                "result": None, "intake": None, "events": [event(
                    "queued", "Invented source dossier committed with its processing outbox entry; no treatment plan is inferred.")],
                "review": None, "ttl": session["ttl"], "stress": stress, "lease_owner": None,
                "lease_until": 0, "execution_id": None, "retry_requests": {},
                "origin": "live_run", "prepared": None}
        if parent_case_id is not None:
            case["events"].append(event("source_revision", "New source revision linked to case " + parent_case_id
                                        + "; the original case and verification evidence remain unchanged."))
        idem = {"sk": idem_sk, "case_id": case["id"], "fingerprint": fingerprint, "ttl": session["ttl"]}
        try:
            self.store.create(pk, case, outbox_for(case), idem)
        except (Conflict, DailyLimit) as exc:
            replay = existing()
            if replay:
                return replay
            if isinstance(exc, DailyLimit):
                raise OpsError(429, "Daily synthetic job allowance reached; resets at 00:00 UTC.") from None
            if len(self.store.cases(pk)) >= 20:
                raise OpsError(429, "This session has reached its limit of 20 synthetic cases.") from None
            raise OpsError(409, "The case changed concurrently. Refresh and retry.") from None
        return case, True, f"{case['id']}:1"

    def create(self, pk: str, session: dict, packet_id: str, facility_id: str,
               stress: bool, idempotency_key: str, *, _compiled_packet: ClinicalPacket | None = None,
               _intake: dict | None = None) -> tuple[dict, bool, str]:
        self.require(session, {"operator", "admin"})
        if facility_id not in {f["id"] for f in FACILITIES}:
            raise OpsError(422, "Choose one of the synthetic discharge desks.")
        try:
            packet = parse_packet(_compiled_packet if _compiled_packet is not None else get_packet(packet_id))
        except KeyError:
            raise OpsError(404, "Choose one of the synthetic sample packets.") from None
        if stress and not packet.adversarial:
            raise OpsError(422, "The deliberate mutation is available on the stress packet only.")
        fingerprint = canonical_hash({"packet_id": packet_id, "facility_id": facility_id, "stress": stress})
        if _intake is not None:
            from medplain.intake import validate_intake_source

            validate_intake_source(packet, _intake)
            fingerprint = canonical_hash({"source_kind": "imported_note", "intake": _intake,
                                          "source_hash": canonical_hash(packet.model_dump()),
                                          "facility_id": facility_id})
        idem_sk = "IDEM#CREATE#" + hashlib.sha256(idempotency_key.encode()).hexdigest()

        def existing():
            record = self.store.get(pk, idem_sk)
            if record:
                if record["fingerprint"] != fingerprint:
                    raise OpsError(409, "This idempotency key already identifies a different request.")
                found = self.get_case(pk, record["case_id"])
                return found, False, f"{found['id']}:1"
            return None

        replay = existing()
        if replay:
            return replay
        if session["case_count"] >= 20:
            raise OpsError(429, "This session has reached its limit of 20 synthetic cases.")
        now = utc_now()
        case = {"id": uuid4().hex, "packet_id": packet_id, "packet_title": packet.title,
                "facility_id": facility_id, "status": "queued", "version": 1, "generation": 1,
                "created_at": now, "updated_at": now, "duration_ms": None, "correction_count": 0,
                "source_hash": canonical_hash(packet.model_dump()), "artifact_hash": None,
                "patient_url": None, "error": None, "source": packet.model_dump(), "result": None,
                "events": [event("queued", "Synthetic case committed with its processing outbox entry.")],
                "review": None, "ttl": session["ttl"], "stress": stress, "lease_owner": None,
                "lease_until": 0, "execution_id": None, "retry_requests": {},
                "origin": "live_run", "prepared": None}
        if _intake is not None:
            case["intake"] = copy.deepcopy(_intake)
            case["events"].append(event("source_accepted", "Original note and source ledger committed; parser Agent extraction is pending."))
        idem = {"sk": idem_sk, "case_id": case["id"], "fingerprint": fingerprint, "ttl": session["ttl"]}
        try:
            self.store.create(pk, case, outbox_for(case), idem)
        except (Conflict, DailyLimit) as exc:
            replay = existing()
            if replay:
                return replay
            if isinstance(exc, DailyLimit):
                raise OpsError(429, "Daily synthetic job allowance reached; resets at 00:00 UTC.") from None
            if len(self.store.cases(pk)) >= 20:
                raise OpsError(429, "This session has reached its limit of 20 synthetic cases.") from None
            raise OpsError(409, "The case changed concurrently. Refresh and retry.") from None
        return case, True, f"{case['id']}:1"

    def list_cases(self, pk: str, workspace="all", status="", q="", sort="created_at",
                   direction="desc", cursor="", limit=10) -> dict:
        self.workspace(workspace)
        if status and status not in STATUSES:
            raise OpsError(422, "Choose an available case status.")
        if sort not in {"created_at", "updated_at", "packet_title", "status", "duration_ms",
                        "facility_id", "correction_count"}:
            raise OpsError(422, "Choose an available sort field.")
        if direction not in {"asc", "desc"} or not 1 <= limit <= 20:
            raise OpsError(422, "Choose a valid page size and sort direction.")
        query_hash = canonical_hash([workspace, status, q.casefold(), sort, direction])
        offset = 0
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                offset = data["offset"]
                if data["query"] != query_hash or type(offset) is not int or not 0 <= offset <= 20:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise OpsError(422, "This page cursor does not match the selected filters.") from None
        needle = q.casefold().strip()
        rows = [case for case in self.store.cases(pk)
                if (workspace == "all" or case["facility_id"] == workspace)
                and (not status or case["status"] == status)
                and (not needle or needle in " ".join([case["id"], case["packet_id"], case["packet_title"]]).casefold())]
        rows.sort(key=lambda c: (c[sort] is not None, c[sort] if c[sort] is not None else 0, c["id"]),
                  reverse=direction == "desc")
        page = rows[offset:offset + limit]
        next_cursor = (base64.urlsafe_b64encode(json.dumps({"offset": offset + limit, "query": query_hash}).encode()).decode()
                       if offset + limit < len(rows) else None)
        return {"items": [summary(case) for case in page], "total": len(rows),
                "next_cursor": next_cursor, "as_of": utc_now()}

    @staticmethod
    def workspace(workspace: str):
        if workspace not in {"all", *(f["id"] for f in FACILITIES)}:
            raise OpsError(422, "Choose an available synthetic workspace.")

    def overview(self, pk: str, workspace="all") -> dict:
        self.workspace(workspace)
        cases = [c for c in self.store.cases(pk) if workspace == "all" or c["facility_id"] == workspace]
        counts = {status: sum(c["status"] == status for c in cases) for status in STATUSES}
        counts["total"] = len(cases)
        return {"counts": counts,
                "origin_counts": {origin: sum(c.get("origin", "unknown") == origin for c in cases)
                                  for origin in ("prepared_demo", "live_run", "unknown")},
                "by_facility": [{"facility_id": f["id"], "name": f["name"], "city": f["city"],
                                 "count": sum(c["facility_id"] == f["id"] for c in cases)}
                                for f in FACILITIES if workspace == "all" or workspace == f["id"]],
                "outcomes": [{"label": status, "value": counts[status]} for status in STATUSES],
                "durations": [{"id": c["id"], "label": c["packet_title"], "duration_ms": c["duration_ms"]}
                              for c in sorted(cases, key=lambda c: c["created_at"])
                              if c.get("origin") == "live_run" and c["duration_ms"] is not None],
                "as_of": utc_now(), "scope": "Current synthetic session"}

    def retry(self, pk: str, session: dict, case_id: str, expected_version: int,
              idempotency_key: str) -> tuple[dict, bool, str]:
        self.require(session, {"operator", "admin"})
        case = self.get_case(pk, case_id)
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        prior = case["retry_requests"].get(key)
        if prior:
            if prior["expected_version"] != expected_version:
                raise OpsError(409, "This retry key already identifies a different request.")
            return case, False, f"{case_id}:{prior['generation']}"
        if case["version"] != expected_version:
            raise OpsError(409, "This case changed. Refresh before retrying.")
        if case["status"] not in {"failed", "rejected"}:
            raise OpsError(409, "Only failed or rejected cases can be retried.")
        if case["generation"] >= 3:
            raise OpsError(409, "This case has reached its limit of three processing generations.")
        if case.get("origin") == "prepared_demo":
            case.setdefault("prepared_history", []).append(copy.deepcopy({
                key: case[key] for key in ["generation", "status", "source_hash", "artifact_hash",
                                           "execution_id", "result", "review", "origin", "prepared"]}))
        case.update(status="queued", version=case["version"] + 1, generation=case["generation"] + 1,
                    updated_at=utc_now(), duration_ms=None, correction_count=0, artifact_hash=None,
                    patient_url=None, result=None, error=None, review=None, lease_owner=None,
                    lease_until=0, execution_id=None, origin="live_run")
        case["retry_requests"][key] = {"generation": case["generation"], "expected_version": expected_version}
        case["events"].append(event("retried", "A new processing generation was committed with its outbox entry."))
        try:
            self.store.replace(pk, case, expected_version, outbox=outbox_for(case))
        except (Conflict, DailyLimit) as exc:
            latest = self.get_case(pk, case_id)
            if key in latest["retry_requests"]:
                return latest, False, f"{case_id}:{latest['retry_requests'][key]['generation']}"
            if isinstance(exc, DailyLimit):
                raise OpsError(429, "Daily synthetic job allowance reached; resets at 00:00 UTC.") from None
            raise OpsError(409, "This case changed. Refresh before retrying.") from None
        return case, True, f"{case_id}:{case['generation']}"

    def claim(self, pk: str, case_id: str, generation: int) -> dict | None:
        case = self.store.get(pk, "CASE#" + case_id)
        if not case or case["generation"] != generation or case["status"] not in {"queued", "processing"}:
            return None
        if case["status"] == "processing" and case["lease_until"] > int(time.time()):
            raise LeaseBusy()
        expected = case["version"]
        case.update(status="processing", version=expected + 1, updated_at=utc_now(),
                    lease_owner=uuid4().hex, lease_until=int(time.time()) + 210,
                    execution_id=f"{case_id}:{generation}")
        case["events"].append(event("processing", "A worker claimed this generation for Strands Graph execution."))
        try:
            self.store.replace(pk, case, expected)
        except Conflict:
            raise LeaseBusy() from None
        return case

    def complete(self, pk: str, claimed: dict, result: dict | None, duration_ms: int) -> bool:
        from medplain.ops_validation import validate_case_evidence

        current = self.store.get(pk, "CASE#" + claimed["id"])
        if (not current or current["status"] != "processing" or current["generation"] != claimed["generation"]
                or current["lease_owner"] != claimed["lease_owner"]):
            return False
        artifact_hash = None
        error = "The agents could not complete verification. No patient explanation was released."
        if result is not None:
            try:
                artifact_hash = validate_case_evidence({**current, "result": result})
                if len(json.dumps({"source": current["source"], "result": result}, ensure_ascii=False).encode()) > 300000:
                    raise ValueError("Evidence exceeds bounded synthetic storage size")
            except (ValueError, KeyError, TypeError):
                result = None
                artifact_hash = None
        expected = current["version"]
        current.update(version=expected + 1, updated_at=utc_now(), duration_ms=max(0, int(duration_ms)),
                       lease_owner=None, lease_until=0, result=result, artifact_hash=artifact_hash,
                       correction_count=result.get("correction_count", 0) if result else 0)
        share = None
        if artifact_hash is None:
            current.update(status="failed", error=error)
            current["events"].append(event("failed", error))
        else:
            dossier = current.get("source_profile") == "medplain-reconciliation-v1"
            for receipt in result["receipts"]:
                current["events"].append({"id": uuid4().hex, "type": "gate_" + receipt["verdict"],
                                          "at": receipt["timestamp"],
                                          "message": ("The source-citation gate withheld a changed clarification card."
                                                      if dossier and receipt["verdict"] == "refused" else
                                                      "The source-citation gate verified clarification-card integrity; no treatment regimen was authorized."
                                                      if dossier else
                                                      "The clinical fidelity gate withheld a changed candidate."
                                                      if receipt["verdict"] == "refused" else
                                                      "The clinical fidelity gate verified the exact patient artifact.")})
            needs_review = bool(dossier or current.get("intake") or result.get("interactions")
                                or current["correction_count"] > 0)
            current.update(status="review_required" if needs_review else "released", error=None)
            current["events"].append(event(current["status"],
                                           "Verified artifact awaits synthetic reviewer release."
                                           if needs_review else "Verified clean synthetic artifact was automatically released."))
            if not needs_review:
                share = self.make_share(current)
        try:
            self.store.replace(pk, current, expected, share=share)
        except Conflict:
            return False
        return True

    def review(self, pk: str, session: dict, case_id: str, decision: str, expected_version: int,
               source_hash: str, artifact_hash: str, comment: str) -> dict:
        from medplain.ops_validation import validate_case_evidence

        self.require(session, {"reviewer", "admin"})
        case = self.get_case(pk, case_id)
        if (case["version"] != expected_version or case["status"] != "review_required"
                or case["source_hash"] != source_hash or case["artifact_hash"] != artifact_hash):
            raise OpsError(409, "Review requires the current version and exact verified source and artifact hashes.")
        if decision not in {"release", "reject"}:
            raise OpsError(422, "Choose release or reject.")
        try:
            if validate_case_evidence(case) != artifact_hash:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise OpsError(409, "The independent publication check failed. This artifact cannot be released.") from None
        case.update(version=case["version"] + 1, updated_at=utc_now(),
                    status="released" if decision == "release" else "rejected")
        case["review"] = {"decision": decision, "at": utc_now(), "role": session["role"],
                          "comment": comment, "source_hash": source_hash, "artifact_hash": artifact_hash,
                          "reviewed_version": expected_version, "demo": True}
        case["events"].append(event(case["status"], "Synthetic reviewer " + decision + " decision recorded for the exact artifact."))
        share = self.make_share(case) if decision == "release" else None
        try:
            self.store.replace(pk, case, expected_version, share=share)
        except Conflict:
            raise OpsError(409, "This case changed. Refresh before reviewing.") from None
        return case

    @staticmethod
    def make_share(case: dict) -> dict:
        share_id = secrets.token_urlsafe(32)
        case["patient_url"] = "/console/patient/" + share_id
        return {"id": share_id, "ttl": case["ttl"], "execution_id": case["execution_id"],
                "case": {**{key: case[key] for key in ["id", "packet_title", "facility_id", "status", "generation",
                                                      "source_hash", "artifact_hash", "updated_at"]},
                         "origin": case.get("origin", "unknown"), "prepared": case.get("prepared"),
                         "source_profile": case.get("source_profile"),
                         "artifact_kind": "clarification_plan" if case.get("source_profile") == "medplain-reconciliation-v1"
                         else "patient_explanation"},
                "source": case["source"], "result": case["result"], "intake": case.get("intake"),
                "source_profile": case.get("source_profile")}

    def patient(self, share_id: str) -> dict:
        from medplain.ops_validation import validate_case_evidence

        if not 32 <= len(share_id) <= 128:
            raise OpsError(404, "This released synthetic explanation is unavailable or expired.")
        share = self.store.get("SHARE#" + share_id, "VIEW")
        if not share:
            raise OpsError(404, "This released synthetic explanation is unavailable or expired.")
        try:
            if (share["case"]["status"] != "released"
                    or validate_case_evidence({**share, "source_hash": share["case"]["source_hash"]}) != share["case"]["artifact_hash"]):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise OpsError(409, "The released artifact did not pass its independent integrity check.") from None
        # The receipt's session_id is a graph execution correlation ID, not the
        # anonymous API session. No API token, owner key or role is published.
        dossier = share.get("source_profile") == "medplain-reconciliation-v1"
        keys = (["status", "source_profile", "source_hash", "artifact_kind", "artifact", "artifact_hash",
                 "patient_regimen", "explanation_text", "trace", "receipts", "correction_count", "model_mode"]
                if dossier else ["status", "explanation", "explanation_text", "trace", "receipts",
                                 "interactions", "correction_count", "model_mode"])
        result = {key: share["result"][key] for key in keys}
        return {"case": share["case"], "source": share["source"], "result": result,
                "receipts": result["receipts"],
                "source_profile": share.get("source_profile"),
                "scope": ("Reviewed source-cited clarification card. No medication regimen is authorized."
                          if dossier else "Synthetic demonstration of source fidelity, not clinical appropriateness.")}
