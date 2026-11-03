"""V3 outbox facade. Legacy records retain the original relay contract."""

import json
import logging
import os

from medplain.careflow_worker import validate_teachback_message
from medplain.ops_relay import relay_record as relay_legacy
from medplain.ops_store import get_store

logger = logging.getLogger(__name__)


def relay_record(record: dict, store=None, sqs=None) -> bool:
    if record.get("eventName") != "INSERT":
        return False
    image = record.get("dynamodb", {}).get("NewImage", {})
    if image.get("entity", {}).get("S") != "OUTBOX":
        return False
    sk = image.get("SK", {}).get("S", "")
    if not sk.startswith("OUTBOX#CARE#"):
        return relay_legacy(record, store, sqs)
    pk = image["PK"]["S"]
    store = store if store is not None else get_store()
    outbox = store.get(pk, sk)
    if not outbox or outbox.get("sent_at") is not None:
        return False
    message = validate_teachback_message({
        "session_key": pk, "case_id": outbox["case_id"], "generation": outbox["generation"],
        "job_kind": outbox["job_kind"], "attempt_id": outbox["attempt_id"],
    })
    if sk != f"OUTBOX#CARE#{message['case_id']}#{message['attempt_id']}":
        raise ValueError("Careflow outbox identity mismatch")
    if sqs is None:
        import boto3

        sqs = boto3.client("sqs")
    sqs.send_message(QueueUrl=os.environ["MEDPLAIN_OPS_QUEUE_URL"],
                     MessageBody=json.dumps(message, separators=(",", ":")))
    store.mark_sent(pk, sk)
    return True


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            relay_record(record)
        except Exception:
            logger.error("Careflow committed outbox awaits retry")
            failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
    return {"batchItemFailures": failures}
