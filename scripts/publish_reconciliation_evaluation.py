"""Publish checked public measurement metadata to a source-specific read-only key.

Uses the existing AWS CLI, installs nothing, never starts model work and never
overwrites a different measurement. Full raw reports must already be public.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PROFILE = "medplain-reconciliation-v1"
JUDGE = "https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws"
TABLE = "atropos-medplain-ops"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def make_manifest(report: dict, report_url: str) -> dict:
    source = report.get("source_commit", "")
    url = urlsplit(report_url)
    if (report.get("schema_version") != 1 or report.get("profile") != PROFILE
            or report.get("mode") != "bedrock" or not re.fullmatch(r"[0-9a-f]{40}", source)
            or report.get("harness_commit") != source
            or digest({k: v for k, v in report.items() if k != "report_sha256"}) != report.get("report_sha256")
            or not isinstance(report.get("arms"), dict) or set(report["arms"]) != {"strands", "baseline"}
            or len(report.get("cases", [])) != 30
            or any(arm.get("case_count") != 30 for arm in report["arms"].values())):
        raise ValueError("A complete source-bound thirty-row Bedrock measurement is required")
    if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
            or not re.fullmatch(r"/upgradedev/atropos-medplain-aws/releases/download/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.json", url.path)):
        raise ValueError("Raw report must be a public project release JSON asset")
    body = {"schema_version": 1, "status": "published", "profile": PROFILE, "mode": "bedrock",
            "source_commit": source, "harness_commit": source, "report_sha256": report["report_sha256"],
            "report_url": report_url, "corpus_sha256": report["corpus"]["sha256"],
            "ruler_sha256": report["ruler"]["sha256"], "references": report["references"],
            "arms": report["arms"], "measurement_status": report["measurement_status"],
            "generated_at": report["generated_at"], "published_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Actual synthetic Bedrock measurement. Full failures and denominators remain in the public report. Not clinical or comprehension benefit."}
    return {**body, "manifest_sha256": digest(body)}


def aws(args, *command):
    result = subprocess.run(["aws", "--profile", args.profile, "--region", "us-east-1",
                             "--cli-connect-timeout", "5", "--cli-read-timeout", "15",
                             *command, "--output", "json"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Scoped AWS publication request failed: " + result.stderr[:500])
    return json.loads(result.stdout.strip() or "{}")


def get_public(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MedPlain-public-evidence-check"}), timeout=30) as response:
        data = response.read(10_000_001)
        if response.status != 200 or len(data) > 10_000_000:
            raise ValueError("Public evidence response is unavailable or too large")
        return data


def publish(args) -> dict:
    raw = args.report.read_bytes()
    report = json.loads(raw)
    manifest = make_manifest(report, args.report_url)
    health = json.loads(get_public(JUDGE + "/api/health"))
    if health.get("commit") != report["source_commit"] or health.get("model_mode") != "bedrock":
        raise ValueError("The report does not describe the exact current Bedrock deployment")
    fixture = json.loads(get_public(JUDGE + "/api/reconciliation-evaluations"))
    if (fixture.get("source_commit") != report["source_commit"]
            or fixture.get("corpus", {}).get("sha256") != report["corpus"]["sha256"]
            or fixture.get("ruler", {}).get("sha256") != report["ruler"]["sha256"]
            or fixture.get("references") != report["references"]):
        raise ValueError("The deployed fixture corpus, ruler or references differ from this measurement")
    if get_public(args.report_url) != raw:
        raise ValueError("Public raw report differs from the local measured artifact")
    result = {"status": "planned", "source_commit": manifest["source_commit"],
              "report_sha256": manifest["report_sha256"], "manifest": manifest}
    if not args.apply:
        return result
    if args.account != "308857099262" or aws(args, "sts", "get-caller-identity")["Account"] != args.account:
        raise ValueError("Use only the dedicated MedPlain account")
    table = aws(args, "dynamodb", "describe-table", "--table-name", TABLE)["Table"]
    tags = aws(args, "dynamodb", "list-tags-of-resource", "--resource-arn", table["TableArn"])["Tags"]
    if {tag["Key"]: tag["Value"] for tag in tags}.get("Project") != "AtroposMedPlain":
        raise ValueError("The table is not owned by this project")
    key = {"PK": {"S": "EVALUATION#" + manifest["source_commit"]}, "SK": {"S": "RECONCILIATION"}}
    existing = aws(args, "dynamodb", "get-item", "--table-name", TABLE, "--consistent-read", "--key", json.dumps(key)).get("Item")
    if existing:
        previous = json.loads(existing["payload"]["S"])["manifest"]
        def comparable(item):
            return {k: v for k, v in item.items() if k not in {"published_at", "manifest_sha256"}}
        if comparable(previous) != comparable(manifest) or digest({k: v for k, v in previous.items() if k != "manifest_sha256"}) != previous["manifest_sha256"]:
            raise ValueError("A different measurement already occupies this immutable source key")
        return {**result, "status": "already_published", "manifest": previous}
    expires = int(time.time()) + 30 * 86400
    row = {"ttl": expires, "manifest": manifest}
    item = {**key, "ttl": {"N": str(expires)}, "version": {"N": "0"}, "entity": {"S": "EVALUATION"},
            "payload": {"S": json.dumps(row, separators=(",", ":"), ensure_ascii=False)}}
    aws(args, "dynamodb", "put-item", "--table-name", TABLE, "--item", json.dumps(item),
        "--condition-expression", "attribute_not_exists(PK)")
    fetched = json.loads(get_public(JUDGE + "/api/reconciliation-evaluations/live"))
    if fetched != manifest:
        raise ValueError("Published public metadata differs from the checked manifest")
    return {**result, "status": "published", "expires_at": expires}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--account", default="308857099262")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(publish(parser.parse_args()), indent=2))
