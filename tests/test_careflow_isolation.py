"""The additive-version guard must reject a deliberate clinical-file mutation."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


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
    assert guard._check_files(tmp_path, json.loads(manifest.read_text())["files"]) == []
    target.write_bytes(original + b"relaxed = True\n")
    assert guard._check_files(tmp_path, json.loads(manifest.read_text())["files"]) == ["gate.py"]
    target.unlink()
    assert guard._check_files(tmp_path, json.loads(manifest.read_text())["files"]) == ["gate.py"]


class IsolationRegressionTests(unittest.TestCase):
    """Run with stdlib unittest as well as the full dependency-backed CI suite."""

    def setUp(self):
        self.guard = module()
        self.root = Path(__file__).resolve().parents[1]
        self.frozen = json.loads(self.guard.MANIFEST.read_text(encoding="utf-8"))

    def check_manifest_rejected(self, frozen):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(frozen), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.guard.check(self.root, path)

    def test_actual_current_parent_files(self):
        test_protected_runtime_and_measurement_source_remains_exact()

    def test_independent_original_mutation_and_missing_file_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            test_guard_rejects_changed_or_missing_frozen_bytes(Path(directory))

    def test_current_parent_and_all_original_H12_regressions_are_protected(self):
        self.assertEqual(self.frozen["frozen_parent"], "d29b702ca3f2074cd2ad75351ddad76234c9c4fc")
        self.assertEqual(len(self.frozen["files"]), 69)
        for name in (
            "src/medplain/gate.py", "src/medplain/reconciliation.py",
            "src/medplain/required_tool_model.py", "src/medplain/reconciliation_engine.py",
            "src/medplain/reconciliation_evaluation.py",
            "src/medplain/data/reconciliation_corpus.json",
            "src/medplain/data/reconciliation_references.json",
            "scripts/evaluate_postreset_reconciliation.py", "scripts/deploy_aws.py",
            "tests/test_converse_single_call.py", "tests/test_covered_question_scopes.py",
            "tests/test_nova_tool_schema.py", "tests/test_question_decision_inputs.py",
        ):
            self.assertIn(name, self.frozen["files"])

    def test_old_e277_parent_is_not_current_isolation(self):
        self.frozen["frozen_parent"] = "e277167e3ec925c21acf4924bffd116470160afb"
        self.check_manifest_rejected(self.frozen)

    def test_dropping_a_protected_file_cannot_hide_a_change(self):
        del self.frozen["files"]["src/medplain/required_tool_model.py"]
        self.check_manifest_rejected(self.frozen)

    def test_rewriting_an_expected_hash_cannot_bless_new_bytes(self):
        self.frozen["files"]["src/medplain/gate.py"] = "0" * 64
        self.check_manifest_rejected(self.frozen)

    def test_empty_map_is_not_an_isolation_pass(self):
        self.frozen["files"] = {}
        self.check_manifest_rejected(self.frozen)

    def test_wrong_profile_and_extra_fields_fail_closed(self):
        self.frozen["profile"] = "unprotected"
        self.check_manifest_rejected(self.frozen)
        self.frozen["profile"] = "medplain-careflow-v3"
        self.frozen["skip_validation"] = True
        self.check_manifest_rejected(self.frozen)

    def test_duplicate_manifest_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            text = self.guard.MANIFEST.read_text(encoding="utf-8")
            path.write_text(text[:-2] + ', "files": {}\n}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate isolation manifest key"):
                self.guard.check(self.root, path)

    def test_external_or_noncanonical_names_are_rejected(self):
        for name in ("../gate.py", "/tmp/gate.py", "C:/gate.py", "dir\\gate.py", "./gate.py", "dir//gate.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.guard._check_files(self.root, {name: "0" * 64})

    def test_directory_cannot_replace_protected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gate.py").mkdir()
            self.assertEqual(self.guard._check_files(root, {"gate.py": "0" * 64}), ["gate.py"])

    def test_symlink_cannot_satisfy_matching_protected_bytes(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "original.py"
            payload = b"independent original bytes\n"
            outside.write_bytes(payload)
            link = root / "gate.py"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest("Host cannot create symlinks: " + str(exc))
            self.assertEqual(self.guard._check_files(root, {"gate.py": hashlib.sha256(payload).hexdigest()}), ["gate.py"])


if __name__ == "__main__":
    unittest.main()
