"""Scoped AWS resources for the durable MedPlain outbox and worker path.

This module prepares and executes a reviewable plan only when called by the
deployment coordinator. Every existing resource is checked before any mutation.
"""

from __future__ import annotations

import json
import re
import time


PROJECT = "AtroposMedPlain"
WORKER_TIMEOUT = 180
QUEUE_VISIBILITY = 1080
MAX_RECEIVES = 3
LOG_RETENTION_DAYS = 7


def operations_plan(account: str, region: str, function: str, model_id: str) -> dict:
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"atropos-medplain(?:-[a-z0-9-]+)?", function):
        raise ValueError("operations resources require a scoped MedPlain function and AWS account")
    if region != "us-east-1":
        raise ValueError("this deployment is pinned to the verified us-east-1 Bedrock region")
    if not re.fullmatch(r"amazon\.nova-[a-z0-9-]+-v[0-9]+:[0-9]+", model_id):
        raise ValueError("operations require an explicitly named Amazon Nova foundation model")
    table = function + "-ops"
    queue, dlq = function + "-jobs", function + "-jobs-dlq"
    worker, relay = function + "-worker", function + "-outbox-relay"
    return {
        "table": table, "table_arn": f"arn:aws:dynamodb:{region}:{account}:table/{table}",
        "table_keys": ["PK", "SK"], "ttl_attribute": "ttl", "stream_view": "NEW_IMAGE",
        "point_in_time_recovery": True, "table_encryption": "AWS managed KMS",
        "queue": queue, "queue_arn": f"arn:aws:sqs:{region}:{account}:{queue}",
        "dlq": dlq, "dlq_arn": f"arn:aws:sqs:{region}:{account}:{dlq}",
        "queue_encryption": "SSE-SQS", "queue_visibility_seconds": QUEUE_VISIBILITY,
        "max_receive_count": MAX_RECEIVES, "worker": worker, "relay": relay,
        "worker_role": worker + "-runtime", "relay_role": relay + "-runtime",
        "worker_timeout_seconds": WORKER_TIMEOUT, "worker_reserved_concurrency": 2,
        "relay_reserved_concurrency": 1, "worker_handler": "medplain.ops_worker.handler",
        "relay_handler": "medplain.ops_relay.handler", "batch_size": 1,
        "partial_batch_failures": True, "alarm_notifications": "unconfigured",
        "daily_job_limit": 120,
        "alarms": [worker + "-errors", dlq + "-visible", queue + "-oldest", relay + "-iterator-age"],
        "model_arn": f"arn:aws:bedrock:{region}::foundation-model/{model_id}",
    }


def require_project(tags: dict | list, resource: str) -> None:
    values = tags if isinstance(tags, dict) else {item["Key"]: item["Value"] for item in tags}
    if values.get("Project") != PROJECT:
        raise ValueError(f"existing resource lacks MedPlain ownership tag: {resource}")


def queue_lookup(args, name: str, cli):
    return cli(args, "sqs", "get-queue-url", "--queue-name", name,
               "--queue-owner-aws-account-id", args.account,
               missing=("NonExistentQueue", "QueueDoesNotExist"))


