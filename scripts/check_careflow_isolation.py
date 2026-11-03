"""Assert V3 leaves the original clinical and measurement contracts byte exact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MANIFEST = Path(__file__).with_name("careflow_frozen_manifest.json")
FROZEN_PARENT = "d29b702ca3f2074cd2ad75351ddad76234c9c4fc"
# Deliberately bound to the reviewed parent map, not calculated from live files.
# A truncated manifest or rewritten expected hash cannot turn a change green.
PROTECTED_MAP_SHA256 = "e70a39f9fb2e57cd69e7cf0bc207664db8dc5b0849163fb922213d056e93f11b"


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate isolation manifest key: " + key)
        result[key] = value
    return result


def _validate_files(files: dict) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError("The protected file map must be nonempty")
    for name, expected in files.items():
        path = PurePosixPath(name) if isinstance(name, str) else None
        if (path is None or not path.parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
                or path.is_absolute() or path.as_posix() != name
                or any(part in {".", ".."} for part in path.parts)):
            raise ValueError("Protected file paths must stay relative to the checkout")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Protected files require exact SHA-256 digests")


def _load_manifest(manifest: Path) -> dict:
    frozen = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    if (not isinstance(frozen, dict)
            or set(frozen) != {"profile", "frozen_parent", "purpose", "files"}
            or frozen.get("profile") != "medplain-careflow-v3"
            or frozen.get("frozen_parent") != FROZEN_PARENT
            or not isinstance(frozen.get("purpose"), str) or not frozen["purpose"].strip()):
        raise ValueError("Isolation manifest does not bind the reviewed current parent")
    _validate_files(frozen["files"])
    mapping = json.dumps(frozen["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(mapping).hexdigest() != PROTECTED_MAP_SHA256:
        raise ValueError("The reviewed protected file map was removed, added to or rewritten")
    return frozen


def _check_files(root: Path, files: dict) -> list[str]:
    """Compare actual independent bytes; never follow a replacement symlink."""
    _validate_files(files)
    root = root.resolve()
    errors = []
    for name, expected in files.items():
        parts = PurePosixPath(name).parts
        target = root.joinpath(*parts)
        if (any(root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1))
                or not target.is_file()):
            errors.append(name)
            continue
        try:
            with target.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError:
            errors.append(name)
            continue
        if actual != expected:
            errors.append(name)
    return errors


def check(root: Path, manifest: Path = MANIFEST) -> list[str]:
    return _check_files(root, _load_manifest(manifest)["files"])


if __name__ == "__main__":
    try:
        failures = check(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, UnicodeError) as exc:
        raise SystemExit("Frozen source manifest invalid: " + str(exc)) from None
    if failures:
        raise SystemExit("Frozen source changed: " + ", ".join(failures))
    print("Careflow isolation: all 69 protected current-main d29b702 files are byte exact")
