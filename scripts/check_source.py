"""Dependency-free tracked-source secret and judge-facing punctuation checks."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


PATTERNS = (
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("assigned secret", re.compile(r'''(?i)(?:aws_secret_access_key|api_secret|client_secret)\s*[:=]\s*["'][A-Za-z0-9/+]{24,}["']''')),
)


def check() -> list[str]:
    errors = []
    files = set(filter(None, subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")))
    generated_console = Path("src/medplain/static/console")
    if generated_console.is_dir():
        files.update(path.as_posix() for path in generated_console.rglob("*") if path.is_file())
    for name in sorted(files):
        path = Path(name)
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(content):
                errors.append(f"{name}: possible {label}; inspect before publishing")
        if name == "README.md" or name.startswith(("src/medplain/static/", "web/")):
            if "\u2014" in content:
                errors.append(f"{name}: em dash in judge-facing prose")
    return errors


if __name__ == "__main__":
    failures = check()
    for failure in failures:
        print(failure)
    if failures:
        raise SystemExit(1)
    print("Tracked-source secret scan and judge-facing punctuation checks passed")
