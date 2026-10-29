"""Exercise durable jobs, session isolation, hash-bound review and public release.

This instrument uses only the public HTTP contract. It never reads deployment
credentials, writes patient data or treats a successful dispatch as completion.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from live_smoke import canonical, verify_chain


class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.token: str | None = None

    def call(self, path: str, payload: dict | None = None, expected: int = 200):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["X-Medplain-Session"] = self.token
        req = Request(self.url + path, data=canonical(payload) if payload is not None else None, headers=headers)
        try:
            with urlopen(req, timeout=35) as response:
                status, raw = response.status, response.read()
                value = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
        except HTTPError as exc:
            status = exc.code
            value = json.loads(exc.read())
        if status != expected:
            # Do not include a provider result, a session token or an unchecked artifact.
            raise AssertionError(f"{path.split('?')[0]} expected HTTP {expected}, received {status}")
        return value

    def session(self):
        session = self.call("/api/ops/session", {}, expected=201)
        self.token = session["session_token"]
        if session["demo"] is not True or session["role"] != "operator":
            raise AssertionError("new session must be an explicitly synthetic operator lens")
        return session


def wait_case(client: Client, case_id: str, max_seconds: int = 240) -> dict:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        case = client.call(f"/api/ops/cases/{case_id}")
        if case["status"] not in {"queued", "processing"}:
            return case
        time.sleep(2)
    raise AssertionError("persisted job did not reach a terminal processing state within its bound")


def validate_artifact(case: dict):
    result = case["result"]
    if not result or result["status"] != "verified":
        raise AssertionError("case has no verified result")
    receipts = result["receipts"]
    if not verify_chain(receipts) or receipts[-1]["verdict"] != "verified":
        raise AssertionError("persisted receipt chain is invalid")
    source_hash = hashlib.sha256(canonical(case["source"])).hexdigest()
    artifact_hash = hashlib.sha256(canonical({
        "structured": result["explanation"], "text": result["explanation_text"],
    })).hexdigest()
    if (case["source_hash"] != source_hash or case["artifact_hash"] != artifact_hash
            or any(r["source_hash"] != source_hash or r["session_id"] != f"{case['id']}:{case['generation']}"
                   for r in receipts)
            or receipts[-1]["artifact_hash"] != artifact_hash):
        raise AssertionError("saved source, job generation and patient artifact are not bound together")
    return result


def smoke(url: str, expected_commit: str | None, allow_fixture: bool = False) -> dict:
    client = Client(url)
    root = client.call("/")
    if not isinstance(root, bytes) or b"Atropos" not in root:
        raise AssertionError("judge URL must return the Atropos console with HTTP 200")
    health = client.call("/api/health")
    if expected_commit and health["commit"] != expected_commit:
        raise AssertionError("deployed console commit differs from submitted source")
    if health.get("surface") != "operations-console":
        raise AssertionError("built operations console is missing from the deployment")
    if not allow_fixture and health.get("model_mode") != "bedrock":
        raise AssertionError("live operations must use actual Bedrock inference")
    client.session()
    boot = client.call("/api/ops/bootstrap")
    if len(boot["facilities"]) != 3 or not all(item["synthetic"] for item in boot["facilities"]):
        raise AssertionError("geographic facilities must be explicit synthetic demonstration points")
    if not allow_fixture and boot["runtime"]["transport_mode"] != "DynamoDB transactional outbox / Streams / SQS":
        raise AssertionError("production operations are not using the deployed queue path")
    another = Client(url)
    another.session()
    packets = boot["packets"]
    if {p["id"] for p in packets} != {
        "home-after-pneumonia", "heart-medicine-review", "dosage-mutation-stress",
    }:
        raise AssertionError("three required packet journeys must be reachable")
    proof = []
    for index, packet in enumerate(packets):
        client.call("/api/ops/session/role", {"role": "operator"})
        payload = {
            "packet_id": packet["id"], "facility_id": boot["facilities"][index]["id"],
            "stress": bool(packet["stress_available"]), "idempotency_key": uuid4().hex,
        }
        created = client.call("/api/ops/cases", payload, expected=202)
        case_id = created["case"]["id"]
        duplicate = client.call("/api/ops/cases", payload, expected=202)
        if duplicate["created"] is not False or duplicate["case"]["id"] != case_id:
            raise AssertionError("command idempotency created a duplicate case")
        another.call(f"/api/ops/cases/{case_id}", expected=404)
        case = wait_case(client, case_id)
        if case["status"] not in {"review_required", "released"}:
            raise AssertionError(f"{packet['id']}: durable processing failed instead of producing verified evidence")
        result = validate_artifact(case)
        if not allow_fixture and result["model_mode"] != "bedrock":
            raise AssertionError("worker used an inference fallback")
        if payload["stress"]:
            if result["correction_count"] != 1 or [r["verdict"] for r in result["receipts"]] != ["refused", "verified"]:
                raise AssertionError("stress case did not persist the refusal and automatic correction")
            if result["explanation"]["medications"][0]["dose"] != "2.5":
                raise AssertionError("corrected dose differs from the captured clinical source")
        if case["status"] == "review_required":
            if case.get("patient_url"):
                raise AssertionError("review-required case exposed a public patient capability")
            decision = {
                "decision": "release", "expected_version": case["version"],
                "source_hash": case["source_hash"], "artifact_hash": case["artifact_hash"],
                "comment": "Synthetic release drill: source and receipt inspected.",
            }
            client.call(f"/api/ops/cases/{case_id}/review", decision, expected=403)
            client.call("/api/ops/session/role", {"role": "reviewer"})
            mutated = copy.deepcopy(decision)
            mutated["artifact_hash"] = "a" * 64
            client.call(f"/api/ops/cases/{case_id}/review", mutated, expected=409)
            stale = copy.deepcopy(decision)
            stale["expected_version"] -= 1
            client.call(f"/api/ops/cases/{case_id}/review", stale, expected=409)
            client.call(f"/api/ops/cases/{case_id}/review", decision)
            case = client.call(f"/api/ops/cases/{case_id}")
        if case["status"] != "released" or not case.get("patient_url"):
            raise AssertionError("authorized review did not produce a public patient view")
        share_id = case["patient_url"].rstrip("/").split("/")[-1]
        public = Client(url).call("/api/ops/patient/" + share_id)
        if public["case"].get("artifact_hash") != case["artifact_hash"]:
            raise AssertionError("public release did not retain the approved artifact")
        validate_artifact({**public["case"], "source": public["source"], "result": public["result"]})
        if "session_token" in public or "PK" in public or "lease_owner" in public:
            raise AssertionError("public release leaked internal session or worker state")
        proof.append({
            "packet_id": packet["id"], "case_id": case_id, "status": case["status"],
            "generation": case["generation"], "duration_ms": case["duration_ms"],
            "corrections": case["correction_count"], "source_hash": case["source_hash"],
            "artifact_hash": case["artifact_hash"], "final_receipt_hash": result["receipts"][-1]["hash"],
            "patient_url": case["patient_url"],
        })
        print(f"Verified durable journey: {packet['id']} -> released", flush=True)
    overview = client.call("/api/ops/overview?workspace=all")
    if overview["counts"]["total"] != 3 or overview["counts"]["released"] != 3:
        raise AssertionError("overview does not report the actual persisted session cohort")
    if another.call("/api/ops/overview?workspace=all")["counts"]["total"] != 0:
        raise AssertionError("another anonymous session can observe the first session's cohort")
    page = client.call("/api/ops/cases?workspace=all&limit=1&sort=created_at&direction=asc")
    if page["total"] != 3 or len(page["items"]) != 1 or not page["next_cursor"]:
        raise AssertionError("server pagination did not preserve the full cohort size")
    second = client.call("/api/ops/cases?workspace=all&limit=1&sort=created_at&direction=asc&cursor=" + page["next_cursor"])
    if second["items"][0]["id"] == page["items"][0]["id"]:
        raise AssertionError("server pagination repeated the previous case")
    return {
        "url": url, "http_status": 200, "commit": health["commit"],
        "model_mode": health["model_mode"], "runtime": boot["runtime"],
        "isolation": "cross-session read rejected", "review": "role, hash and version negative checks rejected",
        "cases": proof,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--expected-commit")
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    proof = smoke(args.url, args.expected_commit, args.allow_fixture)
    rendered = json.dumps(proof, indent=2)
    if args.output:
        from pathlib import Path
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
