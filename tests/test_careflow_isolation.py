"""The additive-version guard must reject a deliberate clinical-file mutation."""

import importlib.util
import json
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/check_careflow_isolation.py"
    spec = importlib.util.spec_from_file_location("careflow_isolation", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_protected_runtime_and_measurement_source_remains_exact():
    guard = module()
    assert guard.check(Path(__file__).resolve().parents[1]) == []


def test_guard_rejects_changed_or_missing_frozen_bytes(tmp_path):
    guard = module()
    import hashlib

    original = b"an independent frozen safety contract\n"
    target = tmp_path / "gate.py"
    target.write_bytes(original)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": {"gate.py": hashlib.sha256(original).hexdigest()}}))
    assert guard.check(tmp_path, manifest) == []
    target.write_bytes(original + b"relaxed = True\n")
    assert guard.check(tmp_path, manifest) == ["gate.py"]
    target.unlink()
    assert guard.check(tmp_path, manifest) == ["gate.py"]
