"""Synthetic case lifecycle with owner-scoped capabilities and fenced execution."""

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from medplain.clinical import check_interactions, parse_packet
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
    if os.getenv("MEDPLAIN_COMMIT"):
        return os.environ["MEDPLAIN_COMMIT"]
    build = Path(__file__).parent / "_build.json"
    return json.loads(build.read_text())["commit"] if build.exists() else "development"


def event(kind: str, message: str) -> dict:
    return {"id": uuid4().hex, "type": kind, "at": utc_now(), "message": message}


def summary(case: dict) -> dict:
    return {key: case[key] for key in SUMMARY_KEYS}


def detail(case: dict) -> dict:
    return {**summary(case), "source": case["source"], "result": case["result"],
            "events": case["events"], "review": case["review"]}


def outbox_for(case: dict) -> dict:
    return {"sk": f"OUTBOX#{case['id']}#{case['generation']}", "case_id": case["id"],
            "generation": case["generation"], "ttl": case["ttl"], "sent_at": None}


def _key(token: str) -> str:
    return "SESSION#" + hashlib.sha256(token.encode()).hexdigest()


class OpsService:
    def __init__(self, store):
        self.store = store

    def new_session(self) -> dict:
        token = secrets.token_urlsafe(32)
        ttl = int(time.time()) + 86400
        session = {"session_id": uuid4().hex, "role": "operator", "roles": ROLES.copy(),
                   "expires_at": datetime.fromtimestamp(ttl, timezone.utc).isoformat(),
                   "demo": True, "ttl": ttl, "case_count": 0}
        self.store.put_new(_key(token), "SESSION", session)
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

    def bootstrap(self, session: dict) -> dict:
        return {"session": {key: session[key] for key in ["session_id", "role", "roles", "demo"]},
                "workspaces": [{"id": "all", "name": "All synthetic desks"}]
                + [{"id": f["id"], "name": f["name"]} for f in FACILITIES],
                "facilities": FACILITIES,
                "packets": [{"id": p.packet_id, "title": p.title, "summary": p.subtitle,
                             "stress_available": p.adversarial} for p in get_packets()],
                "runtime": {"model_mode": os.getenv("MEDPLAIN_MODEL_MODE", "fixture"),
                            "transport_mode": self.store.transport_mode,
                            "commit": commit(), "storage": self.store.storage},
                "limits": {"max_cases": 20, "daily_jobs": daily_allowance()[1], "daily_reset": "00:00 UTC"}}

    def get_case(self, pk: str, case_id: str) -> dict:
        case = self.store.get(pk, "CASE#" + case_id)
        if case is None:
            raise OpsError(404, "This case is unavailable in your synthetic session.")
        return case

    def create(self, pk: str, session: dict, packet_id: str, facility_id: str,
               stress: bool, idempotency_key: str) -> tuple[dict, bool, str]:
        self.require(session, {"operator", "admin"})
        if facility_id not in {f["id"] for f in FACILITIES}:
            raise OpsError(422, "Choose one of the synthetic discharge desks.")
        try:
            packet = parse_packet(get_packet(packet_id))
        except KeyError:
            raise OpsError(404, "Choose one of the synthetic sample packets.") from None
        if stress and not packet.adversarial:
            raise OpsError(422, "The deliberate mutation is available on the stress packet only.")
        fingerprint = canonical_hash({"packet_id": packet_id, "facility_id": facility_id, "stress": stress})
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
                "lease_until": 0, "execution_id": None, "retry_requests": {}}
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
                "outcomes": [{"label": status, "value": counts[status]} for status in STATUSES],
                "durations": [{"id": c["id"], "label": c["packet_title"], "duration_ms": c["duration_ms"]}
                              for c in sorted(cases, key=lambda c: c["created_at"]) if c["duration_ms"] is not None],
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
        case.update(status="queued", version=case["version"] + 1, generation=case["generation"] + 1,
                    updated_at=utc_now(), duration_ms=None, correction_count=0, artifact_hash=None,
                    patient_url=None, result=None, error=None, review=None, lease_owner=None,
                    lease_until=0, execution_id=None)
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
        from medplain.ops_validation import validate_result

        current = self.store.get(pk, "CASE#" + claimed["id"])
        if (not current or current["status"] != "processing" or current["generation"] != claimed["generation"]
                or current["lease_owner"] != claimed["lease_owner"]):
            return False
        artifact_hash = None
        error = "The agents could not complete verification. No patient explanation was released."
        if result is not None:
            try:
                packet = ClinicalPacket.model_validate(current["source"])
                if canonical_hash(packet.model_dump()) != current["source_hash"]:
                    raise ValueError("Source snapshot changed")
                artifact_hash = validate_result(packet, result, current["execution_id"])
                if result.get("interactions") != check_interactions(packet):
                    raise ValueError("Interaction evidence changed")
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
            for receipt in result["receipts"]:
                current["events"].append({"id": uuid4().hex, "type": "gate_" + receipt["verdict"],
                                          "at": receipt["timestamp"],
                                          "message": ("The clinical fidelity gate withheld a changed candidate."
                                                      if receipt["verdict"] == "refused" else
                                                      "The clinical fidelity gate verified the exact patient artifact.")})
            needs_review = bool(check_interactions(packet) or current["correction_count"] > 0)
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
        from medplain.ops_validation import validate_result

        self.require(session, {"reviewer", "admin"})
        case = self.get_case(pk, case_id)
        if (case["version"] != expected_version or case["status"] != "review_required"
                or case["source_hash"] != source_hash or case["artifact_hash"] != artifact_hash):
            raise OpsError(409, "Review requires the current version and exact verified source and artifact hashes.")
        if decision not in {"release", "reject"}:
            raise OpsError(422, "Choose release or reject.")
        try:
            packet = ClinicalPacket.model_validate(case["source"])
            if (canonical_hash(packet.model_dump()) != source_hash
                    or validate_result(packet, case["result"], case["execution_id"]) != artifact_hash
                    or case["result"].get("interactions") != check_interactions(packet)):
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
                "case": {key: case[key] for key in ["id", "packet_title", "facility_id", "status", "generation",
                                                    "source_hash", "artifact_hash", "updated_at"]},
                "source": case["source"], "result": case["result"]}

    def patient(self, share_id: str) -> dict:
        from medplain.ops_validation import validate_result

        if not 32 <= len(share_id) <= 128:
            raise OpsError(404, "This released synthetic explanation is unavailable or expired.")
        share = self.store.get("SHARE#" + share_id, "VIEW")
        if not share:
            raise OpsError(404, "This released synthetic explanation is unavailable or expired.")
        try:
            packet = ClinicalPacket.model_validate(share["source"])
            if (share["case"]["status"] != "released"
                    or canonical_hash(packet.model_dump()) != share["case"]["source_hash"]
                    or validate_result(packet, share["result"], share["execution_id"]) != share["case"]["artifact_hash"]
                    or share["result"].get("interactions") != check_interactions(packet)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise OpsError(409, "The released artifact did not pass its independent integrity check.") from None
        # The receipt's session_id is a graph execution correlation ID, not the
        # anonymous API session. No API token, owner key or role is published.
        result = {key: share["result"][key] for key in ["status", "explanation", "explanation_text", "trace",
                                                       "receipts", "interactions", "correction_count", "model_mode"]}
        return {"case": share["case"], "source": share["source"], "result": result,
                "receipts": result["receipts"],
                "scope": "Synthetic demonstration of source fidelity, not clinical appropriateness."}