def preflight(args, plan: dict, cli) -> None:
    """Read-only, account-bound ownership checks for all resources we may update."""
    ops = plan["operations"]
    bucket = plan["staging_bucket"]
    exists = cli(args, "s3api", "head-bucket", "--bucket", bucket,
                 "--expected-bucket-owner", args.account, missing="(404)")
    if exists is not None:
        tags = cli(args, "s3api", "get-bucket-tagging", "--bucket", bucket,
                   "--expected-bucket-owner", args.account, missing="NoSuchTagSet")
        require_project((tags or {}).get("TagSet", []), bucket)
    for role_name in [args.function + "-runtime", ops["worker_role"], ops["relay_role"]]:
        role = cli(args, "iam", "get-role", "--role-name", role_name, missing="NoSuchEntity")
        if role:
            if role["Role"]["Arn"] != f"arn:aws:iam::{args.account}:role/{role_name}":
                raise ValueError("runtime role does not belong to the selected account and path")
            require_project(role["Role"].get("Tags", []), role_name)
    for function in [args.function, ops["worker"], ops["relay"]]:
        existing = cli(args, "lambda", "get-function", "--function-name", function, missing="ResourceNotFoundException")
        if existing:
            expected = f"arn:aws:lambda:{args.region}:{args.account}:function:{function}"
            if existing["Configuration"]["FunctionArn"] != expected:
                raise ValueError("function does not belong to the selected account and region")
            require_project(existing.get("Tags", {}), function)
        log_group = "/aws/lambda/" + function
        groups = cli(args, "logs", "describe-log-groups", "--log-group-name-prefix", log_group)
        if any(item["logGroupName"] == log_group for item in groups.get("logGroups", [])):
            tags = cli(args, "logs", "list-tags-log-group", "--log-group-name", log_group)
            require_project(tags.get("tags", {}), log_group)
    table = cli(args, "dynamodb", "describe-table", "--table-name", ops["table"], missing="ResourceNotFoundException")
    if table:
        if table["Table"]["TableArn"] != ops["table_arn"]:
            raise ValueError("table does not belong to the selected account and region")
        tags = cli(args, "dynamodb", "list-tags-of-resource", "--resource-arn", ops["table_arn"])
        require_project(tags.get("Tags", []), ops["table"])
        if table["Table"]["KeySchema"] != [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]:
            raise ValueError("existing operations table has a different key schema")
        ttl = cli(args, "dynamodb", "describe-time-to-live", "--table-name", ops["table"])["TimeToLiveDescription"]
        if ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"} and ttl.get("AttributeName") != "ttl":
            raise ValueError("existing table TTL differs from the domain's epoch ttl field")
    for name in [ops["queue"], ops["dlq"]]:
        found = queue_lookup(args, name, cli)
        if found:
            url = found["QueueUrl"]
            attributes = cli(args, "sqs", "get-queue-attributes", "--queue-url", url, "--attribute-names", "QueueArn")
            if attributes["Attributes"]["QueueArn"] != f"arn:aws:sqs:{args.region}:{args.account}:{name}":
                raise ValueError("queue does not belong to the selected account and region")
            require_project(cli(args, "sqs", "list-queue-tags", "--queue-url", url).get("Tags", {}), name)
    alarms = cli(args, "cloudwatch", "describe-alarms", "--alarm-names", *ops["alarms"])
    for alarm in alarms.get("MetricAlarms", []) + alarms.get("CompositeAlarms", []):
        tags = cli(args, "cloudwatch", "list-tags-for-resource", "--resource-arn", alarm["AlarmArn"])
        require_project(tags.get("Tags", []), alarm["AlarmName"])
    for function in [ops["worker"], ops["relay"]]:
        mappings = cli(args, "lambda", "list-event-source-mappings", "--function-name", function, missing="ResourceNotFoundException")
        for mapping in (mappings or {}).get("EventSourceMappings", []):
            source = mapping.get("EventSourceArn", "")
            if source == ops["queue_arn"] or source.startswith(ops["table_arn"] + "/stream/"):
                arn = f"arn:aws:lambda:{args.region}:{args.account}:event-source-mapping:{mapping['UUID']}"
                require_project(cli(args, "lambda", "list-tags", "--resource", arn).get("Tags", {}), arn)


def table_permissions(plan: dict) -> dict:
    return {"Effect": "Allow", "Action": [
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
    ], "Resource": plan["operations"]["table_arn"]}


