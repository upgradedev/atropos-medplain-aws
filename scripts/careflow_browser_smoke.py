"""Exercise additive careflow against a real loopback fixture API in CI.

No task, patient-response, assessment, source, receipt or revision result is
substituted. One labelled evidence-search503 proves visible error recovery.
Capabilities and session headers are never written to artifacts.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

from browser_smoke import wait_for_server
from console_browser_smoke import change_role, session_client
from ops_live_smoke import Client, wait_case
from reconciliation_browser_smoke import assert_patient_width, audit_detail


def binding(view: dict) -> dict:
    return {"expected_version": view["case_version"], "source_hash": view["source_hash"],
            "artifact_hash": view["artifact_hash"], "idempotency_key": uuid4().hex}


def open_case(page, url, case_id, expect):
    page.goto(url + "/console/cases?case=" + case_id, wait_until="networkidle")
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer).to_be_visible()
    drawer.get_by_role("tab", name="Clarifications", exact=True).click()
    expect(drawer.get_by_role("heading", name="Clarification workspace", exact=True)).to_be_visible()
    return drawer


def select_task(panel, task, expect):
    control = panel.get_by_test_id("careflow-task-" + task["id"])
    expect(control).to_contain_text(task["prompt"])
    control.click()


def wait_attempt(page, panel, expect, count=1):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        rows = panel.locator('[aria-label="Teach-back attempt history"] > li')
        if rows.count() >= count and rows.last.locator(".careflow-status").inner_text().lower() in {"assessed", "reviewed", "failed"}:
            expect(rows.last.get_by_text("Provisional assessment, reviewer required", exact=True)).to_be_visible()
            return
        panel.get_by_role("button", name="Refresh response history", exact=True).click()
        expect(panel.get_by_role("button", name="Refresh response history", exact=True)).to_be_enabled()
        page.wait_for_timeout(500)
    raise AssertionError("actual fixture teach-back assessment did not complete within120seconds")


def exercise(page, browser, url: str, output: Path, expect) -> dict:
    page.goto(url + "/console/overview?collection=empty", wait_until="networkidle")
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    client = session_client(page, url)
    health = Client(url).call("/api/health")
    assert health["model_mode"] == "fixture", "careflow browser journey must never invoke live models"
    boot = client.call("/api/ops/bootstrap")
    examples = client.call("/api/ops/reconciliation-examples")
    dossier = copy.deepcopy(next(row["dossier"] for row in examples["examples"] if row["id"] == "pending-result-gap"))
    dossier["patient_alias"] = "Demo-Careflow"
    created = client.call("/api/ops/cases/reconcile", {"dossier": dossier, "synthetic_confirmed": True,
        "facility_id": boot["facilities"][0]["id"], "idempotency_key": uuid4().hex, "stress": False}, expected=202)
    case_id = created["case"]["id"]
    original = wait_case(client, case_id)
    original_proof = audit_detail(original, dossier, "fixture", health["commit"])
    path = f"/api/ops/cases/{case_id}/careflow"
    drawer = open_case(page, url, case_id, expect)
    panel = drawer.get_by_test_id("careflow-panel")
    expect(panel.get_by_text("Anonymous synthetic staff simulation", exact=True)).to_be_visible()
    actors = client.call("/api/ops/careflow/actors")
    actor = next(row for row in actors["actors"] if row["id"] == "demo-pharmacist")
    with page.expect_response(lambda r: r.url.endswith("/careflow/actor") and r.request.method == "POST") as selected:
        panel.get_by_label("Act as synthetic staff", exact=True).select_option(actor["id"])
    assert selected.value.status == 200
    view = client.call(path)
    task = next(row for row in view["tasks"] if row["code"] == "confirm_pending_owner")
    select_task(panel, task, expect)
    panel.get_by_label("Assign synthetic staff", exact=True).select_option(actor["id"])
    with page.expect_response(lambda r: r.url.endswith("/careflow/assign") and r.request.method == "POST") as assigned:
        panel.get_by_role("button", name="Assign follow-up", exact=True).click()
    assert assigned.value.status == 200
    expect(panel.get_by_role("button", name="Accept responsibility", exact=True)).to_be_disabled()
    expect(panel.get_by_text("Switch to the reviewer or admin synthetic demo role.", exact=True).first).to_be_visible()
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "reviewer", expect)
    drawer = open_case(page, url, case_id, expect)
    panel = drawer.get_by_test_id("careflow-panel")
    select_task(panel, task, expect)
    with page.expect_response(lambda r: r.url.endswith("/careflow/accept") and r.request.method == "POST") as accepted:
        panel.get_by_role("button", name="Accept responsibility", exact=True).click()
    assert accepted.value.status == 200
    panel.get_by_label("Source-bound clinician answer", exact=True).fill("Synthetic discharge-team review: the named follow-up contact is recorded in a separate source addendum. Keep treatment decisions with the care team.")
    with page.expect_response(lambda r: r.url.endswith("/careflow/answer") and r.request.method == "POST") as answered:
        panel.get_by_role("button", name="Record clinician answer", exact=True).click()
    assert answered.value.status == 200
    expect(panel.get_by_role("button", name="Link verified revision", exact=True)).to_be_disabled()
    expect(panel.get_by_text("Create and independently verify a revised source packet", exact=False)).to_be_visible()

    # Original case and verified source remain immutable while a direct child is verified.
    client.call("/api/ops/session/role", {"role": "operator"})
    revised = copy.deepcopy(dossier)
    revised["documents"].append({"document_id": "clarified-owner", "kind": "addendum",
        "issued_at": "2026-10-30T12:00:00Z", "effective_at": None,
        "text": "Pending test: potassium; owner: discharge team; follow-up: within 2 days.\nThe discharge team will contact the synthetic patient about the pending result. No medication change is recorded in this addendum."})
    child = client.call("/api/ops/cases/reconcile", {"dossier": revised, "synthetic_confirmed": True,
        "facility_id": boot["facilities"][0]["id"], "idempotency_key": uuid4().hex, "stress": False,
        "parent_case_id": case_id}, expected=202)
    revision_id = child["case"]["id"]
    revision = wait_case(client, revision_id)
    revision_proof = audit_detail(revision, revised, "fixture", health["commit"])
    assert revision["parent_case_id"] == case_id and revision["source_hash"] != original["source_hash"]
    client.call("/api/ops/session/role", {"role": "reviewer"})
    drawer = open_case(page, url, case_id, expect)
    panel = drawer.get_by_test_id("careflow-panel")
    select_task(panel, task, expect)
    panel.get_by_label("Separately verified source revision", exact=True).select_option(revision_id)
    with page.expect_response(lambda r: r.url.endswith("/careflow/link-revision") and r.request.method == "POST") as linked:
        panel.get_by_role("button", name="Link verified revision", exact=True).click()
    assert linked.value.status == 200
    close = panel.get_by_role("button", name="Confirm resolution and close", exact=True)
    expect(close).to_be_disabled()
    panel.get_by_label("I reviewed this verified revision", exact=False).check()
    with page.expect_response(lambda r: r.url.endswith("/careflow/close") and r.request.method == "POST") as closed:
        close.click()
    assert closed.value.status == 200
    expect(panel.get_by_text("Resolution recorded", exact=True)).to_be_visible()
    after = client.call(f"/api/ops/cases/{case_id}")
    assert after["source"] == original["source"] and after["source_hash"] == original["source_hash"]
    assert after["artifact_hash"] == original["artifact_hash"] and after["result"]["patient_regimen"] is None
    flow = client.call(path)
    closed_task = next(row for row in flow["tasks"] if row["id"] == task["id"])
    assert closed_task["status"] == "closed" and closed_task["revision"]["case_id"] == revision_id
    assert closed_task["closure"]["revision_source_hash"] == revision["source_hash"]

    evidence = panel.get_by_test_id("careflow-evidence")
    evidence.get_by_label("Evidence question", exact=True).fill("pending test responsibility accepted transfer")
    evidence.get_by_role("button", name="Search reviewed evidence", exact=True).click()
    expect(evidence.get_by_text("Matching reviewed passages", exact=True)).to_be_visible()
    expect(evidence.locator("blockquote").first).to_be_visible()
    evidence.get_by_label("Evidence question", exact=True).fill("zzzxqvnonexistenttopic")
    evidence.get_by_role("button", name="Search reviewed evidence", exact=True).click()
    expect(evidence.get_by_text("No matching reviewed passage", exact=True)).to_be_visible()
    page.route("**/api/ops/careflow/evidence/search", lambda route: route.fulfill(status=503, json={"detail": "Injected evidence service unavailable"}), times=1)
    evidence.get_by_role("button", name="Search reviewed evidence", exact=True).click()
    expect(evidence.get_by_role("alert")).to_contain_text("Injected evidence service unavailable")
    evidence.get_by_label("Evidence question", exact=True).fill("pending test responsibility")
    evidence.get_by_role("button", name="Search reviewed evidence", exact=True).click()
    expect(evidence.get_by_text("Matching reviewed passages", exact=True)).to_be_visible()
    page.set_viewport_size({"width": 375, "height": 812})
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "careflow drawer overflows375px"
    page.screenshot(path=str(output / "careflow-mobile-375.png"), full_page=True)

    # Releasing a clarification does not resolve every task or certify understanding.
    drawer.get_by_label("Review comment", exact=True).fill("Release the synthetic source-bound clarification card only. Medication regimen remains withheld and open issues remain open.")
    with page.expect_response(lambda r: r.url.endswith(f"/cases/{case_id}/review") and r.request.method == "POST") as released:
        drawer.get_by_test_id("release-case").click()
    assert released.value.status == 200
    panel = drawer.get_by_test_id("careflow-panel")
    review_panel = panel.get_by_test_id("teachback-review")
    expect(review_panel.get_by_role("button", name="Enable synthetic patient responses", exact=True)).to_be_enabled()
    review_panel.get_by_role("button", name="Enable synthetic patient responses", exact=True).click()
    response_link = review_panel.get_by_role("link", name="Open private synthetic response view", exact=True)
    expect(response_link).to_be_visible()
    response_url = urljoin(url, response_link.get_attribute("href"))
    parsed = urlparse(response_url)
    assert parsed.hostname in {"localhost", "127.0.0.1"} and parse_qs(parsed.fragment).get("teachback")
    bare_url = response_url.split("#", 1)[0]
    share = parsed.path.rsplit("/", 1)[1]
    anonymous = browser.new_context(viewport={"width": 375, "height": 812})
    anonymous.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname in {"127.0.0.1", "localhost"} else route.abort())
    bare = anonymous.new_page()
    bare.goto(bare_url, wait_until="networkidle")
    expect(bare.get_by_test_id("patient-teachback").get_by_text("This share is read-only", exact=True)).to_be_visible()
    assert bare.request.get(url + f"/api/ops/careflow/patient/{share}").status in {401, 403, 404}
    patient = anonymous.new_page()
    guarded_headers = []
    def inspect_request(request):
        if "/api/ops/careflow/patient/" in request.url:
            headers = request.all_headers()
            guarded_headers.append({"method": request.method,
                "has_response_capability": bool(headers.get("x-medplain-teachback")),
                "has_console_session": "x-medplain-session" in headers})
    patient.on("request", inspect_request)
    try:
        patient.goto(response_url, wait_until="networkidle")
    except Exception:
        raise AssertionError("Private synthetic response navigation failed; capability omitted") from None
    feedback = patient.get_by_test_id("patient-teachback")
    expect(feedback.get_by_role("button", name="Submit synthetic response", exact=True)).to_be_disabled()
    assert not urlparse(patient.url).fragment, "response capability must be stripped after tab-scoped storage"
    feedback.get_by_label("Your synthetic response", exact=True).fill("Yes, I understand.")
    feedback.get_by_label("This is wholly invented", exact=False).check()
    with patient.expect_response(lambda r: r.url.endswith("/responses") and r.request.method == "POST") as submitted:
        feedback.get_by_role("button", name="Submit synthetic response", exact=True).click()
    assert submitted.value.status == 202
    wait_attempt(patient, feedback, expect)
    expect(feedback.get_by_role("button", name="Submit synthetic recheck", exact=True)).to_be_disabled()
    patient.reload(wait_until="networkidle")
    expect(feedback.get_by_text("Synthetic response access", exact=True)).to_be_visible()
    assert_patient_width(patient, output)
    patient.screenshot(path=str(output / "teachback-mobile-375.png"), full_page=True)

    # Real owner review requests another explanation; no model automatically certifies comprehension.
    panel.get_by_role("button", name="Reload workflow", exact=True).click()
    attempts = client.call(path)["teachback"]["attempts"]
    assert len(attempts) == 1 and attempts[0]["status"] == "assessed"
    review_panel.get_by_label("Response awaiting reviewer", exact=True).select_option(attempts[0]["id"])
    review_panel.get_by_label("Review decision", exact=True).select_option("follow_up")
    review_panel.get_by_label("Private reviewer comment", exact=True).fill("The acknowledgement did not demonstrate understanding. Re-explain only the approved source and request another synthetic response.")
    with page.expect_response(lambda r: r.url.endswith(f"/teachback/{attempts[0]['id']}/review") and r.request.method == "POST") as review_saved:
        review_panel.get_by_role("button", name="Record teach-back review", exact=True).click()
    assert review_saved.value.status == 200
    patient.get_by_role("button", name="Refresh response history", exact=True).click()
    expect(feedback.get_by_text("Reviewer recorded follow-up", exact=False)).to_be_visible()
    feedback.get_by_label("Your synthetic response", exact=True).fill("I would ask the discharge team to confirm who will contact us about the pending potassium result. I would not decide a medication change from this card.")
    feedback.get_by_label("This is wholly invented", exact=False).check()
    with patient.expect_response(lambda r: r.url.endswith("/responses") and r.request.method == "POST") as rechecked:
        feedback.get_by_role("button", name="Submit synthetic recheck", exact=True).click()
    assert rechecked.value.status == 202
    wait_attempt(patient, feedback, expect, count=2)
    public = patient.request.get(url + f"/api/ops/careflow/patient/{share}", headers={
        "X-Medplain-Teachback": parse_qs(parsed.fragment)["teachback"][0]}).json()
    assert public["source_hash"] == original["source_hash"] and public["artifact_hash"] == original["artifact_hash"]
    assert len(public["attempts"]) == 2 and all(attempt["assessment"]["requires_reviewer"] is True for attempt in public["attempts"])
    def public_keys(value):
        if isinstance(value, dict):
            return set(value).union(*(public_keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(public_keys(child) for child in value))
        return set()
    assert not {"runtime", "evidence", "receipt", "actor_id", "comment", "tasks", "response_capability"} & public_keys(public), "patient projection leaked private workflow data"
    assert all(row["has_response_capability"] and not row["has_console_session"] for row in guarded_headers)
    feedback.get_by_role("button", name="Clear response access", exact=True).click()
    expect(feedback.get_by_text("This share is read-only", exact=True)).to_be_visible()
    patient.reload(wait_until="networkidle")
    expect(feedback.get_by_text("This share is read-only", exact=True)).to_be_visible()
    anonymous.close()
    return {"scope": "Real loopback fixture API and SDK workflow only; no clinical or live-model efficacy claim.",
        "source_case": original_proof, "revision_case": revision_proof, "task_closed": closed_task["id"],
        "original_source_and_artifact_unchanged": True, "regimen_withheld": True,
        "roles_and_explicit_acceptance": True, "verified_revision_required": True,
        "patient_share_separate_response_capability": True, "response_capability_not_console_session": guarded_headers,
        "actual_responses": 2, "provisional_assessment_not_comprehension": True,
        "reviewer_reexplain_recheck": True, "injected_read_errors": [503], "mobile_width": 375}


def check(url: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright
    assert urlparse(url).hostname in {"127.0.0.1", "localhost"}, "this bounded fixture journey refuses non-loopback targets"
    assert Client(url).call("/api/health")["model_mode"] == "fixture"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname in {"127.0.0.1", "localhost"} else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            proof = exercise(page, browser, url.rstrip("/"), output, expect)
            assert not errors, f"JavaScript errors: {errors}"
            (output / "results.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return proof
        except Exception as error:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            # Do not serialize response capabilities, headers or share URLs.
            (output / "failure.json").write_text(json.dumps({"error_type": type(error).__name__, "page_error_count": len(errors)}) + "\n", encoding="utf-8")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/careflow-browser"))
    args = parser.parse_args()
    process = log = None
    try:
        url = args.url or "http://127.0.0.1:8769"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_CAREFLOW_MODEL_MODE="fixture", MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            environment.pop("MEDPLAIN_OPS_TABLE", None)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8769"], env=environment, stdout=log, stderr=subprocess.STDOUT)
            wait_for_server(url)
        print(json.dumps(check(url, args.output), indent=2))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if log:
            log.close()
