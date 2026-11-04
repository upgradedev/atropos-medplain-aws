"""Check first-use comprehension against the real loopback fixture UI in CI.

Preview must issue no writes or jobs. Only the explicit confirmation/submit
creates one fixture case. Existing careflow journey actions remain untouched.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

from browser_smoke import wait_for_server
from console_browser_smoke import session_client
from ops_live_smoke import Client, wait_case


def width_proof(page, width):
    observed = page.evaluate("({width: innerWidth, content: document.documentElement.scrollWidth})")
    assert observed["width"] == width, "Use measured CSS viewport, not requested width alone"
    assert observed["content"] <= width, "First-use content must not overflow horizontally"
    return observed


def exercise(page, url, output, expect):
    page.goto(url + "/console/overview?collection=empty", wait_until="networkidle")
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    client = session_client(page, url)
    assert Client(url).call("/api/health")["model_mode"] == "fixture"
    assert client.call("/api/ops/cases?limit=20")["total"] == 0
    writes = []
    page.on("request", lambda request: writes.append({"method": request.method, "path": urlparse(request.url).path})
            if request.method not in {"GET", "HEAD", "OPTIONS"} else None)

    entry = page.get_by_test_id("careflow-entry")
    expect(entry.get_by_role("heading", name="Who will follow up after discharge?", exact=True)).to_be_visible()
    expect(entry.get_by_test_id("careflow-preview-boundary")).to_contain_text("starts no model job")
    entry_box = entry.bounding_box()
    kpi_box = page.locator(".kpi-grid").bounding_box()
    assert entry_box and kpi_box and entry_box["y"] < kpi_box["y"], "Explain the task before session counts"
    guide = entry.get_by_test_id("careflow-input-action-outcome")
    guide.locator(":scope > summary").click()
    expect(guide.locator("ol > li")).to_have_count(3)
    expect(guide).to_contain_text("Medication decisions stay with the care team")
    guide.locator(":scope > summary").click()
    page.get_by_role("button", name="Collapse sidebar", exact=True).click()
    expect(page.locator(".app-shell.rail")).to_be_visible()
    expect(entry.get_by_role("button", name="Preview coordinated follow-up", exact=True)).to_be_visible()
    desktop = width_proof(page, 1440)
    page.screenshot(path=str(output / "first-use-collapsed-sidebar.png"), full_page=False)
    page.get_by_role("button", name="Expand sidebar", exact=True).click()

    page.get_by_role("button", name="New case", exact=True).click()
    source_choice = page.get_by_role("dialog", name="New verification case", exact=True)
    expect(source_choice.locator("#source-choice-guide")).to_contain_text("Reconcile multiple sources")
    expect(source_choice.get_by_test_id("start-verification")).to_be_enabled()
    source_choice.get_by_role("button", name="Close dialog", exact=True).click()

    page.set_viewport_size({"width": 375, "height": 812})
    mobile = width_proof(page, 375)
    entry.get_by_role("button", name="Preview coordinated follow-up", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(output / "first-use-mobile-375.png"), full_page=False)
    entry.get_by_role("button", name="Preview coordinated follow-up", exact=True).click()
    preview = page.get_by_role("dialog", name="Reconcile synthetic source packet", exact=True)
    expect(preview.get_by_test_id("careflow-preview-notice")).to_contain_text("creates no case and starts no model job")
    expect(preview.get_by_label("Complex synthetic example", exact=True)).to_have_value("pending-result-gap")
    expect(preview.get_by_label("Clarification card language", exact=True)).to_have_value("en")
    steps = preview.get_by_test_id("reconciliation-steps")
    steps.locator(":scope > summary").click()
    expect(steps.locator("ol > li")).to_have_count(3)
    expect(steps.locator(":scope > ol")).to_have_css("list-style-type", "decimal")
    expect(steps).to_contain_text("A failed gate withholds the card")
    boundary_note = steps.locator(":scope > p")
    expect(boundary_note).to_be_visible()
    last_step_box = steps.locator("ol > li").last.bounding_box()
    boundary_note_box = boundary_note.bounding_box()
    assert last_step_box and boundary_note_box, "Expanded submission guide requires visible geometry"
    last_step_bottom = last_step_box["y"] + last_step_box["height"]
    assert last_step_bottom <= boundary_note_box["y"], "Submission guidance overlaps the following review/release boundary"
    expect(preview.get_by_label("Confirm invented reconciliation sources", exact=True)).not_to_be_checked()
    expect(preview.get_by_test_id("start-reconciliation")).to_be_disabled()
    preview_mobile = width_proof(page, 375)
    page.screenshot(path=str(output / "first-use-preview-mobile-375.png"), full_page=False)
    assert writes == [], "Opening forms and preview must transmit no mutation request"
    assert client.call("/api/ops/cases?limit=20")["total"] == 0, "Preview must not create a case"

    preview.get_by_label("Confirm invented reconciliation sources", exact=True).check()
    expect(preview.get_by_test_id("start-reconciliation")).to_be_enabled()
    with page.expect_response(lambda response: response.url.endswith("/api/ops/cases/reconcile")
                              and response.request.method == "POST") as submitted:
        preview.get_by_test_id("start-reconciliation").click()
    assert submitted.value.status == 202
    created = submitted.value.json()
    case_id = created["case"]["id"]
    case = wait_case(client, case_id)
    assert case["result"]["status"] == "verified" and case["result"]["model_mode"] == "fixture"
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer.get_by_text("New verification run using an offline fixture model.", exact=True)).to_be_visible()
    expect(drawer.get_by_test_id("careflow-case-orientation")).to_contain_text("The follow-up workspace", ignore_case=True)
    drawer.get_by_role("button", name="Open clarification workspace", exact=True).click()
    panel = drawer.get_by_test_id("careflow-panel")
    role_guide = panel.get_by_test_id("careflow-role-guide")
    role_guide.locator(":scope > summary").click()
    expect(role_guide).to_contain_text("Changing either does not accept a task")
    expect(role_guide.get_by_test_id("careflow-role-context")).to_contain_text("Current demo role: Operator")
    with page.expect_response(lambda response: response.url.endswith("/session/role")
                              and response.request.method == "POST") as changed:
        panel.get_by_label("Workflow demo role", exact=True).select_option("reviewer")
    assert changed.value.status == 200
    expect(role_guide.get_by_test_id("careflow-role-context")).to_contain_text("Current demo role: Reviewer")
    workflow_mobile = width_proof(page, 375)
    page.screenshot(path=str(output / "first-use-role-guide-mobile-375.png"), full_page=False)
    assert client.call("/api/ops/cases?limit=20")["total"] == 1
    assert sum(row["path"] == "/api/ops/cases/reconcile" for row in writes) == 1
    return {"scope": "Actual loopback fixture UI/API only; no Bedrock or clinical efficacy claim",
            "preview_mutation_requests": 0, "preview_cases": 0, "explicit_submissions": 1,
            "verified_model_mode": "fixture", "purpose_before_counts": True,
            "source_choice_and_input_action_outcome_guides": True, "role_context_updates_after_actual_role_change": True,
            "expanded_submission_guide_geometry": {"last_step_bottom": last_step_bottom,
                                                   "boundary_note_top": boundary_note_box["y"],
                                                   "gap": boundary_note_box["y"] - last_step_bottom},
            "observed_widths": {"collapsed_desktop": desktop, "mobile": mobile,
                                "preview_mobile": preview_mobile, "workflow_mobile": workflow_mobile}}


def check(url, output):
    from playwright.sync_api import expect, sync_playwright
    assert urlparse(url).hostname in {"127.0.0.1", "localhost"}, "First-use fixture checks refuse remote targets"
    assert Client(url).call("/api/health")["model_mode"] == "fixture"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname
                      in {"127.0.0.1", "localhost"} else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        try:
            proof = exercise(page, url.rstrip("/"), output, expect)
            assert not errors, "Unexpected JavaScript error in first-use journey"
            (output / "results.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
            return proof
        except Exception as error:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "failure.json").write_text(json.dumps({"error_type": type(error).__name__,
                                                           "page_error_count": len(errors)}) + "\n", encoding="utf-8")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/experience-browser"))
    args = parser.parse_args()
    process = log = None
    try:
        target = args.url or "http://127.0.0.1:8773"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_CAREFLOW_MODEL_MODE="fixture",
                               MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            environment.pop("MEDPLAIN_OPS_TABLE", None)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8773"],
                                       env=environment, stdout=log, stderr=subprocess.STDOUT)
            wait_for_server(target)
        print(json.dumps(check(target, args.output), indent=2))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log:
            log.close()
