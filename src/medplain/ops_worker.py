"""SQS worker: real Strands execution, leased attempts and terminal CAS fencing."""

import json
import logging
import os
import re
import time

from medplain.gate import canonical_hash
from medplain.ops_domain import OpsService, commit
from medplain.ops_store import get_store
from medplain.schemas import ClinicalPacket

logger = logging.getLogger(__name__)
SESSION_KEY = re.compile(r"SESSION#[0-9a-f]{64}")
CASE_ID = re.compile(r"[0-9a-f]{32}")


def validate_message(message: dict) -> dict:
    if (not isinstance(message, dict) or set(message) != {"session_key", "case_id", "generation"}
            or not isinstance(message["session_key"], str) or not SESSION_KEY.fullmatch(message["session_key"])
            or not isinstance(message["case_id"], str) or not CASE_ID.fullmatch(message["case_id"])
            or type(message["generation"]) is not int or not 1 <= message["generation"] <= 3):
        raise ValueError("Unsupported synthetic job selector")
    return message


def process_message(message: dict, store=None) -> bool:
    message = validate_message(message)
    svc = OpsService(store if store is not None else get_store())
    pk = message["session_key"]
    claimed = svc.claim(pk, message["case_id"], message["generation"])
    if claimed is None:
        return False
    started = time.monotonic()
    result = None
    try:
        mode = os.getenv("MEDPLAIN_MODEL_MODE", "fixture")
        if mode not in {"fixture", "bedrock"}:
            raise ValueError("No configured agent runtime")
        if claimed.get("source_profile") == "medplain-reconciliation-v1":
            from medplain.reconciliation import run_reconciliation, validate_dossier

            dossier = validate_dossier(claimed["source"])
            if canonical_hash(dossier) != claimed["source_hash"] or claimed.get("intake") is not None:
                raise ValueError("Source dossier snapshot changed")
            result = run_reconciliation(dossier, claimed["execution_id"], mode=mode, stress=claimed["stress"])
        else:
            if claimed.get("source_profile") is not None:
                raise ValueError("No configured source profile")
            from medplain.engine import run_packet

            packet = ClinicalPacket.model_validate(claimed["source"])
            if canonical_hash(packet.model_dump()) != claimed["source_hash"]:
                raise ValueError("Source snapshot changed")
            kwargs = {"model_mode": mode, "stress": claimed["stress"]}
            if claimed.get("intake") is not None:
                kwargs["intake_source"] = claimed["intake"]
            result = run_packet(packet, claimed["execution_id"], **kwargs)
        result["commit"] = commit()
    except Exception:
        # Never log a provider trace, clinical input, model text or session key.
        logger.error("Synthetic job failed before independent publication verification")
    elapsed = int((time.monotonic() - started) * 1000)
    completed = svc.complete(pk, claimed, result, elapsed)
    logger.info("Synthetic job attempt finished", extra={"case_id": claimed["id"],
                "generation": claimed["generation"], "duration_ms": elapsed, "committed": completed})
    return completed


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            process_message(json.loads(record["body"]))
        except Exception:
            # Includes active leases and store outages. Retry only this SQS item.
            logger.error("Synthetic queue item could not be committed")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
