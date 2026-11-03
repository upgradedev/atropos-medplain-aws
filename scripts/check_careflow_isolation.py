"""Assert V3 leaves the original clinical and measurement contracts byte exact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name("careflow_frozen_manifest.json")


def check(root: Path, manifest: Path = MANIFEST) -> list[str]:
    frozen = json.loads(manifest.read_text(encoding="utf-8"))
    errors = []
    for name, expected in frozen["files"].items():
        target = root / name
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            errors.append(name)
    return errors


if __name__ == "__main__":
    failures = check(Path(__file__).resolve().parents[1])
    if failures:
        raise SystemExit("Frozen source changed: " + ", ".join(failures))
    print("V3 isolation: every protected e277 source, test and measurement file is byte exact")
