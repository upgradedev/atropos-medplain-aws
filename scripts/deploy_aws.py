"""Deploy only named MedPlain resources from a Linux-built, hashed Lambda zip.

The default is a reviewable plan. --apply performs the deployment. This script
installs no dependencies and never mutates resources outside its project prefix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from aws_request import request


PROJECT = "AtroposMedPlain"
STAGING_EXPIRY_DAYS = 1


def staging_plan(account: str, region: str, commit: str, digest: str, bucket: str | None = None) -> dict:
    """Bind the private staging location to one account, region and artifact."""
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("account must be a twelve-digit AWS account ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region):
        raise ValueError("region must be a concrete AWS region")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("staging requires a full source commit and package SHA-256")
    prefix = f"atropos-medplain-deploy-{account}-{region}"
    selected = bucket or prefix
    if len(selected) > 63 or not re.fullmatch(re.escape(prefix) + r"(?:-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)?", selected):
        raise ValueError("staging bucket must use this project's account and region prefix")
    return {
        "staging_bucket": selected, "staging_key": f"releases/{commit}/{digest}.zip",
        "staging_expiry_days": STAGING_EXPIRY_DAYS, "staging_encryption": "AES256",
    }


def stage_package(args: argparse.Namespace, plan: dict, digest: str) -> None:
    """Require ownership before enforcing a private, short-lived upload bucket."""
    bucket = plan["staging_bucket"]
    owner = ("--expected-bucket-owner", args.account)
    exists = cli(args, "s3api", "head-bucket", "--bucket", bucket, *owner, missing="(404)")
    if exists is None:
        command = ["s3api", "create-bucket", "--bucket", bucket, "--object-ownership", "BucketOwnerEnforced"]
        if args.region != "us-east-1":
            command.extend(["--create-bucket-configuration", json.dumps({"LocationConstraint": args.region})])
        cli(args, *command)
        cli(args, "s3api", "put-bucket-tagging", "--bucket", bucket, *owner,
            "--tagging", json.dumps({"TagSet": [{"Key": "Project", "Value": PROJECT}]}))
    else:
        tags = cli(args, "s3api", "get-bucket-tagging", "--bucket", bucket, *owner, missing="NoSuchTagSet")
        if tags is None or not any(tag.get("Key") == "Project" and tag.get("Value") == PROJECT for tag in tags.get("TagSet", [])):
            raise ValueError("existing staging bucket lacks MedPlain ownership tag")
    cli(args, "s3api", "put-public-access-block", "--bucket", bucket, *owner,
        "--public-access-block-configuration", json.dumps({
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        }))
    cli(args, "s3api", "put-bucket-encryption", "--bucket", bucket, *owner,
        "--server-side-encryption-configuration", json.dumps({
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
        }))
    cli(args, "s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket, *owner,
        "--lifecycle-configuration", json.dumps({"Rules": [{
            "ID": "medplain-staging-expiry", "Status": "Enabled", "Filter": {"Prefix": "releases/"},
            "Expiration": {"Days": STAGING_EXPIRY_DAYS},
            "NoncurrentVersionExpiration": {"NoncurrentDays": STAGING_EXPIRY_DAYS},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": STAGING_EXPIRY_DAYS},
        }]}))
    cli(args, "s3", "cp", str(args.package.resolve()), f"s3://{bucket}/{plan['staging_key']}",
        "--sse", "AES256", "--metadata", f"sha256={digest},commit={args.commit}", "--only-show-errors")
    uploaded = cli(args, "s3api", "head-object", "--bucket", bucket, "--key", plan["staging_key"], *owner)
    if (uploaded.get("ContentLength") != args.package.stat().st_size
            or uploaded.get("ServerSideEncryption") != "AES256"
            or uploaded.get("Metadata", {}).get("sha256") != digest
            or uploaded.get("Metadata", {}).get("commit") != args.commit):
        raise ValueError("staging object metadata differs from the verified package")


def cli(args: argparse.Namespace, *command: str, missing: str | None = None) -> dict | None:
    result = subprocess.run(
        ["aws", "--profile", args.profile, "--region", args.region, *command, "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        if missing and missing in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def deploy(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"atropos-medplain(?:-[a-z0-9-]+)?", args.function):
        raise ValueError("function must use the dedicated atropos-medplain prefix")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise ValueError("commit must be a full Git SHA")
    if not args.package.is_file():
        raise ValueError("Linux CI package does not exist")
    identity = cli(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise ValueError("AWS account differs from the explicitly selected account")
    digest = hashlib.sha256(args.package.read_bytes()).hexdigest()
    manifest = json.loads(args.package.with_suffix(".json").read_text(encoding="utf-8"))
    if manifest.get("sha256") != digest or manifest.get("commit") != args.commit:
        raise ValueError("package hash or source commit differs from CI manifest")
    role_name = f"{args.function}-runtime"
    plan = {
        "account": args.account, "region": args.region, "function": args.function,
        "runtime_role": role_name, "model_mode": args.model_mode, "model_id": args.model_id,
        "commit": args.commit, "package_sha256": digest,
        "public_url": True, "reserved_concurrency": 2, "timeout_seconds": 180,
        "log_retention_days": 7,
        **staging_plan(args.account, args.region, args.commit, digest, getattr(args, "staging_bucket", None)),
    }
    if not args.apply:
        return {"status": "plan", **plan}
    stage_package(args, plan, digest)
    role = cli(args, "iam", "get-role", "--role-name", role_name, missing="NoSuchEntity")
    if role is not None and not any(t["Key"] == "Project" and t["Value"] == PROJECT for t in role["Role"].get("Tags", [])):
        raise ValueError("existing role lacks MedPlain ownership tag")
    if role is None:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        role = cli(args, "iam", "create-role", "--role-name", role_name, "--assume-role-policy-document", json.dumps(trust), "--tags", f"Key=Project,Value={PROJECT}")
    policy = {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": [f"arn:aws:logs:{args.region}:{args.account}:log-group:/aws/lambda/{args.function}", f"arn:aws:logs:{args.region}:{args.account}:log-group:/aws/lambda/{args.function}:*"]},
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": f"arn:aws:bedrock:{args.region}::foundation-model/{args.model_id}"},
        ],
    }
    cli(args, "iam", "put-role-policy", "--role-name", role_name, "--policy-name", "medplain-runtime", "--policy-document", json.dumps(policy))
    existing = cli(args, "lambda", "get-function", "--function-name", args.function, missing="ResourceNotFoundException")
    if existing is not None and existing.get("Tags", {}).get("Project") != PROJECT:
        raise ValueError("existing function lacks MedPlain ownership tag")
    environment = json.dumps({"Variables": {
        "MEDPLAIN_MODEL_MODE": args.model_mode, "MEDPLAIN_BEDROCK_MODEL_ID": args.model_id,
        "MEDPLAIN_COMMIT": args.commit, "OTEL_SDK_DISABLED": "true",
    }})
    if existing is None:
        for attempt in range(6):
            try:
                cli(args, "lambda", "create-function", "--function-name", args.function,
                    "--runtime", "python3.12", "--handler", "medplain.api.handler",
                    "--role", role["Role"]["Arn"], "--code", json.dumps({"S3Bucket": plan["staging_bucket"], "S3Key": plan["staging_key"]}),
                    "--timeout", "180", "--memory-size", "768", "--environment", environment,
                    "--tags", f"Project={PROJECT}")
                break
            except RuntimeError as exc:
                if "cannot be assumed" not in str(exc) or attempt == 5:
                    raise
                time.sleep(5)
    else:
        cli(args, "lambda", "update-function-code", "--function-name", args.function,
            "--s3-bucket", plan["staging_bucket"], "--s3-key", plan["staging_key"])
        cli(args, "lambda", "wait", "function-updated", "--function-name", args.function)
        cli(args, "lambda", "update-function-configuration", "--function-name", args.function, "--environment", environment, "--timeout", "180", "--memory-size", "768")
        cli(args, "lambda", "wait", "function-updated", "--function-name", args.function)
    cli(args, "lambda", "wait", "function-active-v2", "--function-name", args.function)
    log_group = f"/aws/lambda/{args.function}"
    log_groups = cli(args, "logs", "describe-log-groups", "--log-group-name-prefix", log_group)
    if not any(group["logGroupName"] == log_group for group in log_groups.get("logGroups", [])):
        cli(args, "logs", "create-log-group", "--log-group-name", log_group, "--tags", f"Project={PROJECT}")
    cli(args, "logs", "put-retention-policy", "--log-group-name", log_group, "--retention-in-days", "7")
    cli(args, "lambda", "put-function-concurrency", "--function-name", args.function, "--reserved-concurrent-executions", "2")
    url_config = cli(args, "lambda", "get-function-url-config", "--function-name", args.function, missing="ResourceNotFoundException")
    if url_config is None:
        url_config = cli(args, "lambda", "create-function-url-config", "--function-name", args.function, "--auth-type", "NONE")
    elif url_config.get("AuthType") != "NONE":
        raise ValueError("existing URL is authenticated; inspect its intended use before changing it")
    current_policy = cli(args, "lambda", "get-policy", "--function-name", args.function, missing="ResourceNotFoundException")
    statements = json.loads(current_policy["Policy"]).get("Statement", []) if current_policy else []
    present = {statement.get("Sid") for statement in statements}
    permissions = [
        {"StatementId": "MedPlainPublicFunctionUrl", "Action": "lambda:InvokeFunctionUrl", "Principal": "*", "FunctionUrlAuthType": "NONE"},
        {"StatementId": "MedPlainPublicViaFunctionUrl", "Action": "lambda:InvokeFunction", "Principal": "*", "InvokedViaFunctionUrl": True},
    ]
    for permission in permissions:
        if permission["StatementId"] not in present:
            request("lambda", args.region, f"/2015-03-31/functions/{args.function}/policy", method="POST", payload=permission, profile=args.profile)
    return {"status": "deployed", "url": url_config["FunctionUrl"], **plan}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--function", default="atropos-medplain")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--staging-bucket", help="Optional dedicated bucket with the default account and region prefix")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--model-mode", choices=["bedrock", "fixture"], default="bedrock")
    parser.add_argument("--model-id", default="amazon.nova-micro-v1:0")
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(deploy(parser.parse_args()), indent=2))
