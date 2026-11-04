"""Record one actual UI-only careflow journey in CI; native page videos retain real waits."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse

import demo_video as media
import pitch_video as audio

VIEW = {"width": 1280, "height": 720}
FROZEN = "3cbe1dc18ca4e148ca4ee1d80a6305f4fbee7571"
MAX_SECONDS = 120
NOTE = "Synthetic discharge-team review: the named follow-up contact is recorded in a separate source addendum. Keep treatment decisions with the care team."
ADDENDUM = "Pending test: potassium; owner: discharge team; follow-up: within 2 days.\nThe discharge team will contact the synthetic patient about the pending result. No medication change is recorded in this addendum."
RECHECK = "I would ask the discharge team to confirm who will contact us about the pending potassium result. I would not decide a medication change from this card."
PHASES = ("overview", "source_submit", "card", "assigned", "accepted", "reply", "child_revision", "child_submit", "closure",
          "release", "response1", "assessment1", "reviewer_follow_up", "recheck", "rag", "gate", "complete")


def load_schedule(path, expected_sha, alignment_path, alignment_sha, application_source):
    media.require(path is not None and alignment_path is not None and media.HASH.fullmatch(expected_sha or "")
                  and media.HASH.fullmatch(alignment_sha or ""), "Actual schedule and original alignment files/hashes are required")
    media.require(media.digest(path) == expected_sha and media.digest(alignment_path) == alignment_sha, "Reviewed schedule/alignment changed")
    schedule = json.loads(path.read_bytes())
    value = json.loads(alignment_path.read_bytes())
    original = value.get("alignment", value)
    text = "".join(original["characters"])
    aligned = audio.alignment_data(value, text)
    media.require(schedule["schema"] == "medplain-realtime-reading-schedule-v1" and schedule["status"] == "root_reviewed_reading_schedule"
                  and schedule["application_source"] == application_source and schedule["narration_alignment_sha256"] == alignment_sha,
                  "Actual source-bound reading schedule required")
    media.require(abs(schedule["ui_film_offset_seconds"] - 16.904) < 0.002
                  and abs(schedule["complete_after_seconds"] - 94.132) < 0.002, "Approved continuous narrative window changed")
    phases = schedule["phases"]
    media.require(tuple(row["id"] for row in phases) == PHASES, "The whole declared UI sequence must be paced")
    previous = -1
    for row in phases:
        target = row["at_seconds"]
        media.require(audio.finite_number(target) and previous < target <= schedule["complete_after_seconds"], "Invalid or unordered real reading deadline")
        if row.get("voice_char") is not None:
            index = row["voice_char"]
            media.require(type(index) is int and 0 <= index < len(text)
                          and abs(aligned["character_start_times_seconds"][index] - schedule["ui_film_offset_seconds"] - target) < 0.002,
                          "Declared voice anchor is not an actual original character timestamp")
        else:
            media.require(isinstance(row.get("reading_reason"), str) and row["reading_reason"].strip(), "Operator reading time requires an explicit reason")
        previous = target
    media.require(abs(phases[-1]["at_seconds"] - schedule["complete_after_seconds"]) < 0.002, "Missing complete real-time ending")
    return schedule


class Recorder:
    def __init__(self, output, reading_seconds, schedule=None, schedule_sha=None):
        self.output, self.reading, self.schedule, self.schedule_sha = output, reading_seconds, schedule, schedule_sha
        self.origin = time.monotonic()
        self.events, self.pages, self.contexts, self.secrets = [], [], [], []
        self.closed_rows = None
        self.phase_index = 0
        self.samples = []

    def phase(self, name, page):
        if not self.schedule:
            return
        row = self.schedule["phases"][self.phase_index]
        media.require(row["id"] == name, "A scheduled actual action was skipped or reordered")
        self.activate(page)
        arrived = self.elapsed()
        delay = max(0, row["at_seconds"] - arrived)
        self.event("operator_reading_started", page, phase=name, until_seconds=row["at_seconds"],
                   actual_arrival_seconds=arrived, lateness_seconds=max(0, arrived - row["at_seconds"]), planned_wait_seconds=delay)
        if delay:
            page.wait_for_timeout(delay * 1000)
        self.event("operator_reading_finished", page, phase=name, actual_wait_seconds=self.elapsed() - arrived)
        self.phase_index += 1

    def read_region(self, locator, label):
        self.activate(locator.page)
        locator.scroll_into_view_if_needed()
        self.event("scroll_target", locator.page, label=label)

    def elapsed(self):
        elapsed = time.monotonic() - self.origin
        media.require(elapsed < MAX_SECONDS, "Actual UI journey exceeded120seconds; do not compress or hide waits")
        return elapsed

    def event(self, kind, page=None, **data):
        row = {"seconds": self.elapsed(), "kind": kind, **data}
        if page:
            row["page"] = next(item["alias"] for item in self.pages if item["page"] is page)
            body = page.locator("body").text_content() or ""
            media.require(not any(secret and secret in body for secret in self.secrets), "A capability was painted as page text; keep recording private")
        self.events.append(row)
        (self.output / "events.json").write_bytes(media.encoded(self.events))

    def page(self, browser, alias):
        context = browser.new_context(viewport=VIEW, record_video_dir=str(self.output / "native"), record_video_size=VIEW)
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname in {"127.0.0.1", "localhost"} else route.abort())
        before = self.elapsed()
        page = context.new_page()
        page.set_default_timeout(30_000)
        self.pages.append({"page": page, "video": page.video, "alias": alias, "created_before_seconds": before,
                           "created_after_seconds": self.elapsed(), "context_index": len(self.contexts)})
        self.contexts.append(context)
        return page

    def activate(self, page):
        page.bring_to_front()
        self.event("active_page", page)

    def click(self, locator, label):
        page = locator.page
        self.activate(page)
        locator.scroll_into_view_if_needed()
        bounds = locator.bounding_box()
        media.require(bounds is not None, "Actual click target is not visible")
        point = {"x": bounds["x"] + bounds["width"] / 2, "y": bounds["y"] + bounds["height"] / 2}
        page.mouse.move(point["x"], point["y"])
        self.event("click_target", page, label=label, target=point)
        locator.click()

    def fill(self, locator, text, label):
        self.activate(locator.page)
        self.event("fill", locator.page, label=label, characters=len(text))
        locator.fill(text)

    def select(self, locator, value, label):
        self.activate(locator.page)
        self.event("select", locator.page, label=label)
        locator.select_option(value)

    def confirm(self, locator, label):
        self.activate(locator.page)
        self.event("check", locator.page, label=label)
        locator.check()

    def mark(self, name, page, read=True):
        self.activate(page)
        self.event("milestone", page, name=name)
        media.require(page.locator(".toast-error").count() == 0, "Keep actual error notification visible; capture must fail")
        success = page.locator(".toast:not(.toast-error)")
        for _ in range(4):
            if success.count() == 0:
                break
            toast = success.nth(0)
            self.event("dismiss_success_notification", page, message=toast.locator(":scope > span").inner_text())
            self.click(toast.get_by_role("button", name="Dismiss notification", exact=True), "Dismiss completed action notification")
        media.require(success.count() == 0, "Notifications still obstruct the actual recording")
        media.require(re.fullmatch(r"[a-z][a-z0-9_]{0,55}", name), "Unsafe milestone sample name")
        directory = self.output / "milestones"
        directory.mkdir(exist_ok=True)
        sample = directory / (name + ".png")
        media.require(not sample.exists(), "Preserve previous actual milestone pixels")
        page.screenshot(path=str(sample), full_page=False)
        self.samples.append({"milestone": name, "page": next(item["alias"] for item in self.pages if item["page"] is page),
                             "seconds": self.elapsed(), "image": "milestones/" + sample.name, "sha256": media.digest(sample),
                             "representation": "Actual native viewport screenshot at this recorded milestone; no DOM rewrite or fabricated result."})
        if read and self.reading:
            self.event("operator_reading_started", page, deliberate_seconds=self.reading)
            page.wait_for_timeout(self.reading * 1000)
            self.event("operator_reading_finished", page)

    def close(self):
        if self.closed_rows is not None:
            return self.closed_rows
        brackets = {}
        for index, context in enumerate(self.contexts):
            before = time.monotonic() - self.origin
            context.close()
            brackets[index] = {"closed_before_seconds": before, "closed_after_seconds": time.monotonic() - self.origin}
        rows = []
        for item in self.pages:
            path = Path(item["video"].path())
            media.require(path.resolve().is_relative_to((self.output / "native").resolve()), "Native video escaped recording directory")
            rows.append({key: value for key, value in item.items() if key not in {"page", "video"}}
                        | {"native_video": "native/" + path.name, "sha256": media.digest(path), "ffprobe": media.probe(path),
                           **brackets[item["context_index"]]})
        self.closed_rows = rows
        return rows


def post_by_ui(page, suffix, action, status):
    with page.expect_response(lambda response: response.url.endswith(suffix) and response.request.method == "POST") as accepted:
        action()
    media.require(accepted.value.status == status, "Actual UI mutation did not return its required status")
    return accepted.value


def journey(rec, browser, url, expected_source, expect):
    from console_browser_smoke import session_client
    from reconciliation_browser_smoke import audit_detail
    from ops_live_smoke import Client, wait_case
    page = rec.page(browser, "console")
    rec.activate(page)
    page.goto(url + "/console/overview?collection=empty", wait_until="networkidle")
    client = session_client(page, url)
    health = Client(url).call("/api/health")
    assert health["commit"] == expected_source and health["model_mode"] == "fixture"
    assert client.call("/api/ops/cases?limit=20")["total"] == 0
    rec.mark("overview", page)
    rec.phase("overview", page)
    rec.click(page.get_by_test_id("careflow-entry").get_by_role("button", name="Preview coordinated follow-up", exact=True), "Preview coordinated follow-up")
    form = page.get_by_role("dialog", name="Reconcile synthetic source packet", exact=True)
    expect(form.get_by_test_id("careflow-preview-notice")).to_be_visible()
    expect(form.get_by_label("Complex synthetic example", exact=True)).to_have_value("pending-result-gap")
    expect(form.get_by_test_id("start-reconciliation")).to_be_disabled()
    rec.mark("source_preview_no_job", page)
    rec.phase("source_submit", page)
    rec.confirm(form.get_by_label("Confirm invented reconciliation sources", exact=True), "Confirm invented original source packet")
    response = post_by_ui(page, "/api/ops/cases/reconcile", lambda: rec.click(form.get_by_test_id("start-reconciliation"), "Submit original source verification"), 202)
    parent_id, dossier = response.json()["case"]["id"], response.request.post_data_json["dossier"]
    rec.mark("original_verification_wait", page, read=False)
    parent = wait_case(client, parent_id)
    parent_proof = audit_detail(parent, dossier, "fixture", expected_source)
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer.locator(".drawer-meta .badge")).to_have_text("Review required", timeout=30_000)
    rec.phase("card", page)
    rec.click(drawer.get_by_role("tab", name="Clarification", exact=True), "Read verified clarification card")
    expect(drawer.get_by_test_id("regimen-withheld")).to_be_visible()
    rec.mark("verified_source_card", page)
    rec.click(drawer.get_by_role("button", name="Open clarification workspace", exact=True), "Open clarification workspace")
    panel = drawer.get_by_test_id("careflow-panel")
    flow_path = f"/api/ops/cases/{parent_id}/careflow"
    flow = client.call(flow_path)
    task = next(row for row in flow["tasks"] if row["code"] == "confirm_pending_owner")
    rec.click(panel.get_by_test_id("careflow-task-" + task["id"]), "Select pending-result owner question")
    rec.phase("assigned", page)
    rec.select(panel.get_by_label("Act as synthetic staff", exact=True), "demo-pharmacist", "Select synthetic pharmacist actor")
    rec.select(panel.get_by_label("Assign synthetic staff", exact=True), "demo-pharmacist", "Assign synthetic pharmacist")
    post_by_ui(page, "/careflow/assign", lambda: rec.click(panel.get_by_role("button", name="Assign follow-up", exact=True), "Assign follow-up"), 200)
    rec.mark("assigned", page)
    rec.select(panel.get_by_label("Workflow demo role", exact=True), "reviewer", "Choose reviewer demo role")
    rec.phase("accepted", page)
    post_by_ui(page, "/careflow/accept", lambda: rec.click(panel.get_by_role("button", name="Accept responsibility", exact=True), "Explicitly accept responsibility"), 200)
    rec.mark("accepted", page)
    rec.phase("reply", page)
    rec.fill(panel.get_by_label("Source-bound clarification note", exact=True), NOTE, "Record source-bound clarification note")
    post_by_ui(page, "/careflow/answer", lambda: rec.click(panel.get_by_role("button", name="Record clarification note", exact=True), "Submit source-bound reply"), 200)
    rec.mark("source_bound_reply", page)
    rec.phase("child_revision", page)
    rec.select(panel.get_by_label("Workflow demo role", exact=True), "operator", "Choose operator to record new source")
    rec.click(panel.get_by_role("button", name="Prepare clarified source revision", exact=True), "Prepare separate child source revision")
    revision = page.get_by_role("dialog", name="Revise sources and reverify", exact=True)
    assert len(dossier["documents"]) == 2
    expect(revision.get_by_test_id("reconciliation-source-1")).to_have_value(dossier["documents"][0]["text"])
    rec.click(revision.get_by_role("button", name="Add original source", exact=True), "Add actual source addendum field")
    rec.fill(revision.get_by_label("Source 3 identifier", exact=True), "clarified-owner", "Name the new addendum")
    rec.select(revision.get_by_label("Source 3 kind", exact=True), "addendum", "Classify new source as addendum")
    issued = revision.get_by_label("Source 3 issued time UTC", exact=True)
    rec.fill(issued, "2026-10-30T12:00", "Record original addendum issue time")
    expect(issued).to_have_value("2026-10-30T12:00")
    rec.fill(revision.get_by_label("Source 3 original text", exact=True), ADDENDUM, "Record synthetic source clarification")
    rec.mark("new_source_addendum", page)
    rec.phase("child_submit", page)
    rec.confirm(revision.get_by_label("Confirm invented reconciliation sources", exact=True), "Confirm revised sources are invented")
    revised = post_by_ui(page, "/api/ops/cases/reconcile", lambda: rec.click(revision.get_by_test_id("start-reconciliation"), "Submit independent child verification"), 202)
    child_id, child_source = revised.json()["case"]["id"], revised.request.post_data_json["dossier"]
    assert revised.request.post_data_json["parent_case_id"] == parent_id
    assert child_source["documents"][:2] == dossier["documents"] and child_source["documents"][2]["text"] == ADDENDUM
    assert child_source["documents"][2]["issued_at"] == "2026-10-30T12:00:00Z"
    rec.mark("child_verification_wait", page, read=False)
    child = wait_case(client, child_id)
    child_proof = audit_detail(child, child_source, "fixture", expected_source)
    expect(drawer.locator(".drawer-meta .badge")).to_have_text("Review required", timeout=30_000)
    rec.click(drawer.get_by_role("button", name="Close dialog", exact=True), "Return to case register")
    parent_row = page.get_by_role("row").filter(has_text=parent_id[:12])
    expect(parent_row).to_have_count(1)
    rec.click(parent_row, "Open original parent from actual case register")
    rec.click(drawer.get_by_role("button", name="Open clarification workspace", exact=True), "Return to parent clarification task")
    panel = drawer.get_by_test_id("careflow-panel")
    rec.select(panel.get_by_label("Workflow demo role", exact=True), "reviewer", "Choose reviewer to inspect verified child")
    rec.click(panel.get_by_test_id("careflow-task-" + task["id"]), "Select original answered question")
    rec.select(panel.get_by_label("Separately verified source revision", exact=True), child_id, "Select the verified direct child")
    rec.phase("closure", page)
    post_by_ui(page, "/careflow/link-revision", lambda: rec.click(panel.get_by_role("button", name="Link verified revision", exact=True), "Link exact verified source revision"), 200)
    rec.confirm(panel.get_by_label("I reviewed this verified revision", exact=False), "Confirm synthetic revision review")
    post_by_ui(page, "/careflow/close", lambda: rec.click(panel.get_by_role("button", name="Confirm resolution and close", exact=True), "Record explicit demo closure"), 200)
    expect(panel.get_by_text("Resolution recorded", exact=True)).to_be_visible()
    after = client.call(f"/api/ops/cases/{parent_id}")
    assert after["source"] == dossier and after["source_hash"] == parent["source_hash"] and after["artifact_hash"] == parent["artifact_hash"]
    rec.mark("verified_child_closure", page)
    rec.click(panel.get_by_role("button", name="Open linked verified revision", exact=True), "Open linked child for separate release")
    rec.click(drawer.get_by_role("button", name="Open clarification workspace", exact=True), "Open child workflow")
    panel = drawer.get_by_test_id("careflow-panel")
    rec.click(panel.get_by_role("button", name="Go to artifact release review", exact=True), "Review exact child artifact release")
    rec.fill(drawer.get_by_label("Review comment", exact=True), "Release the synthetic source-bound clarification card only. Medication regimen remains withheld and open issues remain open.", "Record exact-artifact review")
    rec.phase("release", page)
    post_by_ui(page, f"/cases/{child_id}/review", lambda: rec.click(drawer.get_by_test_id("release-case"), "Release verified child card"), 200)
    panel = drawer.get_by_test_id("careflow-panel")
    review = panel.get_by_test_id("teachback-review")
    rec.click(review.get_by_role("button", name="Enable synthetic patient responses", exact=True), "Issue separate private response access")
    link = review.get_by_role("link", name="Open private synthetic response view", exact=True)
    expect(link).to_be_visible()
    private_url = urljoin(url, link.get_attribute("href"))
    assert urlparse(private_url).hostname in {"127.0.0.1", "localhost"}
    capability = parse_qs(urlparse(private_url).fragment)["teachback"][0]
    rec.secrets.append(capability)
    rec.mark("child_released_private_link_issued", page)
    patient = rec.page(browser, "patient")
    patient_headers = []
    def inspect_patient(request):
        if "/api/ops/careflow/patient/" in request.url:
            headers = request.all_headers()
            patient_headers.append({"method": request.method, "response_capability_present": bool(headers.get("x-medplain-teachback")),
                                    "console_session_present": "x-medplain-session" in headers})
    patient.on("request", inspect_patient)
    rec.activate(patient)
    try:
        patient.goto(private_url, wait_until="networkidle")
    except Exception:
        raise AssertionError("Private patient navigation failed; capability omitted") from None
    assert not urlparse(patient.url).fragment
    expect(patient.get_by_text("Released from a new offline fixture verification run.", exact=True)).to_be_visible()
    rec.mark("released_patient_view", patient)
    rec.click(patient.get_by_role("link", name="Questions and response access", exact=True), "Open patient response section")
    feedback = patient.get_by_test_id("patient-teachback")
    rec.phase("response1", patient)
    rec.fill(feedback.get_by_label("Your synthetic response", exact=True), "Yes, I understand.", "Write first invented own-word response")
    rec.confirm(feedback.get_by_label("This is wholly invented", exact=False), "Confirm response is synthetic")
    post_by_ui(patient, "/responses", lambda: rec.click(feedback.get_by_role("button", name="Submit synthetic response", exact=True), "Submit actual first response"), 202)
    first = feedback.get_by_role("list", name="Teach-back attempt history", exact=True).locator(":scope > li").filter(has=patient.get_by_text("Response 1", exact=True))
    expect(first.locator(".careflow-status")).to_have_text("Assessed", timeout=30_000)
    expect(first.get_by_text("Provisional assessment, reviewer required", exact=True)).to_be_visible()
    rec.activate(page)
    rec.click(review.get_by_role("button", name="Refresh patient responses", exact=True), "Read actual response in reviewer view")
    flow = client.call(f"/api/ops/cases/{child_id}/careflow")
    assert len(flow["teachback"]["attempts"]) == 1 and flow["teachback"]["attempts"][0]["status"] == "assessed"
    private_first = review.get_by_role("list", name="Teach-back attempt history", exact=True).locator(":scope > li").filter(has=page.get_by_text("Response 1", exact=True))
    rec.read_region(private_first.locator(".careflow-facts"), "Read the actual provisional covered/missing/misunderstood partition")
    rec.phase("assessment1", page)
    rec.mark("first_provisional_assessment", page)
    rec.phase("reviewer_follow_up", page)
    rec.select(review.get_by_label("Response awaiting reviewer", exact=True), flow["teachback"]["attempts"][0]["id"], "Select actual first response")
    rec.select(review.get_by_label("Review decision", exact=True), "follow_up", "Reviewer requests re-explanation")
    rec.fill(review.get_by_label("Private reviewer comment", exact=True), "The acknowledgement did not demonstrate understanding. Re-explain only the approved source and request another synthetic response.", "Record reviewer follow-up")
    post_by_ui(page, f"/teachback/{flow['teachback']['attempts'][0]['id']}/review", lambda: rec.click(review.get_by_role("button", name="Record teach-back review", exact=True), "Record real reviewer follow-up"), 200)
    rec.mark("reviewer_follow_up", page)
    rec.activate(patient)
    rec.phase("recheck", patient)
    rec.click(feedback.get_by_role("button", name="Refresh response history", exact=True), "Read recorded follow-up in patient view")
    expect(feedback.get_by_text("Reviewer recorded follow-up", exact=False)).to_be_visible()
    rec.fill(feedback.get_by_label("Your synthetic response", exact=True), RECHECK, "Write distinct invented recheck")
    rec.confirm(feedback.get_by_label("This is wholly invented", exact=False), "Confirm recheck is synthetic")
    post_by_ui(patient, "/responses", lambda: rec.click(feedback.get_by_role("button", name="Submit synthetic recheck", exact=True), "Submit actual second response"), 202)
    second = feedback.get_by_role("list", name="Teach-back attempt history", exact=True).locator(":scope > li").filter(has=patient.get_by_text("Response 2", exact=True))
    expect(second.locator(".careflow-status")).to_have_text("Assessed", timeout=30_000)
    expect(second.locator(":scope > blockquote")).to_have_text(RECHECK)
    expect(second.get_by_text("Provisional assessment, reviewer required", exact=True)).to_be_visible()
    rec.read_region(second.locator(".careflow-feedback > strong"), "Read the actual second response and provisional assessment")
    rec.mark("second_response_and_provisional_assessment", patient)
    rec.activate(page)
    rec.phase("rag", page)
    evidence = panel.get_by_test_id("careflow-evidence")
    rec.fill(evidence.get_by_label("Evidence question", exact=True), "pending test responsibility accepted transfer", "Ask a general educational workflow question")
    rec.click(evidence.get_by_role("button", name="Search reviewed evidence", exact=True), "Retrieve actual versioned educational passage")
    expect(evidence.get_by_role("link", name="Clarification responsibility workflow", exact=True)).to_be_visible()
    rec.read_region(evidence.locator('.careflow-evidence > article:has(a[href="https://www.ahrq.gov/patient-safety/settings/hospital/match/chapter-3.html"])'), "Read the actual AHRQ publisher, title and quotation")
    rec.mark("actual_targeted_educational_retrieval", page)
    if rec.schedule:
        rec.phase("gate", page)
        rec.click(drawer.get_by_role("tab", name="Safety", exact=True), "Inspect the actual child verification receipt")
        expect(drawer.locator(".receipt-card.verified")).to_have_count(1)
        rec.read_region(drawer.locator(".receipt-card.verified"), "Read the actual verified receipt; no regimen is authorized")
        rec.mark("actual_strict_gate_receipt", page, read=False)
        rec.phase("complete", page)
        assert rec.phase_index == len(PHASES)
    final = client.call(f"/api/ops/cases/{child_id}/careflow")
    assert len(final["teachback"]["attempts"]) == 2
    assert all(row["assessment"]["requires_reviewer"] is True for row in final["teachback"]["attempts"])
    assert client.call(f"/api/ops/cases/{child_id}")["result"]["patient_regimen"] is None
    assert patient_headers and all(row["response_capability_present"] and not row["console_session_present"] for row in patient_headers)
    return {"parent": parent_proof, "child": child_proof, "original_parent_unchanged": True,
            "all_mutations_via_actual_ui": True, "actual_patient_responses": 2, "clinical_benefit_measured": False,
            "model_mode": "fixture", "provider_calls": 0, "regimen_withheld": True,
            "patient_header_privacy": patient_headers}


def run(app, expected, output, reading_seconds, schedule=None, schedule_sha=None):
    media.require(not output.exists() and re.fullmatch(r"[0-9a-f]{40}", expected), "New source-bound recording directory required")
    media.require(0 <= reading_seconds <= 4, "Reading pauses are bounded and remain real captured time")
    identity = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=app, text=True).strip()
    media.require(identity == expected, "Wrong application checkout")
    output.mkdir(parents=True)
    (output / "native").mkdir()
    sys.path.insert(0, str(app / "scripts"))
    from browser_smoke import wait_for_server
    from playwright.sync_api import expect, sync_playwright
    environment = dict(os.environ, MEDPLAIN_COMMIT=expected, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_CAREFLOW_MODEL_MODE="fixture",
                       MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
    environment.pop("MEDPLAIN_OPS_TABLE", None)
    with (output / "fixture-server.log").open("xb") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8779"], cwd=app, env=environment, stdout=log, stderr=subprocess.STDOUT)
        recorder = None
        try:
            wait_for_server("http://127.0.0.1:8779")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                media.require(not schedule or reading_seconds == 0, "Do not stack fixed pauses on the reviewed reading schedule")
                recorder = Recorder(output, reading_seconds, schedule, schedule_sha)
                try:
                    result = journey(recorder, browser, "http://127.0.0.1:8779", expected, expect)
                    elapsed = recorder.elapsed()
                    pages = recorder.close()
                    proof = {"schema": "medplain-realtime-ui-v1", "status": "completed_actual_ui_fixture_journey_pending_video_privacy_and_pixel_review",
                             "application_source": expected, "recorder_sha256": media.digest(Path(__file__)), "wall_seconds": elapsed,
                             "reading_pause_seconds_per_milestone": reading_seconds, "page_videos": pages, "events": recorder.events,
                             "reading_schedule_sha256": schedule_sha, "reading_schedule": schedule,
                             "actual_milestone_samples": recorder.samples,
                             "journey": result, "timeline_policy": "One monotonic real-time UI journey. Preserve all waits and actual page switches. No speed changes or hidden gaps.",
                             "clock_sync_claim": "Monotonic creation and context-close brackets only; measure native video origin independently before composition. No subframe synchronization claimed.",
                             "raw_recording_private_until_review": True, "public_upload_authorized": False}
                    (output / "recording-proof.json").write_bytes(media.encoded(proof))
                    return proof
                except Exception as error:
                    pages = recorder.close()
                    (output / "failure.json").write_bytes(media.encoded({"status": "failed_actual_ui_capture_preserve_private_raws", "error_type": type(error).__name__, "page_videos": pages, "events": recorder.events}))
                    raise
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reading-seconds", type=float, default=0)
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--schedule-sha256")
    parser.add_argument("--alignment", type=Path)
    parser.add_argument("--alignment-sha256")
    args = parser.parse_args()
    selected = load_schedule(args.schedule, args.schedule_sha256, args.alignment, args.alignment_sha256, args.expected_source) if args.schedule else None
    print(json.dumps(run(args.app.resolve(), args.expected_source, args.output.resolve(), args.reading_seconds, selected, args.schedule_sha256)))
