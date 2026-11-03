"""Release the additive careflow package to existing owned Lambda functions.

Plan by default. Preserve the complete live environment except its source SHA,
including daily allowance and every model/transport setting. No inference,
counter mutation, resource creation or legacy deployment-policy reset occurs.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import re
import subprocess
from pathlib import Path

from deploy_aws import cli, stage_package, staging_plan
from ops_infra import require_project

ACCOUNT = '308857099262'
REGION = 'us-east-1'
FUNCTION = 'atropos-medplain'
POLICY_NAME = 'medplain-careflow-condition-check'
HANDLERS = {
    FUNCTION + '-worker': 'medplain.careflow_worker.handler',
    FUNCTION + '-outbox-relay': 'medplain.careflow_relay.handler',
    FUNCTION: 'medplain.api.handler',
}
LEGACY_HANDLERS = {
    FUNCTION + '-worker': 'medplain.ops_worker.handler',
    FUNCTION + '-outbox-relay': 'medplain.ops_relay.handler',
    FUNCTION: 'medplain.api.handler',
}


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(65536), b''):
            digest.update(part)
    return digest.hexdigest()


def transaction_policy(account=ACCOUNT, region=REGION):
    if account != ACCOUNT or region != REGION:
        raise ValueError('Careflow release is scoped to the existing account and region')
    return {'Version': '2012-10-17', 'Statement': [{
        'Sid': 'CareflowVerifiedRevisionCondition', 'Effect': 'Allow',
        'Action': 'dynamodb:ConditionCheckItem',
        'Resource': f'arn:aws:dynamodb:{region}:{account}:table/{FUNCTION}-ops',
        'Condition': {'ForAnyValue:StringEquals': {'dynamodb:EnclosingOperation': ['TransactWriteItems']}},
    }]}


def validate_config(config, name, expected_commit, expected_digest):
    if name not in HANDLERS or config.get('FunctionName') != name:
        raise ValueError('Unexpected Lambda resource')
    env = config.get('Environment', {}).get('Variables', {})
    expected_code = base64.b64encode(bytes.fromhex(expected_digest)).decode('ascii')
    if (config.get('CodeSha256') != expected_code or env.get('MEDPLAIN_COMMIT') != expected_commit
            or config.get('LastUpdateStatus') != 'Successful' or config.get('Runtime') != 'python3.12'
            or env.get('MEDPLAIN_OPS_DAILY_LIMIT') != '120'
            or env.get('MEDPLAIN_MODEL_MODE') != 'bedrock'
            or env.get('MEDPLAIN_CAREFLOW_MODEL_MODE', 'bedrock') != 'bedrock'
            or env.get('MEDPLAIN_BEDROCK_MODEL_ID') != 'amazon.nova-micro-v1:0'
            or env.get('MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID') != 'amazon.nova-pro-v1:0'
            or env.get('MEDPLAIN_OPS_TABLE') != FUNCTION + '-ops'
            or config.get('Handler') not in {HANDLERS[name], LEGACY_HANDLERS[name]}):
        raise ValueError(f'Live source/package/model/budget/handler prerequisite differs: {name}')
    expected_role = f'arn:aws:iam::{ACCOUNT}:role/{name}-runtime'
    if config.get('Role') != expected_role:
        raise ValueError('Unexpected dedicated runtime role')
    return env


def environment_for_release(environment, commit):
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('A full source SHA is required')
    return {**environment, 'MEDPLAIN_COMMIT': commit}


def validate_released_config(config, name, commit, digest, handler, environment):
    validate_config(config, name, commit, digest)
    if (config.get('Environment', {}).get('Variables') != environment
            or config.get('Handler') != handler):
        raise ValueError('Post-release environment or exact handler differs')


def release_plan(args, call=cli):
    if args.account != ACCOUNT or args.region != REGION or args.function != FUNCTION:
        raise ValueError('Only the existing dedicated MedPlain release is supported')
    for value, length in [(args.commit, 40), (args.expected_commit, 40), (args.expected_sha256, 64)]:
        if not re.fullmatch(f'[0-9a-f]{{{length}}}', value):
            raise ValueError('Exact source and package prerequisites are required')
    identity = call(args, 'sts', 'get-caller-identity')
    if identity['Account'] != ACCOUNT:
        raise ValueError('AWS caller account differs')
    digest = digest_file(args.package)
    manifest = json.loads(args.package.with_suffix('.json').read_text(encoding='utf-8'))
    if manifest.get('sha256') != digest or manifest.get('commit') != args.commit:
        raise ValueError('CI package/source binding differs')
    result = {'status': 'plan', 'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'account': ACCOUNT, 'region': REGION, 'function': FUNCTION, 'commit': args.commit,
              'package_sha256': digest, 'previous_commit': args.expected_commit,
              'previous_package_sha256': args.expected_sha256, 'functions': {},
              'condition_policy': transaction_policy(),
              **staging_plan(ACCOUNT, REGION, args.commit, digest)}
    # Read every prerequisite before any mutation. Policies are appended, never replaced.
    for name in HANDLERS:
        observed = call(args, 'lambda', 'get-function', '--function-name', name)
        require_project(observed.get('Tags', {}), name)
        config = observed['Configuration']
        env = validate_config(config, name, args.expected_commit, args.expected_sha256)
        role_name = name + '-runtime'
        role = call(args, 'iam', 'get-role', '--role-name', role_name)
        require_project(role['Role'].get('Tags', []), role_name)
        result['functions'][name] = {
            'previous_handler': config['Handler'],
            'handler': (LEGACY_HANDLERS if getattr(args, 'runtime_profile', 'careflow') == 'legacy' else HANDLERS)[name],
            'previous_environment': env, 'environment': environment_for_release(env, args.commit),
            'revision_id': config['RevisionId'],
        }
    existing = call(args, 'iam', 'get-role-policy', '--role-name', FUNCTION + '-runtime',
                    '--policy-name', POLICY_NAME, missing='NoSuchEntity')
    if existing and existing.get('PolicyDocument') != result['condition_policy']:
        raise ValueError('Existing careflow-specific policy differs; do not overwrite it')
    result['policy_already_present'] = existing is not None
    # Do not roll workers during an active accepted batch. No receipt or queue item is deleted.
    queue = call(args, 'sqs', 'get-queue-url', '--queue-name', FUNCTION + '-jobs')
    attrs = call(args, 'sqs', 'get-queue-attributes', '--queue-url', queue['QueueUrl'],
                 '--attribute-names', 'ApproximateNumberOfMessages',
                 'ApproximateNumberOfMessagesNotVisible', 'ApproximateNumberOfMessagesDelayed')['Attributes']
    if any(int(value) for value in attrs.values()):
        raise ValueError('Accepted jobs are still pending; deployment is withheld')
    result['queue_observation'] = attrs
    day = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')
    allowance = call(args, 'dynamodb', 'get-item', '--table-name', FUNCTION + '-ops',
                     '--key', json.dumps({'PK': {'S': 'ALLOWANCE#' + day}, 'SK': {'S': 'JOBS'}}),
                     '--consistent-read').get('Item', {})
    result['natural_daily_allowance'] = {
        'utc_day': day, 'used': int(allowance.get('used', {}).get('N', '0')), 'limit': 120,
        'operation': 'read_only_no_reset_or_refund',
    }
    return result


def save_new(path, value):
    if path.exists():
        raise ValueError('Preserve the existing release journal; do not blindly repeat a launch')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def deploy(args, call=cli):
    plan = release_plan(args, call)
    save_new(args.output, plan)
    if not args.apply:
        return plan
    journal = {'utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'steps': [], 'complete': False}
    journal_path = args.output.with_name(args.output.stem + '-execution.json')
    save_new(journal_path, journal)

    def record(action, name=None):
        journal['steps'].append({'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                                 'action': action, 'function': name})
        journal_path.write_text(json.dumps(journal, indent=2) + '\n', encoding='utf-8')

    # The original stage helper enforces private ownership, encryption and expiry.
    stage_package(args, plan, plan['package_sha256'])
    record('private_package_staged')
    if not plan['policy_already_present']:
        call(args, 'iam', 'put-role-policy', '--role-name', FUNCTION + '-runtime',
             '--policy-name', POLICY_NAME, '--policy-document', json.dumps(plan['condition_policy']))
        record('exact_table_condition_permission_added')
    for name, row in plan['functions'].items():
        # Revision fences prevent silently overwriting a concurrent coordinator.
        record('code_update_intent', name)
        call(args, 'lambda', 'update-function-code', '--function-name', name,
             '--revision-id', row['revision_id'], '--s3-bucket', plan['staging_bucket'],
             '--s3-key', plan['staging_key'])
        call(args, 'lambda', 'wait', 'function-updated-v2', '--function-name', name)
        record('code_updated', name)
        config = call(args, 'lambda', 'get-function-configuration', '--function-name', name)
        if config.get('Environment', {}).get('Variables') != row['previous_environment']:
            raise ValueError('Concurrent environment change; stop without overwriting')
        record('configuration_update_intent', name)
        call(args, 'lambda', 'update-function-configuration', '--function-name', name,
             '--revision-id', config['RevisionId'], '--handler', row['handler'],
             '--environment', json.dumps({'Variables': row['environment']}))
        call(args, 'lambda', 'wait', 'function-updated-v2', '--function-name', name)
        config = call(args, 'lambda', 'get-function-configuration', '--function-name', name)
        validate_released_config(config, name, args.commit, plan['package_sha256'],
                                 row['handler'], row['environment'])
        record('exact_package_handler_environment_verified', name)
    journal['complete'] = True
    journal_path.write_text(json.dumps(journal, indent=2) + '\n', encoding='utf-8')
    return {**plan, 'status': 'deployed', 'journal': str(journal_path)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', default=ACCOUNT)
    parser.add_argument('--region', default=REGION)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--function', default=FUNCTION)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--runtime-profile', choices=['careflow', 'legacy'], default='careflow',
                        help='Legacy is an explicit recovery of a retained package, not an inference fallback')
    parser.add_argument('--operator-commit', help='Exact reviewed operator SHA when restoring a different retained package')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    # A clean committed source makes the operator itself reviewable.
    root = Path(__file__).resolve().parents[1]
    if (subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            != (arguments.operator_commit or arguments.commit)
            or subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True).strip()):
        raise ValueError('Release operator must run from its clean exact reviewed source')
    print(json.dumps(deploy(arguments), indent=2))
