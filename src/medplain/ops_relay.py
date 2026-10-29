"""Committed DynamoDB outbox to SQS. Send then mark permits safe duplicates."""

import json
import logging
import os

from medplain.ops_store import get_store
from medplain.ops_worker import validate_message

logger = logging.getLogger(__name__)


def relay_record(record: dict, store=None, sqs=None) -> bool:
    if record.get("eventName") != "INSERT":
        return False
    image = record.get("dynamodb", {}).get("NewImage", {})
    if image.get("entity", {}).get("S") != "OUTBOX":
        return False
    pk = image["PK"]["S"]
    sk = image["SK"]["S"]
    store = store if store is not None else get_store()
    outbox = store.get(pk, sk)
    if not outbox or outbox.get("sent_at") is not None:
        return False
    message = validate_message({"session_key": pk, "case_id": outbox["case_id"], "generation": outbox["generation"]})
    if sk != f"OUTBOX#{message['case_id']}#{message['generation']}":
        raise ValueError("Outbox identity mismatch")
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
            logger.error("Committed synthetic outbox item awaits retry")
            failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
    return {"batchItemFailures": failures}
