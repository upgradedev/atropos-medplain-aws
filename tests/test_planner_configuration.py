"""Role-specific model configuration and least-privilege deployment boundaries."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from medplain.api import app
from medplain.ops_domain import OpsService
from medplain.ops_store import MemoryStore

MICRO = "amazon.nova-micro-v1:0"
PRO = "amazon.nova-pro-v1:0"
MICRO_ARN = "arn:aws:bedrock:us-east-1::foundation-model/" + MICRO
PRO_ARN = "arn:aws:bedrock:us-east-1::foundation-model/" + PRO


def scripts(monkeypatch):
    root = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(root))
    modules = []
    for name in ["ops_infra", "deploy_aws"]:
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


@pytest.mark.parametrize("planner", [None, MICRO])
def test_health_and_bootstrap_expose_the_configured_role_models(monkeypatch, planner):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    monkeypatch.setenv("MEDPLAIN_BEDROCK_MODEL_ID", MICRO)
    if planner is None:
        monkeypatch.delenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", raising=False)
    else:
        monkeypatch.setenv("MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID", planner)
    expected = {"DischargeEvidenceIndexerAgent": MICRO,
                "CrossDocumentClarificationPlannerAgent": planner or PRO,
                "ClarificationCardPublisherAgent": MICRO}
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["model_id"] == MICRO
    assert response.json()["reconciliation_model_ids"] == expected
    service = OpsService(MemoryStore())
    visitor = service.new_session(prepared_demo=False)
    pk, session = service.authenticate(visitor["session_token"])
    bootstrap = service.bootstrap(session, pk)
    assert bootstrap["runtime"]["reconciliation_model_ids"] == expected
    assert bootstrap["runtime"]["model_mode"] == "fixture"


def test_default_planner_and_explicit_same_model_permission_deduplication(monkeypatch):
    ops, deployment = scripts(monkeypatch)
    plan = ops.operations_plan("123456789012", "us-east-1", "atropos-medplain", MICRO)
    assert plan["model_arn"] == MICRO_ARN
    assert plan["planner_model_id"] == PRO and plan["planner_model_arn"] == PRO_ARN
    assert plan["worker_model_arns"] == [MICRO_ARN, PRO_ARN]
    same = ops.operations_plan("123456789012", "us-east-1", "atropos-medplain", MICRO, MICRO)
    assert same["worker_model_arns"] == [MICRO_ARN]
    arguments = ["--account", "123456789012", "--package", "fixture.zip", "--commit", "ab" * 20]
    assert deployment.argument_parser().parse_args(arguments).planner_model_id == PRO
    assert deployment.argument_parser().parse_args(arguments + ["--planner-model-id", MICRO]).planner_model_id == MICRO


@pytest.mark.parametrize("bad", ["*", PRO_ARN, "us.amazon.nova-pro-v1:0",
                                  "amazon.nova-unknown-v1:0", "anthropic.unconfigured-model"])
def test_planner_rejects_unknown_models_arns_and_inference_profiles(monkeypatch, bad):
    ops, deployment = scripts(monkeypatch)
    with pytest.raises(ValueError, match="explicitly allowed"):
        ops.operations_plan("123456789012", "us-east-1", "atropos-medplain", MICRO, bad)
    with pytest.raises(SystemExit):
        deployment.argument_parser().parse_args([
            "--account", "123456789012", "--package", "fixture.zip", "--commit", "ab" * 20,
            "--planner-model-id", bad])


def test_only_worker_gets_the_two_scoped_models_and_environment_reaches_runtime(monkeypatch):
    ops, deployment = scripts(monkeypatch)
    args = argparse.Namespace(account="123456789012", region="us-east-1", function="atropos-medplain",
                              model_id=MICRO, planner_model_id=PRO, model_mode="bedrock", commit="ab" * 20)
    operation = ops.operations_plan(args.account, args.region, args.function, args.model_id, args.planner_model_id)
    plan = {"operations": operation}
    roles, functions = {}, {}

    def role(_args, name, statements, _cli):
        roles[name] = copy.deepcopy(statements)
        return "arn:aws:iam::123456789012:role/" + name

    def function(_args, _plan, name, handler, role_arn, environment, timeout, memory, concurrency, _cli):
        functions[name] = {"environment": copy.deepcopy(environment), "timeout": timeout,
                           "memory": memory, "concurrency": concurrency}

    def no_network(*_args, **_kwargs):
        raise AssertionError("Unexpected cloud request in pure policy/configuration test")

    monkeypatch.setattr(ops, "ensure_table", lambda *_: operation["table_arn"] + "/stream/fixture")
    monkeypatch.setattr(ops, "ensure_queue", lambda _args, name, _attributes, _cli: "https://queue.invalid/" + name)
    monkeypatch.setattr(ops, "ensure_role", role)
    monkeypatch.setattr(ops, "ensure_function", function)
    monkeypatch.setattr(ops, "ensure_mapping", lambda *_: {})
    monkeypatch.setattr(ops, "alarms", lambda *_: None)
    result = ops.provision(args, plan, no_network, no_network)
    worker = roles[operation["worker_role"]]
    invoke = [row for row in worker if "bedrock:InvokeModel" in row["Action"]]
    assert len(invoke) == 1 and invoke[0]["Resource"] == [MICRO_ARN, PRO_ARN]
    assert all("*" not in arn for arn in invoke[0]["Resource"])
    relay = roles[operation["relay_role"]]
    assert not any(action.startswith("bedrock:") for row in relay for action in row["Action"])
    api_policy = deployment.api_runtime_policy(args, plan)
    api_invoke = [row for row in api_policy["Statement"] if "bedrock:InvokeModel" in row["Action"]]
    assert len(api_invoke) == 1 and api_invoke[0]["Resource"] == MICRO_ARN
    assert PRO_ARN not in json.dumps(api_policy)
    assert result["environment"]["MEDPLAIN_BEDROCK_MODEL_ID"] == MICRO
    assert result["environment"]["MEDPLAIN_RECONCILIATION_PLANNER_MODEL_ID"] == PRO
    assert functions[operation["worker"]]["environment"] == result["environment"]
    assert functions[operation["relay"]]["environment"] == result["environment"]
    assert functions[operation["worker"]]["timeout"] == 180
    assert operation["daily_job_limit"] == 120


@pytest.mark.parametrize("bad", [PRO_ARN, "*", "us.amazon.nova-micro-v1:0"])
def test_base_model_keeps_its_existing_direct_nova_id_boundary(monkeypatch, bad):
    ops, _ = scripts(monkeypatch)
    with pytest.raises(ValueError, match="explicitly named Amazon Nova"):
        ops.operations_plan("123456789012", "us-east-1", "atropos-medplain", bad)
