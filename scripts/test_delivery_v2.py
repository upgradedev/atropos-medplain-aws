"""Dependency-free checks for V2 deployment boundaries and nested build assets."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from ops_infra import operations_plan, preflight, require_project, table_permissions
from stage_console import stage, verify_archive


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class DeliveryBoundaries(unittest.TestCase):
    def setUp(self):
        self.ops = operations_plan("123456789012", "us-east-1", "atropos-medplain", "amazon.nova-micro-v1:0")

    def test_queue_budget_and_actual_domain_storage_contract(self):
        self.assertEqual(self.ops["queue_visibility_seconds"], 1080)
        self.assertGreaterEqual(self.ops["queue_visibility_seconds"], 6 * 180)
        self.assertEqual(self.ops["max_receive_count"], 3)
        self.assertEqual(self.ops["table_keys"], ["PK", "SK"])
        self.assertEqual(self.ops["ttl_attribute"], "ttl")
        self.assertEqual(self.ops["stream_view"], "NEW_IMAGE")
        self.assertEqual(self.ops["daily_job_limit"], 120)
        self.assertEqual(self.ops["alarm_notifications"], "unconfigured")
        self.assertEqual(len(self.ops["alarms"]), 4)

    def test_api_has_only_named_table_data_actions(self):
        policy = table_permissions({"operations": self.ops})
        self.assertEqual(policy["Resource"], "arn:aws:dynamodb:us-east-1:123456789012:table/atropos-medplain-ops")
        self.assertEqual(set(policy["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"})

    def test_wrong_scope_and_ownership_are_refused(self):
        for function, region in [("unrelated", "us-east-1"), ("atropos-medplain", "eu-west-1")]:
            with self.assertRaises(ValueError):
                operations_plan("123456789012", region, function, "amazon.nova-micro-v1:0")
        for tags in [{}, {"Project": "DifferentProject"}, [{"Key": "Project", "Value": "DifferentProject"}]]:
            with self.assertRaises(ValueError):
                require_project(tags, "existing-resource")

    def test_all_preflight_calls_remain_read_only_on_foreign_resource(self):
        calls = []

        def read_only(args, *command, **kwargs):
            calls.append(command[:2])
            if command[:2] == ("s3api", "head-bucket"):
                return None
            if command[:2] == ("iam", "get-role"):
                return {"Role": {"Arn": "arn:aws:iam::123456789012:role/atropos-medplain-runtime", "Tags": []}}
            raise AssertionError("preflight reached a mutation or ignored foreign ownership")

        args = argparse.Namespace(account="123456789012", region="us-east-1", function="atropos-medplain")
        with self.assertRaisesRegex(ValueError, "ownership tag"):
            preflight(args, {"operations": self.ops, "staging_bucket": "atropos-medplain-deploy-123456789012-us-east-1"}, read_only)
        self.assertEqual(calls, [("s3api", "head-bucket"), ("iam", "get-role")])

    def test_nested_console_assets_survive_archive_and_tampering_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "web/dist"
            (source / "assets/deep").mkdir(parents=True)
            (source / "index.html").write_text('<script src="/console/assets/deep/app.js"></script>')
            (source / "assets/deep/app.js").write_text("console.log('compiled fixture');")
            destination = root / "src/medplain/static/console"
            with working_directory(root):
                stage(source, destination)
                manifest = json.loads((destination / "_console_manifest.json").read_text())
                self.assertIn("assets/deep/app.js", manifest["files"])
                archive = root / "release.zip"
                with zipfile.ZipFile(archive, "w") as package:
                    package.write(destination / "index.html", "medplain/static/console/index.html")
                    package.write(destination / "assets/deep/app.js", "medplain/static/console/assets/deep/app.js")
                self.assertEqual(verify_archive(archive, destination)["verified_console_assets"], 2)
                with zipfile.ZipFile(archive, "w") as package:
                    package.write(destination / "index.html", "medplain/static/console/index.html")
                    package.writestr("medplain/static/console/assets/deep/app.js", "changed")
                with self.assertRaisesRegex(ValueError, "differs"):
                    verify_archive(archive, destination)
                with self.assertRaisesRegex(ValueError, "destination"):
                    stage(source, root / "outside-generated-console")


if __name__ == "__main__":
    unittest.main()
