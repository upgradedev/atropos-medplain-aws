"""Apply Oct31 admission guards around the unchanged reconciliation evaluator.

Only session/case POST admission changes. Responses, exceptions, reservations,
clinical checks, partial reports and scores belong to the original harness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
from unittest.mock import patch

CANONICAL_URL = "https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws"
CORPUS_SHA256 = "9b3a505d607272554100a34713cdd0573cdb542a998f65b8cc5b28d6dd0c5c43"
WINDOW_START = datetime(2026, 10, 31, tzinfo=timezone.utc)
ADMISSION_BEFORE = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)
POST_PATHS = frozenset({"/api/ops/session", "/api/ops/cases/reconcile"})
ROOT = Path(__file__).resolve().parents[1]
FROZEN_FILES = {
    "scripts/evaluate_reconciliation.py": "81ddbe18d1cb4f76f59f6797196ed4b0485c00f67201ab41a7734802ef190e9d",
    "src/medplain/reconciliation_evaluation.py": "c878ef02a12aa9d479de2a93767e13176d5a9f348c4fe2836ba5aa92a8b4d905",
}


class PostAdmissionClosed(RuntimeError):
    """A request was not forwarded; the frozen harness records this failure."""


def utc_now():
    return datetime.now(timezone.utc)


def validate_bindings(judge_url, source_commit, corpus_sha256, harness_commit):
    if judge_url != CANONICAL_URL or corpus_sha256 != CORPUS_SHA256:
        raise ValueError("Exact canonical URL and frozen corpus are required")
    if not isinstance(source_commit, str) or not re.fullmatch("[0-9a-f]{40}", source_commit) or source_commit != harness_commit:
        raise ValueError("The exact checked-out and deployed source commits must match")


class PostAdmissionGuard:
    """Delegate the identical request once, only after a fresh per-POST clock read."""

    def __init__(self, delegate, source_commit, clock=None):
        self.delegate, self.source_commit = delegate, source_commit
        self.clock = clock or utc_now
        self.forwarded_posts = self.blocked_posts = 0
        self.last_post_time = None

    def request(self, runner, path, payload=None):
        if runner.url != CANONICAL_URL or runner.commit != self.source_commit:
            raise ValueError("Remote request source binding changed")
        if payload is not None:
            now = self.clock()
            allowed_clock = isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None
            now = now.astimezone(timezone.utc) if allowed_clock else None
            self.last_post_time = now.isoformat() if now is not None else None
            if path not in POST_PATHS or now is None or not WINDOW_START <= now < ADMISSION_BEFORE:
                self.blocked_posts += 1
                raise PostAdmissionClosed("Session/case POST admission is closed; no request forwarded")
            self.forwarded_posts += 1
        return self.delegate(runner, path, payload)

    def receipt(self):
        return {"scope": "Transport admission only; original harness results and reservations remain unchanged.",
                "window_start": WINDOW_START.isoformat(), "admission_before": ADMISSION_BEFORE.isoformat(),
                "forwarded_session_and_case_posts": self.forwarded_posts, "blocked_posts": self.blocked_posts,
                "last_post_utc": self.last_post_time, "source_commit": self.source_commit,
                "provider_request_count": "not_measured; forwarded POSTs are not model invocations"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-url", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--corpus-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    validate_bindings(args.judge_url, args.source_commit, args.corpus_sha256, os.getenv("GITHUB_SHA"))
    for relative, expected in FROZEN_FILES.items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("The frozen evaluator bytes changed")
    # Imports and dependency installation occur only in CI. The guard itself is
    # standard-library-only and has no provider credential or model capability.
    from medplain.reconciliation_evaluation import RemoteRunner
    harness = ROOT / "scripts/evaluate_reconciliation.py"
    entrypoint = runpy.run_path(str(harness), run_name="frozen_reconciliation_entrypoint")["main"]
    guard = PostAdmissionGuard(RemoteRunner.request, args.source_commit)

    def guarded_request(runner, path, payload=None):
        return guard.request(runner, path, payload)

    frozen_argv = [str(harness), "--mode", "bedrock", "--judge-url", args.judge_url,
                   "--source-commit", args.source_commit, "--corpus-sha256", args.corpus_sha256,
                   "--output", str(args.output)]
    try:
        with patch.object(RemoteRunner, "request", guarded_request), patch.object(sys, "argv", frozen_argv):
            entrypoint()
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        receipt_path = args.output.with_name(args.output.stem + ".admission.json")
        receipt_path.write_text(json.dumps(guard.receipt(), indent=2) + "\n", encoding="utf-8")
    if guard.blocked_posts:
        raise SystemExit("POST admission blocked; original failed/unexecuted report and partial evidence retained")


if __name__ == "__main__":
    main()
