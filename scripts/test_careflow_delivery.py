"""Dependency-free negative controls for the additive release operator."""

import argparse
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from deploy_careflow import (
    HANDLERS, LEGACY_HANDLERS, environment_for_release, release_plan, save_new,
    transaction_policy, validate_config, validate_released_config,
)


class CareflowDelivery(unittest.TestCase):
    def setUp(self):
        self.commit = 'e' * 40
        self.digest = hashlib.sha256(b'independent original package').hexdigest()
        self.env = {
            'MEDPLAIN_COMMIT': self.commit, 'MEDPLAIN_MODEL_MODE': 'bedrock',
            'MEDPLAIN_BEDROCK_MODEL_ID': 'amazon.nova-micro-v1:0',
            'MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID': 'amazon.nova-pro-v1:0',
            'MEDPLAIN_OPS_DAILY_LIMIT': '120', 'MEDPLAIN_OPS_TABLE': 'atropos-medplain-ops',
            'FUTURE_TRANSPORT_GUARD': 'preserve-this-unrecognized-setting',
        }

    def config(self, name='atropos-medplain'):
        return {'FunctionName': name, 'CodeSha256': base64.b64encode(bytes.fromhex(self.digest)).decode(),
                'LastUpdateStatus': 'Successful', 'Runtime': 'python3.12',
                'Handler': LEGACY_HANDLERS[name], 'Environment': {'Variables': copy.deepcopy(self.env)},
                'Role': f'arn:aws:iam::308857099262:role/{name}-runtime', 'RevisionId': 'fenced-revision'}

    def test_unknown_and_model_settings_survive_without_default_reconstruction(self):
        updated = environment_for_release(self.env, 'b' * 40)
        self.assertEqual(updated['MEDPLAIN_COMMIT'], 'b' * 40)
        self.assertEqual({key: value for key, value in updated.items() if key != 'MEDPLAIN_COMMIT'},
                         {key: value for key, value in self.env.items() if key != 'MEDPLAIN_COMMIT'})
        self.assertEqual(self.env['MEDPLAIN_COMMIT'], self.commit)

    def test_different_limits_sources_packages_roles_and_handlers_fail_closed(self):
        for name in HANDLERS:
            config = self.config(name)
            validate_config(config, name, self.commit, self.digest)
            mutations = [
                ('Handler', 'unrelated.handler'), ('Role', config['Role'] + '-other'),
                ('CodeSha256', base64.b64encode(b'x' * 32).decode()),
                ('LastUpdateStatus', 'InProgress'), ('FunctionName', 'unrelated'),
            ]
            for key, value in mutations:
                wrong = {**config, key: value}
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    validate_config(wrong, name, self.commit, self.digest)
            for key, value in [('MEDPLAIN_OPS_DAILY_LIMIT', '163'), ('MEDPLAIN_COMMIT', 'a' * 40),
                               ('MEDPLAIN_MODEL_MODE', 'fixture'),
                               ('MEDPLAIN_CAREFLOW_MODEL_MODE', 'fixture'),
                               ('MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID', 'amazon.nova-micro-v1:0')]:
                wrong = copy.deepcopy(config)
                wrong['Environment']['Variables'][key] = value
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    validate_config(wrong, name, self.commit, self.digest)

    def test_only_exact_table_transaction_condition_permission_is_added(self):
        statement = transaction_policy()['Statement'][0]
        self.assertEqual(statement['Action'], 'dynamodb:ConditionCheckItem')
        self.assertEqual(statement['Resource'],
                         'arn:aws:dynamodb:us-east-1:308857099262:table/atropos-medplain-ops')
        self.assertEqual(statement['Condition']['ForAnyValue:StringEquals']['dynamodb:EnclosingOperation'],
                         ['TransactWriteItems'])
        with self.assertRaises(ValueError):
            transaction_policy('123456789012')

    def test_new_source_with_legacy_dispatch_is_not_a_successful_careflow_release(self):
        for name in ('atropos-medplain-worker', 'atropos-medplain-outbox-relay'):
            config = self.config(name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'exact handler'):
                validate_released_config(config, name, self.commit, self.digest, HANDLERS[name], self.env)
            config['Handler'] = HANDLERS[name]
            validate_released_config(config, name, self.commit, self.digest, HANDLERS[name], self.env)

    def test_foreign_function_is_rejected_before_any_aws_call(self):
        calls = []
        args = argparse.Namespace(account='308857099262', region='us-east-1', function='foreign')
        with self.assertRaises(ValueError):
            release_plan(args, lambda *a, **kw: calls.append(a))
        self.assertEqual(calls, [])

    def test_preflight_never_mutates_when_a_later_function_has_drifted(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'release.zip'
            package.write_bytes(b'independent new package')
            package.with_suffix('.json').write_text(json.dumps({
                'commit': 'b' * 40, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}))
            args = argparse.Namespace(account='308857099262', region='us-east-1', function='atropos-medplain',
                                      commit='b' * 40, expected_commit=self.commit,
                                      expected_sha256=self.digest, package=package)
            calls = []

            def observe(args, *command, **kwargs):
                calls.append(command[:2])
                if command[:2] == ('sts', 'get-caller-identity'):
                    return {'Account': '308857099262'}
                if command[:2] == ('lambda', 'get-function'):
                    name = command[-1]
                    config = self.config(name)
                    if name == 'atropos-medplain-outbox-relay':
                        config['Environment']['Variables']['MEDPLAIN_OPS_DAILY_LIMIT'] = '163'
                    return {'Tags': {'Project': 'AtroposMedPlain'}, 'Configuration': config}
                if command[:2] == ('iam', 'get-role'):
                    return {'Role': {'Tags': [{'Key': 'Project', 'Value': 'AtroposMedPlain'}]}}
                raise AssertionError('Preflight reached an unexpected call or mutation')

            with self.assertRaises(ValueError):
                release_plan(args, observe)
            self.assertTrue(all(command in {('sts', 'get-caller-identity'), ('lambda', 'get-function'),
                                            ('iam', 'get-role')} for command in calls))

    def test_existing_launch_journal_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'launch.json'
            save_new(path, {'accepted': True})
            with self.assertRaises(ValueError):
                save_new(path, {'accepted': False})
            self.assertEqual(json.loads(path.read_text()), {'accepted': True})


if __name__ == '__main__':
    unittest.main()
