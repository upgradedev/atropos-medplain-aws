"""Execute the real optional Strands Evals SDK and preserve paired raw outcomes."""

import argparse
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from medplain.evaluation import CANONICAL_URL, run_evaluation, write_report


@contextmanager
def offline_sockets():
    connect = socket.socket.connect

    def refuse(sock, address):
        if sock.family != socket.AF_UNIX:
            raise RuntimeError("Offline evaluation attempted an internet connection")
        return connect(sock, address)

    with patch.object(socket.socket, "connect", refuse), patch.object(socket.socket, "connect_ex", refuse):
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fixture", "bedrock"], default="fixture")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--judge-url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/evaluation_report.json"))
    args = parser.parse_args()
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ.pop("AGENT_OBSERVABILITY_ENABLED", None)
    if args.mode == "fixture":
        with offline_sockets():
            report = run_evaluation(args.source_commit, mode="fixture")
    else:
        if (args.judge_url or "").rstrip("/") != CANONICAL_URL:
            parser.error("Bedrock mode permits only the canonical public MedPlain URL")
        report = run_evaluation(args.source_commit, mode="bedrock", judge_url=args.judge_url)
    write_report(report, args.output)
    print(f"Saved {args.mode} evidence to {args.output}; sha256={report['report_sha256']}")
    lines = [f"## MedPlain paired evaluation ({args.mode})", "",
             f"Source: `{args.source_commit}`. Corpus: `{report['corpus']['sha256']}`.", "",
             "| Arm | Cases | Valid outcomes | Exact extraction | Field fidelity | False accepts |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm, c in report["arms"].items():
        lines.append(f"| {arm} | {c['case_count']} | {c['validity_passed']} | "
                     f"{c['extraction_exact_count']}/{c['extraction_eligible_count']} | "
                     f"{c['field_fidelity_passed']}/{c['field_fidelity_eligible_count']} | "
                     f"{c['gate_false_accept_count']}/{c['gate_mutation_count']} |")
    lines += ["", "Synthetic engineering checks only. Fixture runs do not measure Bedrock inference capability, "
              "cost, clinical appropriateness or comprehension. Read all raw failures and limitations in the JSON."]
    print("\n".join(lines))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
    # Completion failures remain visible in a valid report. A safety false
    # accept is a hard failure after the raw evidence has been written.
    if any(c["gate_false_accept_count"] for c in report["arms"].values()):
        raise SystemExit("Clinical gate accepted a coherent mutation")


if __name__ == "__main__":
    main()
