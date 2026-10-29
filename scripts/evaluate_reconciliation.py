"""Run the separately versioned synthetic reconciliation comparison.

Fixture mode is offline. Only an explicit canonical URL, exact deployed source
and pinned corpus permit the bounded manual Bedrock run. Partial evidence is
written before commands and after every accepted terminal response.
"""

import argparse
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from medplain.reconciliation_evaluation import CANONICAL_URL, run_evaluation, write_report


@contextmanager
def offline_sockets():
    original = socket.socket.connect

    def refuse(sock, address):
        if sock.family != socket.AF_UNIX:
            raise RuntimeError("Offline reconciliation evaluation attempted a network connection")
        return original(sock, address)

    with patch.object(socket.socket, "connect", refuse), patch.object(socket.socket, "connect_ex", refuse):
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fixture", "bedrock"], default="fixture")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--corpus-sha256")
    parser.add_argument("--judge-url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/reconciliation-evaluation.json"))
    args = parser.parse_args()
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ.pop("AGENT_OBSERVABILITY_ENABLED", None)
    checkpoint = args.output.with_name(args.output.stem + ".partial.json")
    if args.mode == "bedrock" and ((args.judge_url or "").rstrip("/") != CANONICAL_URL
                                   or not args.corpus_sha256):
        parser.error("Bedrock mode requires the canonical URL and the frozen --corpus-sha256")
    if args.mode == "fixture":
        with offline_sockets():
            report = run_evaluation(args.source_commit, mode="fixture",
                                    expected_corpus_sha256=args.corpus_sha256, checkpoint=checkpoint)
    else:
        report = run_evaluation(args.source_commit, mode="bedrock", judge_url=args.judge_url,
                                expected_corpus_sha256=args.corpus_sha256, checkpoint=checkpoint)
    write_report(report, args.output)
    lines = [f"## Reconciliation comparison ({args.mode})", "",
             f"Source: `{args.source_commit}`. Corpus: `{report['corpus']['sha256']}`.", "",
             "| Arm | Cases | Valid | Exact atoms | Withheld regimen | Focus TP/FP/FN |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm, row in report["arms"].items():
        focus = row["question_focus"]
        lines.append(f"| {arm} | {row['case_count']} | {row['valid_outcome_count']} | "
                     f"{row['supported_atoms_exact_count']} | {row['proper_withholding_count']} | "
                     f"{focus['true_positive']}/{focus['false_positive']}/{focus['false_negative']} |")
    lines += ["", "Engineering-authored synthetic clarification targets. Fixture replay is not model capability. "
              "Both arms receive the same complete support and strict gate. Read every failure and all denominators. "
              "Selected label-review flags do not measure all critical drug interactions. No clinical benefit claim.",
              "", f"Report: `{args.output}`; SHA-256 `{report['report_sha256']}`. Partial evidence: `{checkpoint}`."]
    rendered = "\n".join(lines)
    print(rendered)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    # Semantic quality may be low, including in fixtures. Never change gold or
    # drop rows to turn that observation into a pass. Write raw evidence first.
    unsafe = any(row["status"] == "verified" and not all(case["scores"][arm][key]
                 for key in ["source_coverage", "source_citations", "supported_atoms_exact", "proper_withholding"])
                 for case in report["cases"] for arm, row in case["outcomes"].items())
    if unsafe:
        raise SystemExit("A verified clarification artifact failed independent source/withholding checks")
    if args.mode == "fixture" and any(row["valid_outcome_count"] != row["case_count"]
                                      for row in report["arms"].values()):
        raise SystemExit("The complete offline instrument did not produce all expected safe artifacts")


if __name__ == "__main__":
    main()
