"""Assemble already-installed Linux dependencies plus source. Installs nothing."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import zipfile
from pathlib import Path


def package(dependency_dir: Path, source_dir: Path, output: Path, commit: str, require_console: bool = False,
            require_prepared_demo: bool = False, require_evaluation_report: bool = False,
            require_reconciliation_report: bool = False) -> dict:
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise ValueError("commit must be a full lowercase Git SHA")
    if not (dependency_dir / "strands").is_dir() or not (dependency_dir / "mangum").is_dir():
        raise ValueError("dependency directory must contain the Linux-installed runtime dependencies")
    if not (source_dir / "medplain" / "api.py").is_file():
        raise ValueError("source directory must contain medplain/api.py")
    if require_console and not (source_dir / "medplain/static/console/_console_manifest.json").is_file():
        raise ValueError("the release package requires the compiled console manifest")
    prepared_path = source_dir / "medplain/data/prepared_demo.json"
    if require_prepared_demo and not prepared_path.is_file():
        raise ValueError("the release package requires the prepared demonstration collection")
    evidence_path = source_dir / "medplain/data/evaluation_report.json"
    if require_evaluation_report:
        if not evidence_path.is_file():
            raise ValueError("the release package requires the Strands evaluation comparison")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        payload = {key: value for key, value in evidence.items() if key != "report_sha256"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        if evidence.get("source_commit") != commit or evidence.get("report_sha256") != digest:
            raise ValueError("evaluation report differs from the release source or content hash")
    if require_reconciliation_report:
        reconciliation = source_dir / "medplain/data/reconciliation_report.json"
        if not reconciliation.is_file():
            raise ValueError("the release package requires the complex discharge evaluation report")
        evidence = json.loads(reconciliation.read_text(encoding="utf-8"))
        payload = {key: value for key, value in evidence.items() if key != "report_sha256"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        if (evidence.get("source_commit") != commit or evidence.get("report_sha256") != digest
                or evidence.get("profile") != "medplain-reconciliation-v1" or evidence.get("mode") != "fixture"):
            raise ValueError("reconciliation report differs from the source, profile or content hash")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"commit": commit, "built_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    entries = {}
    for root in (dependency_dir, source_dir):
        for path in root.rglob("*"):
            if path.is_file() and not any(part in {"__pycache__", ".pytest_cache", "tests"} for part in path.parts):
                if path.suffix not in {".pyc", ".pyo"}:
                    entries[path.relative_to(root).as_posix()] = path
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(entries.items()):
            archive.write(path, name)
        archive.writestr("medplain/_build.json", json.dumps(metadata, sort_keys=True))
    result = {**metadata, "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path("src"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/medplain-lambda.zip"))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--require-console", action="store_true")
    parser.add_argument("--require-prepared-demo", action="store_true")
    parser.add_argument("--require-evaluation-report", action="store_true")
    parser.add_argument("--require-reconciliation-report", action="store_true")
    args = parser.parse_args()
    print(json.dumps(package(args.dependency_dir, args.source_dir, args.output, args.commit,
                             args.require_console, args.require_prepared_demo, args.require_evaluation_report,
                             args.require_reconciliation_report)))
