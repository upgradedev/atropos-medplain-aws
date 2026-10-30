"""UTC admission controls and frozen-harness replay, with no live transport."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("postreset_transport", ROOT / "scripts/evaluate_postreset_reconciliation.py")
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class TestPostAdmissionGuard(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.returned = (202, {"created": True})
        self.runner = SimpleNamespace(url=transport.CANONICAL_URL, commit="a" * 40)

        def delegate(runner, path, payload=None):
            self.calls.append((runner, path, payload))
            return self.returned

        self.delegate = delegate

    def test_exact_utc_boundaries_for_both_post_paths(self):
        for path in sorted(transport.POST_PATHS):
            for now, allowed in [
                (transport.WINDOW_START - timedelta(microseconds=1), False),
                (transport.WINDOW_START, True),
                (transport.ADMISSION_BEFORE - timedelta(microseconds=1), True),
                (transport.ADMISSION_BEFORE, False),
                (datetime(2026, 11, 1, tzinfo=timezone.utc), False),
            ]:
                with self.subTest(path=path, now=now):
                    self.calls.clear()
                    guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=lambda: now)
                    payload = {}  # Empty dict is a POST, never mistaken for a GET.
                    if allowed:
                        self.assertIs(guard.request(self.runner, path, payload), self.returned)
                        self.assertIs(self.calls[0][2], payload)
                    else:
                        with self.assertRaises(transport.PostAdmissionClosed):
                            guard.request(self.runner, path, payload)
                        self.assertEqual(self.calls, [])

    def test_fresh_clock_blocks_case_after_an_allowed_session(self):
        times = iter([transport.WINDOW_START, transport.ADMISSION_BEFORE])
        guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=lambda: next(times))
        guard.request(self.runner, "/api/ops/session", {"prepared_demo": False})
        with self.assertRaises(transport.PostAdmissionClosed):
            guard.request(self.runner, "/api/ops/cases/reconcile", {"dossier": {}})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((guard.forwarded_posts, guard.blocked_posts), (1, 1))

    def test_backward_clock_shift_cannot_reopen_the_previous_utc_day(self):
        times = iter([transport.WINDOW_START + timedelta(minutes=1), transport.WINDOW_START - timedelta(seconds=1)])
        guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=lambda: next(times))
        guard.request(self.runner, "/api/ops/session", {})
        with self.assertRaises(transport.PostAdmissionClosed):
            guard.request(self.runner, "/api/ops/cases/reconcile", {})
        self.assertEqual(len(self.calls), 1)

    def test_get_evidence_reads_are_unchanged_after_admission_closes(self):
        def clock_must_not_be_read():
            raise AssertionError("GET must retain frozen evidence-read semantics")
        guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=clock_must_not_be_read)
        self.assertIs(guard.request(self.runner, "/api/ops/cases/already-accepted"), self.returned)
        self.assertEqual((guard.forwarded_posts, guard.blocked_posts), (0, 0))

    def test_unknown_post_naive_clock_and_changed_source_never_forward(self):
        guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=lambda: transport.WINDOW_START)
        with self.assertRaises(transport.PostAdmissionClosed):
            guard.request(self.runner, "/api/other-write", {})
        guard = transport.PostAdmissionGuard(self.delegate, self.runner.commit, clock=lambda: datetime(2026, 10, 31))
        with self.assertRaises(transport.PostAdmissionClosed):
            guard.request(self.runner, "/api/ops/session", {})
        for field, value in [("url", "https://example.invalid"), ("commit", "b" * 40)]:
            changed = copy.copy(self.runner)
            setattr(changed, field, value)
            with self.assertRaises(ValueError):
                guard.request(changed, "/api/ops/session", {})
        self.assertEqual(self.calls, [])

    def test_delegate_exception_is_identical_and_never_retried(self):
        failure = TimeoutError("indeterminate transport")
        def delegate(*args):
            self.calls.append(args)
            raise failure
        guard = transport.PostAdmissionGuard(delegate, self.runner.commit, clock=lambda: transport.WINDOW_START)
        with self.assertRaises(TimeoutError) as observed:
            guard.request(self.runner, "/api/ops/cases/reconcile", {})
        self.assertIs(observed.exception, failure)
        self.assertEqual(len(self.calls), 1)

    def test_binding_validation_and_frozen_file_fingerprints(self):
        values = [transport.CANONICAL_URL, "a" * 40, transport.CORPUS_SHA256, "a" * 40]
        transport.validate_bindings(*values)
        for index, changed in [(0, transport.CANONICAL_URL + "/"), (1, "a" * 39), (2, "0" * 64), (3, "b" * 40)]:
            wrong = list(values)
            wrong[index] = changed
            with self.assertRaises(ValueError):
                transport.validate_bindings(*wrong)
        for path, expected in transport.FROZEN_FILES.items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected)


class TestFrozenHarnessReplay(unittest.TestCase):
    """Runs only under dependency-enabled CI; no actual provider/HTTP request."""

    def _fixture_report(self):
        candidates = [ROOT / "artifacts/reconciliation-fixture.json", ROOT / "src/medplain/data/reconciliation_report.json"]
        path = next(path for path in candidates if path.is_file())
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(report["cases"]), 30)
        self.assertEqual(report["mode"], "fixture")
        return report

    def test_frozen_main_replays_saved_fixture_bodies_without_relabelling_them_live(self):
        from medplain import reconciliation_evaluation as evaluation
        report = self._fixture_report()
        source = report["source_commit"]
        by_source = {evaluation.digest(row["expected"]["dossier"]): row for row in report["cases"]}
        calls, details = [], {}

        def replay(runner, path, payload=None):
            calls.append((path, payload is not None))
            if path == "/api/health":
                return 200, {"commit": source, "model_mode": "bedrock"}
            if path == "/api/ops/session":
                return 201, {"session_token": "fixture-session", "role": "operator", "demo": True}
            if path == "/api/ops/bootstrap":
                return 200, {"runtime": {"commit": source}, "limits": {"max_cases": 20},
                             "collection": {"initialized": True, "prepared_count": 0, "live_count": 0}}
            if path == "/api/ops/cases/reconcile":
                original = by_source[evaluation.digest(payload["dossier"])]["outcomes"]["strands"]["result"]
                case_id = "spent-" + str(len(details))
                details[case_id] = {"source": payload["dossier"], "source_hash": evaluation.digest(payload["dossier"]),
                                    "source_profile": evaluation.PROFILE, "generation": 1,
                                    "result": copy.deepcopy(original), "status": "review_required", "duration_ms": 0,
                                    "review": None, "patient_url": None}
                return 202, {"created": True, "case": {"id": case_id}}
            return 200, details[path.rsplit("/", 1)[1]]

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_SHA": source}), \
                patch.object(evaluation.RemoteRunner, "request", replay), patch.object(transport, "utc_now", return_value=transport.WINDOW_START), \
                patch("sys.stdout", new=io.StringIO()):
            output = Path(directory) / "replayed.json"
            transport.main(["--judge-url", transport.CANONICAL_URL, "--source-commit", source,
                            "--corpus-sha256", transport.CORPUS_SHA256, "--output", str(output)])
            replayed = json.loads(output.read_text())
            self.assertEqual(len(replayed["cases"]), 30)
            for row in replayed["cases"]:
                outcome = row["outcomes"]["strands"]
                self.assertEqual(outcome["status"], "error")  # The original fixture identity is correctly refused as live.
                self.assertEqual(outcome["result"], by_source[evaluation.digest(row["expected"]["dossier"])]["outcomes"]["strands"]["result"])
                self.assertEqual(outcome["result"]["model_mode"], "fixture")
            self.assertEqual(replayed["arms"]["strands"]["verified_count"], 0)
            self.assertTrue(output.with_name("replayed.partial.json").is_file())
            admission = json.loads(output.with_name("replayed.admission.json").read_text())
            self.assertEqual(admission["forwarded_session_and_case_posts"], 32)
            self.assertEqual(admission["blocked_posts"], 0)
        self.assertEqual(sum(path == "/api/ops/cases/reconcile" and post for path, post in calls), 30)

    def test_queued_late_start_keeps_frozen_failed_and_unexecuted_partial_rows(self):
        from medplain import reconciliation_evaluation as evaluation
        cases = evaluation.corpus()
        calls = []

        def replay(runner, path, payload=None):
            calls.append((path, payload))
            return 200, {"commit": "a" * 40, "model_mode": "bedrock"}

        guard = transport.PostAdmissionGuard(replay, "a" * 40, clock=lambda: transport.ADMISSION_BEFORE)
        with tempfile.TemporaryDirectory() as directory:
            def guarded(runner, path, payload=None):
                return guard.request(runner, path, payload)
            with patch.object(evaluation.RemoteRunner, "request", guarded):
                path = Path(directory) / "partial.json"
                journal = evaluation.CheckpointJournal(path, cases, "a" * 40, "bedrock")
                runner = evaluation.RemoteRunner(transport.CANONICAL_URL, "a" * 40, journal)
                self.assertEqual(runner.run(cases[0])["status"], "error")
                self.assertEqual(runner.run(cases[1])["status"], "not_executed")
                saved = json.loads(path.read_text())
                self.assertEqual(saved["job_reservations"], 0)
                self.assertEqual(saved["cases"][0]["outcomes"]["strands"]["error"], "PostAdmissionClosed")
                self.assertEqual(sum(row["outcomes"]["strands"]["status"] == "not_executed" for row in saved["cases"]), 29)
        self.assertEqual(calls, [("/api/health", None)])
        self.assertEqual(guard.blocked_posts, 1)

    def test_cutoff_during_setup_preserves_reservation_but_forwards_no_case_post(self):
        from medplain import reconciliation_evaluation as evaluation
        cases, calls = evaluation.corpus(), []
        times = iter([transport.WINDOW_START, transport.ADMISSION_BEFORE])

        def replay(runner, path, payload=None):
            calls.append((path, payload is not None))
            if path == "/api/health":
                return 200, {"commit": "a" * 40, "model_mode": "bedrock"}
            if path == "/api/ops/session":
                return 201, {"session_token": "fixture-session", "role": "operator", "demo": True}
            if path == "/api/ops/bootstrap":
                return 200, {"runtime": {"commit": "a" * 40}, "limits": {"max_cases": 20},
                             "collection": {"initialized": True, "prepared_count": 0, "live_count": 0}}
            raise AssertionError("A case POST must not reach the transport")

        guard = transport.PostAdmissionGuard(replay, "a" * 40, clock=lambda: next(times))

        def guarded(runner, path, payload=None):
            return guard.request(runner, path, payload)

        with tempfile.TemporaryDirectory() as directory, patch.object(evaluation.RemoteRunner, "request", guarded):
            path = Path(directory) / "partial.json"
            runner = evaluation.RemoteRunner(transport.CANONICAL_URL, "a" * 40,
                                             evaluation.CheckpointJournal(path, cases, "a" * 40, "bedrock"))
            outcome = runner.run(cases[0])
            self.assertEqual(outcome["error"], "PostAdmissionClosed")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["job_reservations"], runner.jobs)
            self.assertEqual(runner.jobs, 1)  # A frozen local reservation is not a forwarded/model job.
            self.assertEqual(saved["cases"][0]["outcomes"]["strands"]["status"], "error")
        self.assertEqual(calls, [("/api/health", False), ("/api/ops/session", True), ("/api/ops/bootstrap", False)])
        self.assertEqual((guard.forwarded_posts, guard.blocked_posts), (1, 1))
