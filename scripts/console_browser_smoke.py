"""Exercise the real console, asynchronous SDK case, review and patient view.

No case API responses are substituted. One explicit HTTP 503 read is injected
after the successful journey to prove the UI exposes and recovers from errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_smoke import wait_for_server
from demo_collection_smoke import audit_prepared_collection
from ops_live_smoke import Client


def contrast_ratio(foreground: str, background: str) -> float:
    """Measure explicitly computed native-option colors, including alpha refusal."""
    def luminance(color):
        assert color.startswith(("rgb(", "rgba(", "color(srgb")), "native option colors must resolve to RGB"
        values = [float(value) for value in re.findall(r"[\d.]+", color)]
        assert len(values) in {3, 4}, "native option colors must resolve to RGB"
        assert len(values) != 4 or values[3] == 1, "native option colors must be opaque"
        scale = 1 if color.startswith("color(srgb") else 255
        channels = [value / scale for value in values[:3]]
        assert all(0 <= value <= 1 for value in channels)
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return sum(channel * weight for channel, weight in zip(linear, [0.2126, 0.7152, 0.0722], strict=True))

    left, right = sorted([luminance(foreground), luminance(background)])
    return (right + 0.05) / (left + 0.05)


def session_client(page, url: str) -> Client:
    client = Client(url)
    client.token = page.evaluate("localStorage.getItem('medplain.session.v2')")
    assert client.token, "the browser must receive its own anonymous session"
    return client


def exercise_prepared_collection(page, url: str, output: Path, expect) -> dict:
    expect(page.get_by_test_id("prepared-demo-notice")).to_be_visible()
    proof = audit_prepared_collection(session_client(page, url))
    expect(page.locator(".kpi").filter(has_text="Cases in session").locator("strong")).to_have_text("12")
    expect(page.get_by_test_id("outcomes-chart")).to_be_visible()
    expect(page.get_by_test_id("desk-load-chart")).to_be_visible()
    page.screenshot(path=str(output / "prepared-overview-desktop.png"), full_page=True)
    page.get_by_test_id("nav-cases").click()
    expect(page.get_by_test_id("case-origin").first).to_contain_text("Prepared")
    expect(page.get_by_role("button", name="Next page", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Next page", exact=True).click()
    expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
    expect(page.locator('[data-testid="case-origin"]:visible')).to_have_count(2)
    expect(page.get_by_role("button", name="Next page", exact=True)).to_be_disabled()
    page.screenshot(path=str(output / "prepared-cases-page-2.png"), full_page=True)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(url.rstrip("/") + "/console/overview", wait_until="networkidle")
    expect(page.get_by_test_id("prepared-demo-notice")).to_be_visible()
    expect(page.get_by_test_id("outcomes-chart")).to_be_visible()
    expect(page.get_by_test_id("desk-load-chart")).to_be_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "prepared overview overflows at 375px"
    page.screenshot(path=str(output / "prepared-overview-mobile-375.png"), full_page=True)
    return proof


def exercise_native_options(page, url: str, output: Path, expect) -> list[dict]:
    proof = []
    for width in [1440, 375]:
        page.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 812})
        for theme in ["dark", "light"]:
            page.goto(url.rstrip("/") + "/console/settings", wait_until="networkidle")
            page.get_by_label("Theme", exact=True).select_option(theme)
            expect(page.locator("html")).to_have_attribute("data-theme", theme)
            styles = page.locator("select:visible").evaluate_all("""elements => elements.flatMap(select =>
              Array.from(select.options).map(option => ({
                label: select.getAttribute('aria-label') || select.name || 'native select',
                colorScheme: getComputedStyle(select).colorScheme,
                foreground: getComputedStyle(option).color,
                background: getComputedStyle(option).backgroundColor
              })))""")
            assert styles, "native selects must be present in settings"
            ratios = []
            for style in styles:
                assert style["colorScheme"] == theme, "native menu must use the selected theme's color scheme"
                ratios.append(contrast_ratio(style["foreground"], style["background"]))
            assert min(ratios) >= 4.5, "native option foreground and background must remain readable"
            workspace = page.get_by_label("Workspace", exact=True)
            workspace.click()  # Open the real native popup before capturing it.
            filename = f"native-popup-{theme}-{width}.png"
            page.screenshot(path=str(output / filename))
            page.keyboard.press("Home")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            expect(workspace).to_have_value("demo-athens")
            workspace.click()
            page.keyboard.press("Home")
            page.keyboard.press("Enter")
            expect(workspace).to_have_value("all")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            proof.append({"width": width, "theme": theme, "minimum_computed_contrast": round(min(ratios), 3),
                          "native_popup_screenshot": filename, "keyboard_selection": True,
                          "scope": "CI Chromium native interaction and computed colors; Windows appearance needs its own visual check."})
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(url.rstrip("/") + "/console/settings", wait_until="networkidle")
    page.get_by_label("Theme", exact=True).select_option("dark")
    return proof


def start_empty_session(page, url: str, output: Path, expect) -> None:
    page.goto(url.rstrip("/") + "/console/settings", wait_until="networkidle")
    with page.expect_response(lambda res: res.url.endswith("/api/ops/session") and res.request.method == "POST") as created:
        page.get_by_test_id("start-empty-session").click()
    assert created.value.status == 201 and created.value.request.post_data_json == {"prepared_demo": False}
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    client = session_client(page, url)
    boot = client.call("/api/ops/bootstrap")
    assert boot["collection"]["mode"] == "empty" and boot["collection"]["initialized"] is True
    assert client.call("/api/ops/cases?limit=20")["total"] == 0
    page.reload(wait_until="networkidle")
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="See the safety gate stop a changed dose.", exact=True)).to_be_visible()
    assert session_client(page, url).call("/api/ops/cases?limit=20")["total"] == 0, "explicit empty sessions must survive reload without auto-seeding"
    page.screenshot(path=str(output / "explicit-empty-session.png"), full_page=True)


def change_role(page, role: str, expect) -> None:
    page.get_by_role("button", name="Open user menu").click()
    with page.expect_response(lambda response: response.url.endswith("/api/ops/session/role") and response.request.method == "POST") as changed:
        page.get_by_label("Synthetic demo role", exact=True).select_option(role)
    assert changed.value.status == 200
    expect(page.get_by_label("Synthetic demo role", exact=True)).to_have_value(role)
    page.get_by_role("button", name="Close menu", exact=True).click()


def exercise(page, browser, url: str, output: Path, expect) -> dict:
    response = page.goto(url.rstrip("/") + "/", wait_until="networkidle")
    assert response and response.status == 200
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible(timeout=30_000)
    prepared = exercise_prepared_collection(page, url, output, expect)
    native_options = exercise_native_options(page, url, output, expect)
    start_empty_session(page, url, output, expect)
    expect(page.get_by_role("button", name="New case", exact=True)).to_be_enabled()
    page.screenshot(path=str(output / "overview-desktop.png"), full_page=True)
    page.get_by_role("button", name="New case", exact=True).click()
    intake = page.get_by_role("dialog", name="New verification case", exact=True)
    expect(intake).to_be_visible()
    intake.get_by_label("Discharge packet", exact=True).select_option("dosage-mutation-stress")
    intake.locator('input[type="checkbox"]').check()
    with page.expect_response(lambda res: res.url.endswith("/api/ops/cases") and res.request.method == "POST") as created:
        intake.get_by_test_id("start-verification").click()
    assert created.value.status == 202
    case_id = created.value.json()["case"]["id"]
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer).to_be_visible()
    expect(drawer.locator(".drawer-meta .badge")).to_have_text("Review required", timeout=240_000)
    session_token = page.evaluate("localStorage.getItem('medplain.session.v2')")
    read = page.request.get(url.rstrip("/") + f"/api/ops/cases/{case_id}", headers={"X-Medplain-Session": session_token})
    assert read.status == 200
    detail = read.json()
    assert detail["origin"] == "live_run", "the real stress journey must not use a prepared result"
    assert detail["result"]["status"] == "verified" and detail["correction_count"] > 0
    assert any(receipt["verdict"] == "refused" for receipt in detail["result"]["receipts"])
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        assert detail["result"]["model_mode"] == "bedrock"
    drawer.get_by_role("tab", name="Safety", exact=True).click()
    expect(drawer.locator(".receipt-card.refused").first).to_be_visible()
    expect(drawer.locator(".receipt-card.verified").first).to_be_visible()
    page.screenshot(path=str(output / "safety-refusal-and-correction.png"), full_page=True)
    drawer.get_by_role("tab", name="Agents", exact=True).click()
    for agent in ["LabAndDischargeParserAgent", "DrugAndContraindicationVerifierAgent", "PatientAdvocateExplainerAgent"]:
        expect(drawer.get_by_text(agent, exact=True).first).to_be_visible()
    drawer.get_by_role("tab", name=re.compile(r"^Receipt")).click()
    with page.expect_download() as downloaded:
        drawer.get_by_role("button", name="Download receipts", exact=True).click()
    downloaded.value.save_as(output / "console-receipts.json")
    with page.expect_response(lambda res: res.url.endswith("/api/receipts/verify") and res.request.method == "POST") as checked:
        drawer.get_by_role("button", name="Verify chain", exact=True).click()
    assert checked.value.json()["valid"] is True
    expect(drawer.get_by_test_id("release-case")).to_be_disabled()
    expect(drawer.get_by_text("Switch to the reviewer or admin synthetic demo role to decide.", exact=True)).to_be_visible()
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "reviewer", expect)
    page.goto(url.rstrip("/") + f"/console/cases?case={case_id}", wait_until="networkidle")
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer).to_be_visible()
    drawer.get_by_label("Review comment", exact=True).fill("Reviewed the synthetic source, refusal, correction and exact accepted hashes.")
    with page.expect_response(lambda res: res.url.endswith(f"/cases/{case_id}/review") and res.request.method == "POST") as released:
        drawer.get_by_test_id("release-case").click()
    submitted = released.value.request.post_data_json
    assert submitted["source_hash"] == detail["source_hash"]
    assert submitted["artifact_hash"] == detail["artifact_hash"]
    assert submitted["expected_version"] == detail["version"]
    assert released.value.status == 200
    expect(drawer.get_by_role("link", name="Open patient view")).to_be_visible()
    patient_url = drawer.get_by_role("link", name="Open patient view").get_attribute("href")
    if patient_url.startswith("/"):
        patient_url = url.rstrip("/") + patient_url
    patient_context = browser.new_context(viewport={"width": 1280, "height": 900})
    patient = patient_context.new_page()
    patient.goto(patient_url, wait_until="networkidle")
    expect(patient.get_by_role("heading", name="Your medicines", exact=True)).to_be_visible(timeout=30_000)
    expect(patient.get_by_text("Read-only patient view", exact=True)).to_be_visible()
    patient.screenshot(path=str(output / "anonymous-patient-view.png"), full_page=True)
    patient_context.close()
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    page.get_by_test_id("nav-maps").click()
    expect(page.get_by_role("heading", name="Execution & facility maps", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Workflow", exact=True).click()
    page.get_by_role("button", name="Inspect Atropos safety gate", exact=True).click()
    expect(page.get_by_role("heading", name="Atropos safety gate", exact=True)).to_be_visible()
    page.screenshot(path=str(output / "workflow-map.png"), full_page=True)
    page.get_by_role("tab", name="Facilities", exact=True).click()
    geographic_map = page.get_by_test_id("facility-map")
    expect(geographic_map).to_be_visible()
    expect(geographic_map.locator("path.geographic-country[data-country='GRC']")).to_be_visible()
    expect(geographic_map.locator("path.geographic-country[data-country='GRC']")).to_have_attribute("d", re.compile(r"^M"))
    expect(geographic_map.locator(".facility-marker")).to_have_count(3)
    expect(geographic_map.locator("img.leaflet-tile")).to_have_count(0)
    expect(geographic_map.get_by_text("Natural Earth", exact=True)).to_be_visible()
    page.screenshot(path=str(output / "facility-map.png"), full_page=True)
    geographic_map.locator(f".facility-marker[data-facility-id='{detail['facility_id']}']").click()
    expect(geographic_map.locator(".leaflet-popup-content")).to_contain_text("1 cases in current session")
    geographic_map.get_by_role("button", name="Open desk cases", exact=True).click()
    expect(page).to_have_url(re.compile(r"workspace=" + re.escape(detail["facility_id"])))
    expect(page.get_by_role("row", name=f"Open {detail['packet_title']}", exact=True)).to_be_visible()
    page.get_by_test_id("nav-cases").click()
    page.get_by_label("Search all session cases", exact=True).fill("tenfold")
    row = page.get_by_role("row", name=f"Open {detail['packet_title']}", exact=True)
    expect(row).to_be_visible()
    row.focus()
    row.press("Enter")
    expect(page.get_by_role("dialog", name="Case details", exact=True)).to_be_visible()
    page.get_by_role("dialog", name="Case details").get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "operator", expect)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(url.rstrip("/") + "/console/overview", wait_until="networkidle")
    primary = page.get_by_role("button", name="New case", exact=True)
    expect(primary).to_be_enabled()
    box = primary.bounding_box()
    assert box and 0 <= box["y"] and box["y"] + box["height"] <= 812, "mobile initial viewport must expose the primary action"
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), "console overflows at 375px"
    page.screenshot(path=str(output / "console-mobile-375-initial.png"))
    primary.click()
    expect(page.get_by_role("dialog", name="New verification case")).to_be_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), "mobile intake dialog overflows"
    page.screenshot(path=str(output / "console-mobile-375-dialog.png"))
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="Open navigation", exact=True).click()
    page.locator('[data-testid="nav-cases"]:visible').click()
    expect(page.locator(".mobile-case").first).to_be_visible()
    page.locator(".mobile-case").first.click()
    expect(page.get_by_role("dialog", name="Case details")).to_be_visible()
    page.screenshot(path=str(output / "console-mobile-375-case.png"))
    page.keyboard.press("Escape")
    page.route("**/api/ops/overview?*", lambda route: route.fulfill(status=503, json={"detail": "Injected read failure for the browser check."}))
    page.goto(url.rstrip("/") + "/console/overview", wait_until="networkidle")
    expect(page.locator(".api-banner")).to_contain_text("Injected read failure")
    page.screenshot(path=str(output / "console-api-error.png"))
    page.unroute("**/api/ops/overview?*")
    page.locator(".api-banner").get_by_role("button", name="Retry read", exact=True).click()
    expect(page.locator(".api-banner")).to_have_count(0)
    return {"url": url, "case_id": case_id, "model_mode": detail["result"]["model_mode"],
            "correction_count": detail["correction_count"], "artifact_hash": detail["artifact_hash"],
            "released_exact_hashes": True, "anonymous_patient_view": True,
            "workflow_and_facility_maps": True, "mobile_width": 375, "api_error_recovery": True,
            "prepared_collection": prepared, "native_options": native_options, "explicit_empty_reload": True}


def check(url: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        host = urlparse(url).hostname
        external_requests = []
        context.on("request", lambda request: external_requests.append(request.url)
                   if urlparse(request.url).hostname not in {host, None} else None)
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == host else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            result = exercise(page, browser, url, output, expect)
            assert not external_requests, f"console requested an external dependency: {external_requests}"
            assert not errors, f"console JavaScript errors: {errors}"
            (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        except Exception:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/console-browser"))
    args = parser.parse_args()
    process = None
    log = None
    try:
        url = args.url or "http://127.0.0.1:8766"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8766"], env=environment, stdout=log, stderr=subprocess.STDOUT)
            wait_for_server(url)
        result = check(url, args.output)
        if not args.url:
            from ops_live_smoke import smoke

            instrument = smoke(url, os.getenv("MEDPLAIN_COMMIT"), allow_fixture=True)
            (args.output / "local-ops.json").write_text(json.dumps(instrument, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if log:
            log.close()
