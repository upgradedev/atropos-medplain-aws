"""Read-only observation of the dedicated live release; never submits model jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

FUNCTIONS = ('atropos-medplain', 'atropos-medplain-worker', 'atropos-medplain-outbox-relay')
ENV_KEYS = (
    'MEDPLAIN_COMMIT', 'MEDPLAIN_MODEL_MODE', 'MEDPLAIN_BEDROCK_MODEL_ID',
    'MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID', 'MEDPLAIN_OPS_DAILY_LIMIT',
    'MEDPLAIN_RECONCILIATION_CONVERSE', 'MEDPLAIN_RECONCILIATION_REQUIRED_TOOL_GUARD',
    'MEDPLAIN_CAREFLOW_MODEL_MODE', 'MEDPLAIN_OPS_TABLE',
)


def aws(profile, region, *args):
    run = subprocess.run(['aws', '--profile', profile, '--region', region, *args,
                          '--output', 'json'], capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def observe(profile, region, base):
    identity = aws(profile, region, 'sts', 'get-caller-identity')
    if identity['Account'] != '308857099262' or region != 'us-east-1':
        raise ValueError('Unexpected account or region')
    result = {'utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'account': identity['Account'],
              'region': region, 'disk_free_bytes': shutil.disk_usage(Path.cwd()).free,
              'scope': 'Read-only configuration and anonymous HTTP, no model jobs', 'functions': {}}
    for name in FUNCTIONS:
        config = aws(profile, region, 'lambda', 'get-function-configuration', '--function-name', name)
        env = config.get('Environment', {}).get('Variables', {})
        row = {key: config.get(key) for key in ('FunctionName', 'Handler', 'CodeSha256', 'Role',
                                               'LastModified', 'LastUpdateStatus', 'RevisionId')}
        row['environment'] = {key: env[key] for key in ENV_KEYS if key in env}
        # Freeze all model/behavior environment variables without publishing unrelated secrets.
        row['complete_environment_sha256'] = hashlib.sha256(
            json.dumps(env, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row['environment_keys'] = sorted(env)
        result['functions'][name] = row
    for path in ('/', '/api/health', '/api/ops/careflow/profile'):
        try:
            with urllib.request.urlopen(base.rstrip('/') + path, timeout=30) as response:
                body = response.read()
                result[path] = {'status': response.status, 'sha256': hashlib.sha256(body).hexdigest()}
                if path != '/':
                    result[path]['body'] = json.loads(body)
        except urllib.error.HTTPError as error:
            result[path] = {'status': error.code}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--url', default='https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = observe(args.profile, args.region, args.url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError('Preserve the existing observation; select a new output path')
    args.output.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(data, indent=2))