def ensure_table(args, ops: dict, cli) -> str:
    table = cli(args, "dynamodb", "describe-table", "--table-name", ops["table"], missing="ResourceNotFoundException")
    stream = {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}
    if table is None:
        cli(args, "dynamodb", "create-table", "--table-name", ops["table"],
            "--attribute-definitions", "AttributeName=PK,AttributeType=S", "AttributeName=SK,AttributeType=S",
            "--key-schema", "AttributeName=PK,KeyType=HASH", "AttributeName=SK,KeyType=RANGE",
            "--billing-mode", "PAY_PER_REQUEST", "--stream-specification", json.dumps(stream),
            "--sse-specification", "Enabled=true,SSEType=KMS", "--tags", f"Key=Project,Value={PROJECT}")
        cli(args, "dynamodb", "wait", "table-exists", "--table-name", ops["table"])
    elif (table["Table"].get("StreamSpecification") != stream
          or table["Table"].get("SSEDescription", {}).get("SSEType") != "KMS"):
        cli(args, "dynamodb", "update-table", "--table-name", ops["table"],
            "--stream-specification", json.dumps(stream), "--sse-specification", "Enabled=true,SSEType=KMS")
        cli(args, "dynamodb", "wait", "table-exists", "--table-name", ops["table"])
    cli(args, "dynamodb", "update-continuous-backups", "--table-name", ops["table"],
        "--point-in-time-recovery-specification", "PointInTimeRecoveryEnabled=true")
    ttl = cli(args, "dynamodb", "describe-time-to-live", "--table-name", ops["table"])["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in {"ENABLED", "ENABLING"}:
        cli(args, "dynamodb", "update-time-to-live", "--table-name", ops["table"],
            "--time-to-live-specification", "Enabled=true,AttributeName=ttl")
    return cli(args, "dynamodb", "describe-table", "--table-name", ops["table"])["Table"]["LatestStreamArn"]


def ensure_queue(args, name: str, attributes: dict, cli) -> str:
    found = queue_lookup(args, name, cli)
    if found:
        url = found["QueueUrl"]
        cli(args, "sqs", "set-queue-attributes", "--queue-url", url, "--attributes", json.dumps(attributes))
        return url
    created = cli(args, "sqs", "create-queue", "--queue-name", name, "--attributes", json.dumps(attributes),
                  "--tags", f"Project={PROJECT}")["QueueUrl"]
    # SQS requires a short propagation delay before a newly created queue is used.
    time.sleep(1)
    return created


def log_policy(args, function: str) -> dict:
    group = f"arn:aws:logs:{args.region}:{args.account}:log-group:/aws/lambda/{function}"
    return {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": [group, group + ":*"]}


def ensure_role(args, name: str, statements: list[dict], cli) -> str:
    role = cli(args, "iam", "get-role", "--role-name", name, missing="NoSuchEntity")
    if role is None:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        role = cli(args, "iam", "create-role", "--role-name", name, "--assume-role-policy-document", json.dumps(trust), "--tags", f"Key=Project,Value={PROJECT}")
    else:
        require_project(role["Role"].get("Tags", []), name)
    cli(args, "iam", "put-role-policy", "--role-name", name, "--policy-name", "medplain-runtime",
        "--policy-document", json.dumps({"Version": "2012-10-17", "Statement": statements}))
    return role["Role"]["Arn"]


def ensure_function(args, plan: dict, function: str, handler: str, role_arn: str, environment: dict,
                    timeout: int, memory: int, concurrency: int, cli) -> None:
    existing = cli(args, "lambda", "get-function", "--function-name", function, missing="ResourceNotFoundException")
    env = json.dumps({"Variables": environment})
    if existing:
        require_project(existing.get("Tags", {}), function)
        cli(args, "lambda", "update-function-code", "--function-name", function,
            "--s3-bucket", plan["staging_bucket"], "--s3-key", plan["staging_key"])
        cli(args, "lambda", "wait", "function-updated", "--function-name", function)
        cli(args, "lambda", "update-function-configuration", "--function-name", function,
            "--role", role_arn, "--handler", handler, "--environment", env,
            "--timeout", str(timeout), "--memory-size", str(memory))
        cli(args, "lambda", "wait", "function-updated", "--function-name", function)
    else:
        for attempt in range(6):
            try:
                cli(args, "lambda", "create-function", "--function-name", function,
                    "--runtime", "python3.12", "--handler", handler, "--role", role_arn,
                    "--code", json.dumps({"S3Bucket": plan["staging_bucket"], "S3Key": plan["staging_key"]}),
                    "--timeout", str(timeout), "--memory-size", str(memory), "--environment", env,
                    "--tags", f"Project={PROJECT}")
                break
            except RuntimeError as exc:
                if "cannot be assumed" not in str(exc) or attempt == 5:
                    raise
                time.sleep(5)
    cli(args, "lambda", "wait", "function-active-v2", "--function-name", function)
    cli(args, "lambda", "put-function-concurrency", "--function-name", function, "--reserved-concurrent-executions", str(concurrency))
    group = "/aws/lambda/" + function
    groups = cli(args, "logs", "describe-log-groups", "--log-group-name-prefix", group)
    if not any(item["logGroupName"] == group for item in groups.get("logGroups", [])):
        cli(args, "logs", "create-log-group", "--log-group-name", group, "--tags", f"Project={PROJECT}")
    cli(args, "logs", "put-retention-policy", "--log-group-name", group, "--retention-in-days", str(LOG_RETENTION_DAYS))


def ensure_mapping(args, function: str, configuration: dict, cli, signed_request) -> dict:
    mappings = cli(args, "lambda", "list-event-source-mappings", "--function-name", function)
    matches = [mapping for mapping in mappings.get("EventSourceMappings", []) if mapping["EventSourceArn"] == configuration["EventSourceArn"]]
    if len(matches) > 1:
        raise ValueError("multiple matching event source mappings need explicit review")
    if matches:
        mapping = matches[0]
        update = {key: value for key, value in configuration.items() if key not in {"EventSourceArn", "StartingPosition", "Tags"}}
        return signed_request("lambda", args.region, f"/2015-03-31/event-source-mappings/{mapping['UUID']}",
                              method="PUT", payload=update, profile=args.profile)
    return signed_request("lambda", args.region, "/2015-03-31/event-source-mappings", method="POST",
                          payload={"FunctionName": function, "Tags": {"Project": PROJECT}, **configuration}, profile=args.profile)


def alarms(args, ops: dict, cli) -> None:
    definitions = [
        (ops["alarms"][0], "AWS/Lambda", "Errors", "Sum", "FunctionName", ops["worker"], 1),
        (ops["alarms"][1], "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", "QueueName", ops["dlq"], 1),
        (ops["alarms"][2], "AWS/SQS", "ApproximateAgeOfOldestMessage", "Maximum", "QueueName", ops["queue"], 300),
        (ops["alarms"][3], "AWS/Lambda", "IteratorAge", "Maximum", "FunctionName", ops["relay"], 60000),
    ]
    for name, namespace, metric, statistic, dimension, value, threshold in definitions:
        cli(args, "cloudwatch", "put-metric-alarm", "--alarm-name", name,
            "--alarm-description", "MedPlain operational alarm; notification destination is not configured.",
            "--namespace", namespace, "--metric-name", metric, "--statistic", statistic,
            "--period", "60", "--evaluation-periods", "1", "--threshold", str(threshold),
            "--comparison-operator", "GreaterThanOrEqualToThreshold", "--treat-missing-data", "notBreaching",
            "--dimensions", f"Name={dimension},Value={value}", "--tags", f"Key=Project,Value={PROJECT}")


def provision(args, plan: dict, cli, signed_request) -> dict:
    ops = plan["operations"]
    stream_arn = ensure_table(args, ops, cli)
    dlq_url = ensure_queue(args, ops["dlq"], {
        "SqsManagedSseEnabled": "true", "MessageRetentionPeriod": "1209600",
        "RedriveAllowPolicy": json.dumps({"redrivePermission": "byQueue", "sourceQueueArns": [ops["queue_arn"]]}),
    }, cli)
    queue_url = ensure_queue(args, ops["queue"], {
        "SqsManagedSseEnabled": "true", "VisibilityTimeout": str(QUEUE_VISIBILITY),
        "MessageRetentionPeriod": "345600", "ReceiveMessageWaitTimeSeconds": "20",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": ops["dlq_arn"], "maxReceiveCount": MAX_RECEIVES}),
    }, cli)
    environment = {
        "MEDPLAIN_MODEL_MODE": args.model_mode, "MEDPLAIN_BEDROCK_MODEL_ID": args.model_id,
        "MEDPLAIN_COMMIT": args.commit, "OTEL_SDK_DISABLED": "true",
        "MEDPLAIN_OPS_TABLE": ops["table"], "MEDPLAIN_OPS_QUEUE_URL": queue_url,
        "MEDPLAIN_OPS_DLQ_URL": dlq_url,
        "MEDPLAIN_OPS_DAILY_LIMIT": str(ops["daily_job_limit"]),
    }
    worker_role = ensure_role(args, ops["worker_role"], [
        log_policy(args, ops["worker"]), table_permissions(plan),
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": ops["model_arn"]},
        {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": ops["queue_arn"]},
    ], cli)
    relay_role = ensure_role(args, ops["relay_role"], [
        log_policy(args, ops["relay"]),
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Resource": ops["table_arn"]},
        {"Effect": "Allow", "Action": ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"], "Resource": ops["table_arn"] + "/stream/*"},
        {"Effect": "Allow", "Action": ["dynamodb:ListStreams"], "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": args.region}}},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": [ops["queue_arn"], ops["dlq_arn"]]},
    ], cli)
    ensure_function(args, plan, ops["worker"], ops["worker_handler"], worker_role, environment,
                    WORKER_TIMEOUT, 768, ops["worker_reserved_concurrency"], cli)
    ensure_function(args, plan, ops["relay"], ops["relay_handler"], relay_role, environment,
                    30, 384, ops["relay_reserved_concurrency"], cli)
    ensure_mapping(args, ops["worker"], {
        "EventSourceArn": ops["queue_arn"], "BatchSize": 1, "Enabled": True,
        "MaximumBatchingWindowInSeconds": 0, "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "ScalingConfig": {"MaximumConcurrency": ops["worker_reserved_concurrency"]},
    }, cli, signed_request)
    pattern = {"eventName": ["INSERT"], "dynamodb": {"NewImage": {"entity": {"S": ["OUTBOX"]}}}}
    ensure_mapping(args, ops["relay"], {
        "EventSourceArn": stream_arn, "StartingPosition": "TRIM_HORIZON", "BatchSize": 1,
        "Enabled": True, "MaximumBatchingWindowInSeconds": 0,
        "FunctionResponseTypes": ["ReportBatchItemFailures"], "ParallelizationFactor": 1,
        "FilterCriteria": {"Filters": [{"Pattern": json.dumps(pattern)}]},
        "MaximumRetryAttempts": 10, "DestinationConfig": {"OnFailure": {"Destination": ops["dlq_arn"]}},
    }, cli, signed_request)
    alarms(args, ops, cli)
    return {"environment": environment, "stream_arn": stream_arn, "queue_url": queue_url, "dlq_url": dlq_url}
