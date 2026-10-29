"""Verify the anonymous AWS judge journey, commit, refusals and receipt integrity."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def verify_chain(receipts: list[dict]) -> bool:
    previous = "0" * 64
    if not receipts:
        return False
    session_id = receipts[0].get("session_id")
    source_hash = receipts[0].get("source_hash")
    for sequence, receipt in enumerate(receipts, start=1):
        data = {key: value for key, value in receipt.items() if key != "hash"}
        if (
            data.get("sequence") != sequence
            or data.get("session_id") != session_id
            or data.get("source_hash") != source_hash
            or data.get("previous_hash") != previous
            or receipt.get("hash") != hashlib.sha256(canonical(data)).hexdigest()
        ):
            return False
        previous = receipt["hash"]
    return True


def fetch(base_url: str, path: str, payload: dict | None = None) -> object:
    body = canonical(payload) if payload is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise AssertionError(f"{path}: expected HTTP 200, received {response.status}")
        data = response.read()
        return json.loads(data) if "application/json" in response.headers.get("Content-Type", "") else data


def smoke(base_url: str, expected_commit: str | None, allow_fixture: bool = False) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("judge URL must be an absolute HTTP(S) URL")
    root = fetch(base_url, "/")
    if not isinstance(root, bytes) or b"Atropos" not in root:
        raise AssertionError("root does not serve the Atropos web surface")
    health = fetch(base_url, "/api/health")
    if expected_commit and health.get("commit") != expected_commit:
        raise AssertionError(f"deployed commit mismatch: {health.get('commit')!r}")
    if not allow_fixture and health.get("model_mode") != "bedrock":
        raise AssertionError("public release must execute the Bedrock adapter")
    packets = fetch(base_url, "/api/packets")["packets"]
    if len(packets) != 3:
        raise AssertionError("the judge path must offer all three synthetic packets")
    results = []
    for packet in packets:
        packet_id = packet["packet_id"]
        source = fetch(base_url, f"/api/packets/{packet_id}")
        stress = bool(source.get("adversarial"))
        result = fetch(base_url, "/api/explain", {"packet_id": packet_id, "stress": stress})
        if result.get("status") != "verified" or not result.get("explanation"):
            raise AssertionError(f"{packet_id}: no verified explanation published")
        receipts = result.get("receipts", [])
        if not verify_chain(receipts) or receipts[-1].get("verdict") != "verified":
            raise AssertionError(f"{packet_id}: invalid or incomplete receipt chain")
        check = fetch(base_url, "/api/receipts/verify", {"receipts": receipts})
        if check.get("valid") is not True:
            raise AssertionError(f"{packet_id}: server rejected its own chain")
        tampered = copy.deepcopy(receipts)
        tampered[-1]["verdict"] = "refused"
        if fetch(base_url, "/api/receipts/verify", {"receipts": tampered}).get("valid") is not False:
            raise AssertionError("receipt verifier accepted deliberately modified evidence")
        if stress:
            if result.get("correction_count", 0) < 1 or not any(r.get("verdict") == "refused" for r in receipts):
                raise AssertionError("adversarial packet did not execute refusal and self-correction")
            if not any(event.get("status") == "refused" for event in result.get("trace", [])):
                raise AssertionError("adversarial refusal is missing from the visible trace")
        results.append({"packet_id": packet_id, "status": result["status"], "corrections": result["correction_count"]})
    return {"url": base_url, "http_status": 200, "commit": health.get("commit"), "model_mode": health.get("model_mode"), "packets": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--expected-commit")
    parser.add_argument("--allow-fixture", action="store_true")
    args = parser.parse_args()
    print(json.dumps(smoke(args.url, args.expected_commit, args.allow_fixture), indent=2))
