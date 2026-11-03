"""V3 worker entry point, preserving the original case worker unchanged.

Only selectors cross SQS. A patient response and its assessment stay in the
owner-scoped case. Duplicate delivery is fenced by the careflow service lease.
"""

import json
import logging
import os
import re

from medplain.ops_store import get_store
from medplain.ops_worker import process_message as process_legacy

logger = logging.getLogger(__name__)
HEX_ID = re.compile(r"[0-9a-f]{32}")
SESSION = re.compile(r"SESSION#[0-9a-f]{64}")


def validate_teachback_message(message: dict) -> dict:
    if (not isinstance(message, dict)
            or set(message) != {"session_key", "case_id", "generation", "job_kind", "attempt_id"}
            or message.get("job_kind") != "careflow_teachback"
            or not isinstance(message.get("session_key"), str)
            or not SESSION.fullmatch(message["session_key"])
            or not isinstance(message.get("case_id"), str)
            or not HEX_ID.fullmatch(message["case_id"])
            or not isinstance(message.get("attempt_id"), str)
            or not HEX_ID.fullmatch(message["attempt_id"])
            or type(message.get("generation")) is not int
            or not 1 <= message["generation"] <= 3):
        raise ValueError("Unsupported careflow assessment selector")
    return message


def process_message(message: dict, store=None) -> bool:
    if not isinstance(message, dict) or message.get("job_kind") != "careflow_teachback":
        return process_legacy(message, store)
    message = validate_teachback_message(message)
    from medplain.careflow_domain import CareflowService
    from medplain.careflow_teachback import run_teachback_assessment

    svc = CareflowService(store if store is not None else get_store())
    claimed = svc.claim_teachback(message["session_key"], message["case_id"],
                                  message["attempt_id"], message["generation"])
    if claimed is None:
        return False
    assessment = None
    error = None
    try:
        mode = os.getenv("MEDPLAIN_CAREFLOW_MODEL_MODE", os.getenv("MEDPLAIN_MODEL_MODE", "fixture"))
        if mode not in {"fixture", "bedrock"}:
            raise ValueError("Unsupported careflow model mode")
        assessment = run_teachback_assessment(
            claimed["source"], claimed["result"], claimed["prompt"],
            claimed["response_text"], claimed["execution_id"], mode=mode,
        )
    except Exception:
        # Do not disclose the patient response, model output or provider error.
        logger.error("Careflow assessment withheld before independent validation")
        error = "assessment_unavailable"
    return svc.complete_teachback(message["session_key"], claimed, assessment, error=error)


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            process_message(json.loads(record["body"]))
        except Exception:
            logger.error("Careflow queue item awaits retry")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
