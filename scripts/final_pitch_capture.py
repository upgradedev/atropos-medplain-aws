"""Add screenshot milestones to the unchanged, asserted loopback careflow journey in CI."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

import demo_video as media

RUNTIME = "3cbe1dc18ca4e148ca4ee1d80a6305f4fbee7571"
# Exact existing statements, never model output or a substituted API response.
HOOKS = {
    'expect(preview.get_by_test_id("careflow-preview-notice")).to_be_visible()': ("preview", "page"),
    'expect(panel.get_by_text("Anonymous synthetic staff simulation", exact=True)).to_be_visible()': ("card", "page"),
    'assert assigned.value.status == 200': ("assigned", "page"),
    'assert accepted.value.status == 200': ("accepted", "page"),
    'assert answered.value.status == 200': ("reply", "page"),
    'expect(revision_form.get_by_test_id("start-reconciliation")).to_be_disabled()': ("revision", "page"),
    'assert linked.value.status == 200': ("linked", "page"),
    'expect(panel.get_by_text("Resolution recorded", exact=True)).to_be_visible()': ("closed", "page"),
    'expect(evidence.locator("blockquote").first).to_be_visible()': ("evidence", "page"),
    'assert released.value.status == 200': ("release", "page"),
    'expect(feedback.get_by_label("Your synthetic response", exact=True)).to_have_value("Yes, I understand.")': ("response", "patient"),
    'expect(feedback.get_by_test_id("teachback-next-step")).to_contain_text("waiting for human review")': ("assessment", "patient"),
    'expect(feedback.get_by_text("Reviewer recorded follow-up", exact=False)).to_be_visible()': ("follow_up", "patient"),
    'wait_attempt(patient, feedback, expect, count=2)': ("recheck", "patient"),
}


def instrument(source):
    tree = ast.parse(source)
    originals = {ast.dump(ast.parse(key).body[0], include_attributes=False): value for key, value in HOOKS.items()}
    found = []

    class CaptureHooks(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            if node.name != "exercise":
                return node
            body = []
            for statement in node.body:
                body.append(statement)
                match = originals.get(ast.dump(statement, include_attributes=False))
                if match:
                    name, page = match
                    found.append(name)
                    body.append(ast.Expr(ast.Call(ast.Name("record_final_frame", ast.Load()),
                                                  [ast.Name(page, ast.Load()), ast.Constant(name)], [])))
            node.body = body
            return node

    altered = ast.fix_missing_locations(CaptureHooks().visit(tree))
    media.require(len(found) == len(HOOKS) and len(set(found)) == len(HOOKS), "Exact screenshot milestones changed; review the journey")
    # Remove additive calls and prove every original action/assertion bytecode AST is preserved.
    clean = ast.parse(ast.unparse(altered))
    function = next(node for node in clean.body if isinstance(node, ast.FunctionDef) and node.name == "exercise")
    function.body = [node for node in function.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == "record_final_frame")]
    media.require(ast.dump(clean, include_attributes=False) == ast.dump(ast.parse(source), include_attributes=False),
                  "Screenshot hooks changed original journey semantics")
    return altered, found


def capture(app, output):
    media.require(not output.exists(), "Preserve earlier captures and failed partials")
    output.mkdir(parents=True)
    app = app.resolve()
    identity = subprocess.run(["git", "rev-parse", "HEAD"], cwd=app, capture_output=True, text=True, check=True, timeout=10)
    media.require(identity.stdout.strip() == RUNTIME, "Capture must build the exact frozen application source")
    source_path = app / "scripts/careflow_browser_smoke.py"
    source = source_path.read_text(encoding="utf-8")
    tree, expected = instrument(source)
    sys.path.insert(0, str(app / "scripts"))
    rows = []

    def record(page, name):
        media.require(urlparse(page.url).hostname in {"127.0.0.1", "localhost"}, "Capture target escaped loopback")
        media.require(not urlparse(page.url).fragment, "Private response capability must be removed before capture")
        path = output / (name + ".png")
        media.require(not path.exists(), "Duplicate accepted screenshot milestone")
        # Browser chrome/URLs are not captured. Hide capability links while preserving layout.
        style = page.add_style_tag(content='a[href*="#teachback="] { visibility: hidden !important; }')
        try:
            page.screenshot(path=str(path), full_page=False)
        finally:
            style.evaluate("node => node.remove()")
        rows.append({"slot": name, "image": name + ".png", "sha256": media.digest(path),
                     "kind": "current_fixture", "source_commit": RUNTIME,
                     "provenance_label": "Current 3cbe1dc | actual offline fixture API + Strands SDK, no new Bedrock execution",
                     "evidence_reference": "capture-proof.json / original asserted careflow browser journey",
                     "viewport": page.viewport_size})

    namespace = {"__name__": "final_capture_journey", "__file__": str(source_path), "record_final_frame": record}
    exec(compile(tree, str(source_path), "exec"), namespace)
    environment = dict(os.environ, MEDPLAIN_COMMIT=RUNTIME, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_CAREFLOW_MODEL_MODE="fixture",
                       MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
    environment.pop("MEDPLAIN_OPS_TABLE", None)
    log = (output / "fixture-server.log").open("xb")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8769"],
                               cwd=app, env=environment, stdout=log, stderr=subprocess.STDOUT)
    try:
        namespace["wait_for_server"]("http://127.0.0.1:8769")
        proof = namespace["check"]("http://127.0.0.1:8769", output / "journey")
        media.require(len(rows) == len(expected), "Missing screenshot milestones")
        result = {"schema": "medplain-final-capture-v1", "status": "completed_asserted_loopback_fixture_journey",
                  "runtime_commit": RUNTIME, "journey_sha256": media.digest(source_path),
                  "capture_helper_sha256": media.digest(Path(__file__)), "screenshots": rows, "journey": proof,
                  "live_provider_calls": 0, "transmitted_non_loopback_application_requests": 0,
                  "representation": "Captured original application pixels during actual offline fixture execution; no provider efficacy or clinical outcome claim."}
        (output / "capture-proof.json").write_bytes(media.encoded(result))
        return result
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(capture(arguments.app, arguments.output.resolve()), indent=2))
