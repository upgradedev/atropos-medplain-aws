"""Verify editable synthetic intake through the real SDK worker and public artifact.

CI installs Playwright. Source validation, case creation and patient release use
real HTTP responses. Explicit 413 and evaluation-read 503 injections prove error
presentation only; they never stand in for a successful agent run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlparse

from browser_smoke import wait_for_server
from console_browser_smoke import change_role, session_client
from ops_live_smoke import Client, validate_artifact, validate_worker_commit


def unfamiliar_note(template: str) -> tuple[str, str, str]:
    """Change a real clinical field, not just a fixture selector or display title."""
    pattern = r"(?m)^((?:Take|Stop|Do not take) .+? )([0-9]+(?:\.[0-9]+)?)( (?:mg|mcg|mL) by mouth )"
    match = re.search(pattern, template)
    assert match, "the official editable template must contain a supported medication"
    dose = format(Decimal(match.group(2)) + Decimal("1"), "f")
    medicine = re.sub(r"^(Take|Stop|Do not take) ", "", match.group(1)).strip()
    note = template[:match.start(2)] + dose + template[match.end(2):]
    note = note.replace("Demo-River", "Demo-Harbor")
    assert note != template
    return note, medicine, dose


def exercise(page, browser, url: str, output: Path, expect) -> dict:
    base = url.rstrip("/")
    response = page.goto(base + "/console/overview?collection=empty", wait_until="networkidle")
    assert response and response.status == 200
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    client = session_client(page, url)
    boot = client.call("/api/ops/bootstrap")
    assert client.call("/api/ops/cases?limit=20")["total"] == 0
    template = Client(url).call("/api/ops/intake-template")
    assert template["profile"] == "medplain-discharge-note-v1"
    note, changed_medicine, changed_dose = unfamiliar_note(template["source_text"])

    page.get_by_role("button", name="New case", exact=True).click()
    intake = page.get_by_role("dialog", name="New verification case", exact=True)
    intake.get_by_test_id("import-note-mode").check()
    load_template = intake.get_by_test_id("load-intake-template")
    expect(load_template).to_be_enabled()
    submit = intake.get_by_test_id("start-verification")
    expect(submit).to_be_disabled()
    expect(intake.get_by_test_id("new-case-reason")).to_contain_text("Enter a synthetic")
    load_template.click()
    source = intake.get_by_test_id("intake-source-text")
    expect(source).to_have_value(template["source_text"])
    confirmation = intake.get_by_test_id("confirm-synthetic-note")
    expect(confirmation).not_to_be_checked()
    expect(submit).to_be_disabled()
    expect(intake.get_by_test_id("new-case-reason")).to_contain_text("Confirm")
    confirmation.check()
    expect(submit).to_be_enabled()
    source.fill("x" * (template["limits"]["max_source_chars"] + 1))
    expect(source).to_have_value("x" * (template["limits"]["max_source_chars"] + 1))
    expect(source).to_have_attribute("aria-invalid", "true")
    expect(confirmation).not_to_be_checked()
    expect(submit).to_be_disabled()
    expect(intake.get_by_test_id("new-case-reason")).to_contain_text("never silently truncated")

    # Exercise an actual pre-model grammar rejection, not a substituted response.
    source.fill(note.replace("MEDPLAIN SYNTHETIC DISCHARGE V1", "UNSUPPORTED FORMAT", 1))
    confirmation.check()
    with page.expect_response(lambda res: res.url.endswith("/api/ops/cases/import") and res.request.method == "POST") as rejected:
        submit.click()
    assert rejected.value.status == 422
    expect(intake.get_by_test_id("intake-error")).to_contain_text("not accepted")
    assert client.call("/api/ops/cases?limit=20")["total"] == 0, "invalid intake must not create a case"

    source.fill(note)
    expect(confirmation).not_to_be_checked()
    intake.get_by_text("Preview exact submitted source", exact=True).click()
    expect(intake.get_by_test_id("intake-source-preview")).to_have_text(note)
    confirmation.check()
    page.screenshot(path=str(output / "editable-intake-desktop.png"), full_page=True)
    with page.expect_response(lambda res: res.url.endswith("/api/ops/cases/import") and res.request.method == "POST") as created:
        submit.click()
    assert created.value.status == 202
    submitted = created.value.request.post_data_json
    assert submitted["source_text"] == note and submitted["synthetic_confirmed"] is True
    case_id = created.value.json()["case"]["id"]
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer).to_be_visible()
    expect(drawer.get_by_test_id("source-provenance")).to_contain_text("Imported synthetic note")
    expect(drawer.get_by_test_id("original-source-note")).to_have_text(note)
    expect(drawer.get_by_test_id("parser-verification-status")).to_contain_text("matched the independent source ledger", timeout=240_000)
    expect(drawer.locator(".drawer-meta .badge")).to_have_text(re.compile(r"^(Released|Review required)$"), timeout=240_000)
    detail = client.call(f"/api/ops/cases/{case_id}")
    result = validate_artifact(detail)
    validate_worker_commit(result, boot["runtime"]["commit"])
    assert detail["origin"] == "live_run" and detail["source_kind"] == "imported_note"
    raw_hash = hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert detail["source_sha256"] == detail["intake"]["source_sha256"] == raw_hash
    assert detail["intake"]["source_text"] == note
    assert result["intake_validation"]["parser_verified"] is True
    assert any(med["name"].casefold() == changed_medicine.casefold() and med["dose"] == changed_dose
               for med in detail["source"]["medications"]), "the edited medicine dose must enter the captured source"
    expected_mode = "fixture" if urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"} else "bedrock"
    assert result["model_mode"] == expected_mode, "a fresh SDK job must use the expected model mode"
    page.screenshot(path=str(output / "imported-source-evidence.png"), full_page=True)
    drawer.get_by_role("tab", name="Agents", exact=True).click()
    for agent in ["LabAndDischargeParserAgent", "DrugAndContraindicationVerifierAgent", "PatientAdvocateExplainerAgent"]:
        expect(drawer.get_by_text(agent, exact=True).first).to_be_visible()

    if detail["status"] == "review_required":
        drawer.get_by_role("button", name="Close dialog", exact=True).click()
        change_role(page, "reviewer", expect)
        page.goto(base + f"/console/cases?case={case_id}", wait_until="networkidle")
        drawer = page.get_by_role("dialog", name="Case details", exact=True)
        drawer.get_by_label("Review comment", exact=True).fill("Checked the invented note, exact parser extraction and source-bound artifact.")
        with page.expect_response(lambda res: res.url.endswith(f"/cases/{case_id}/review") and res.request.method == "POST") as released:
            drawer.get_by_test_id("release-case").click()
        assert released.value.status == 200
        review = released.value.request.post_data_json
        assert review["expected_version"] == detail["version"]
        assert review["source_hash"] == detail["source_hash"] and review["artifact_hash"] == detail["artifact_hash"]
        detail = client.call(f"/api/ops/cases/{case_id}")
    assert detail["status"] == "released" and detail["patient_url"]
    public_client = Client(url)
    share_id = detail["patient_url"].rstrip("/").split("/")[-1]
    public = public_client.call(f"/api/ops/patient/{share_id}")
    validate_artifact({**public["case"], "source": public["source"], "result": public["result"]})
    outsider = Client(url)
    outsider.session()
    outsider.call(f"/api/ops/cases/{case_id}", expected=404)

    patient_context = browser.new_context(viewport={"width": 375, "height": 812})
    try:
        patient = patient_context.new_page()
        patient.goto(urljoin(base + "/", detail["patient_url"]), wait_until="networkidle")
        expect(patient.get_by_text("Read-only patient view", exact=True)).to_be_visible()
        medicine = patient.locator(".medicine").filter(has=patient.get_by_text(re.compile(r"^" + re.escape(changed_medicine) + r"$", re.I)))
        expect(medicine.locator(".dose")).to_contain_text(changed_dose)
        assert not patient.evaluate("document.documentElement.scrollWidth > innerWidth")
        patient.screenshot(path=str(output / "imported-patient-mobile-375.png"), full_page=True)
    finally:
        patient_context.close()
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "operator", expect)

    # Only this error response is injected. A fresh real job has already passed.
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(base + "/console/overview", wait_until="networkidle")
    page.get_by_role("button", name="New case", exact=True).click()
    intake = page.get_by_role("dialog", name="New verification case", exact=True)
    intake.get_by_test_id("import-note-mode").check()
    expect(intake.get_by_test_id("load-intake-template")).to_be_enabled()
    intake.get_by_test_id("intake-source-text").fill(note)
    intake.get_by_test_id("confirm-synthetic-note").check()
    page.route("**/api/ops/cases/import", lambda route: route.fulfill(status=413, json={"detail": "UNSAFE_PROVIDER_DETAIL_MUST_NOT_RENDER"}), times=1)
    intake.get_by_test_id("start-verification").click()
    expect(intake.get_by_test_id("intake-error")).to_contain_text("too large")
    expect(page.locator("body")).not_to_contain_text("UNSAFE_PROVIDER_DETAIL_MUST_NOT_RENDER")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(output / "intake-error-mobile-375.png"), full_page=True)
    page.keyboard.press("Escape")
    assert client.call("/api/ops/cases?limit=20")["total"] == 1

    page.route("**/api/evaluations", lambda route: route.fulfill(status=503, json={"detail": "Injected unavailable evaluation report"}), times=1)
    page.goto(base + "/console/settings", wait_until="networkidle")
    panel = page.get_by_test_id("evaluation-panel")
    expect(panel.get_by_role("heading", name="Comparison unavailable", exact=True)).to_be_visible()
    expect(panel).to_contain_text("No results are inferred")
    panel.get_by_role("button", name="Retry read", exact=True).click()
    expect(panel.get_by_test_id("evaluation-scope")).to_be_visible()
    expect(panel.get_by_role("table")).to_be_visible()
    report = Client(url).call("/api/evaluations")
    assert report["schema_version"] == 1 and report["source_commit"] == boot["runtime"]["commit"]
    if report["mode"] == "fixture":
        expect(panel.get_by_test_id("evaluation-scope")).to_contain_text("not Bedrock performance or clinical benefit")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(output / "evaluation-mobile-375.png"), full_page=True)
    return {"case_id": case_id, "source_sha256": raw_hash, "artifact_hash": detail["artifact_hash"],
            "model_mode": result["model_mode"], "changed_source_medicine": changed_medicine, "changed_source_dose": changed_dose,
            "parser_verified": True, "real_grammar_rejection": True, "confirmation_resets_on_edit": True,
            "no_silent_truncation": True, "public_patient_source_fidelity": True, "session_isolation": True,
            "injected_errors": [413, 503], "mobile_width": 375, "evaluation_report_sha256": report["report_sha256"]}


def check(url: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        host = urlparse(url).hostname
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == host else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            proof = exercise(page, browser, url, output, expect)
            assert not errors, "console must not produce JavaScript errors"
            (output / "results.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
            return proof
        except Exception:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/intake-browser"))
    args = parser.parse_args()
    process = None
    log = None
    try:
        url = args.url or "http://127.0.0.1:8767"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8767"], env=environment, stdout=log, stderr=subprocess.STDOUT)
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
