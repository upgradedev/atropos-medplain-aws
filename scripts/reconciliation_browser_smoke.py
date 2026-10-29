"""Exercise real multisource SDK verification, clarification release and revision.

Source, queue and result responses are never substituted for success. The
explicitly labelled 413 injection checks only form recovery. The simulated
device voice checks manual controls, not speech quality or native audio output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from browser_smoke import wait_for_server
from console_browser_smoke import change_role, session_client
from live_smoke import canonical
from ops_live_smoke import Client, wait_case

PROFILE = "medplain-reconciliation-v1"
AGENTS = ["DischargeEvidenceIndexerAgent", "CrossDocumentClarificationPlannerAgent", "ClarificationCardPublisherAgent"]


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def assert_patient_width(page, output: Path) -> None:
    """Retain the actual patient window and measured offenders before closing it."""
    measured = page.evaluate("""() => {
      const width = innerWidth;
      const offenders = Array.from(document.querySelectorAll('body *')).flatMap(element => {
        const rect = element.getBoundingClientRect(); const style = getComputedStyle(element);
        if (!rect.width || style.display === 'none' || style.visibility === 'hidden' || (rect.right <= width + 0.5 && rect.left >= -0.5)) return [];
        const parents = []; let node = element;
        for (let index = 0; node && index < 5; index++, node = node.parentElement) {
          const box = node.getBoundingClientRect();
          parents.push({tag:node.tagName,id:node.id,class:String(node.className?.baseVal ?? node.className),width:box.width,left:box.left,right:box.right});
        }
        return [{tag:element.tagName,id:element.id,class:String(element.className?.baseVal ?? element.className),
          width:rect.width,left:rect.left,right:rect.right,scrollWidth:element.scrollWidth,clientWidth:element.clientWidth,
          text:element.textContent?.trim().slice(0,180),parents,
          styles:Object.fromEntries(['display','position','width','minWidth','maxWidth','overflowX','overflowWrap','wordBreak','whiteSpace','gridTemplateColumns','flexDirection','fontSize','letterSpacing'].map(name=>[name,style[name]]))}];
      }).sort((left,right)=>right.right-left.right).slice(0,50);
      return {overflow:document.documentElement.scrollWidth > width,viewport:width,documentWidth:document.documentElement.scrollWidth,offenders};
    }""")
    if measured["overflow"]:
        (output / "patient-overflow.json").write_text(json.dumps(measured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        page.screenshot(path=str(output / "patient-overflow.png"), full_page=True)
    assert not measured["overflow"], "patient horizontal overflow; inspect patient-overflow.json/png for measured element widths"


def audit_detail(detail: dict, submitted: dict, expected_mode: str, commit: str) -> dict:
    """Independently bind UTF-8 source citations, accepted artifact and receipts."""
    result = detail["result"]
    assert detail["status"] == "review_required", "complex source questions must await human review"
    assert detail["source"] == submitted and detail["source_profile"] == PROFILE
    assert detail["source_kind"] == "reconciliation" and detail["origin"] == "live_run"
    assert detail["source_hash"] == result["source_hash"] == digest(submitted)
    assert result["status"] == "verified" and result["model_mode"] == expected_mode
    assert result["commit"] == commit and result["artifact_kind"] == "clarification_plan"
    assert result["patient_regimen"] is None and result.get("explanation") is None
    artifact = result["artifact"]
    assert artifact["kind"] == "clarification_plan" and artifact["patient_regimen"] is None
    assert artifact["language"] == submitted["language"] and artifact["source_hash"] == detail["source_hash"]
    assert detail["artifact_hash"] == result["artifact_hash"] == digest({"structured": artifact, "text": result["explanation_text"]})
    statements = {row["statement_id"]: row for row in artifact["source_statements"]}
    documents = {doc["document_id"]: doc for doc in submitted["documents"]}
    if submitted["caregiver_concerns"].strip():
        documents["caregiver-concerns"] = {"text": submitted["caregiver_concerns"], "issued_at": None, "effective_at": None}
    assert statements and len(statements) == len(artifact["source_statements"])
    for statement in statements.values():
        citation = statement["citation"]
        doc = documents[citation["document_id"]]
        source = doc["text"].encode("utf-8")
        assert citation["document_sha256"] == hashlib.sha256(source).hexdigest()
        assert source[citation["byte_start"]:citation["byte_end"]].decode("utf-8") == citation["quote"]
        assert doc["text"].splitlines()[citation["line"] - 1] == citation["quote"]
        assert statement["issued_at"] == doc["issued_at"] and statement["effective_at"] == doc["effective_at"]
    assert artifact["structural_issues"], "the chosen conflict example must have a structural clarification"
    for issue in artifact["structural_issues"]:
        assert issue["classification"] in {"structural_check", "selected_label_reference"}
        assert set(issue["statement_ids"]) <= statements.keys()
    for row in artifact["proposed_questions"] + artifact["teachback"]:
        assert row["classification"] == "context_proposal" and set(row["statement_ids"]) <= statements.keys()
    previous = "0" * 64
    for index, receipt in enumerate(result["receipts"], 1):
        assert receipt["sequence"] == index and receipt["previous_hash"] == previous
        assert receipt["session_id"] == result["session_id"] and receipt["source_hash"] == detail["source_hash"]
        assert receipt["hash"] == digest({key: value for key, value in receipt.items() if key != "hash"})
        previous = receipt["hash"]
    assert result["receipts"][-1]["verdict"] == "verified"
    assert result["receipts"][-1]["artifact_hash"] == detail["artifact_hash"]
    assert all(agent in result["runtime"]["nodes_executed"] for agent in AGENTS)
    return {"case_id": detail["id"], "source_hash": detail["source_hash"], "artifact_hash": detail["artifact_hash"],
            "model_mode": expected_mode, "commit": commit, "source_citations": len(statements),
            "receipt_count": len(result["receipts"]), "correction_count": result["correction_count"]}


def open_form(page, expect):
    page.get_by_role("button", name="New case", exact=True).click()
    # This control immediately opens a different dialog, so it cannot remain
    # mounted for Playwright's post-click checkbox-state assertion.
    page.get_by_test_id("reconcile-packet-mode").click()
    form = page.get_by_role("dialog", name="Reconcile synthetic source packet", exact=True)
    expect(form).to_be_visible()
    return form


def completed_case(page, client, case_id: str, output: Path, filename: str, expect):
    detail = wait_case(client, case_id)
    # Save the actual terminal response before any clinical or workflow assertion.
    (output / filename).write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert detail["status"] == "review_required", "the saved terminal response must contain a verified clarification for review"
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer.locator(".drawer-meta .badge")).to_have_text("Review required", timeout=240_000)
    return drawer, detail


DEVICE_VOICE_SIMULATION = """(() => {
  const calls = []; window.__medplainVoiceTest = calls;
  const voice = {localService:true,lang:'el-GR',name:'CI simulated local Greek voice'};
  Object.defineProperty(window,'speechSynthesis',{configurable:true,value:{
    getVoices:()=>sessionStorage.getItem('medplain.test.voice.unavailable')==='1'?[]:[voice],addEventListener:()=>{},removeEventListener:()=>{},
    speak:utterance=>calls.push({action:'speak',text:utterance.text,lang:utterance.lang}),
    pause:()=>calls.push({action:'pause'}),resume:()=>calls.push({action:'resume'}),
    cancel:()=>calls.push({action:'cancel'})}});
  Object.defineProperty(window,'SpeechSynthesisUtterance',{configurable:true,value:class {
    constructor(text){this.text=text;} }});
})();"""


def exercise(page, browser, url: str, output: Path, expect) -> dict:
    base = url.rstrip("/")
    page.goto(base + "/console/overview?collection=empty", wait_until="networkidle")
    expect(page.get_by_role("heading", name="Operations overview", exact=True)).to_be_visible()
    client = session_client(page, url)
    boot = client.call("/api/ops/bootstrap")
    assert client.call("/api/ops/cases?limit=20")["total"] == 0
    examples = Client(url).call("/api/ops/reconciliation-examples")
    assert examples["profile"] == PROFILE and len(examples["examples"]) >= 6
    chosen = next(item for item in examples["examples"] if item["id"] == "unexplained-dose-stop")
    form = open_form(page, expect)
    submit = form.get_by_test_id("start-reconciliation")
    expect(submit).to_be_disabled()
    form.get_by_label("Complex synthetic example", exact=True).select_option(chosen["id"])
    form.get_by_role("button", name="Load complex synthetic example", exact=True).click()
    source = form.get_by_test_id("reconciliation-source-1")
    expect(source).to_have_value(chosen["dossier"]["documents"][0]["text"])
    confirmation = form.get_by_label("Confirm invented reconciliation sources", exact=True)
    confirmation.check()
    expect(submit).to_be_enabled()
    source.fill("x" * 16001)
    expect(source).to_have_value("x" * 16001)
    expect(confirmation).not_to_be_checked()
    expect(submit).to_be_disabled()
    expect(form.locator("#reconciliation-submit-reason")).to_contain_text("Nothing is silently truncated")
    source.fill(chosen["dossier"]["documents"][0]["text"])
    form.get_by_label("Deliberate dosage or citation mutation drill", exact=True).check()
    expect(confirmation).not_to_be_checked()
    references = form.get_by_test_id("reference-panel")
    references.locator("summary").first.click()
    expect(references).to_contain_text("Pinned reference snapshot")
    expect(references.get_by_test_id("interaction-service-status")).to_contain_text(examples["reference_material"]["interaction_service"])
    expect(references).to_contain_text("separate from patient-specific source proof")
    references.locator("summary").first.click()
    confirmation.check()
    page.screenshot(path=str(output / "reconciliation-source-desktop.png"), full_page=True)
    first = {}

    def accepted_response_lost(route):
        response = route.fetch()
        assert response.status == 202
        first.update(body=route.request.post_data_json, response=response.json())
        route.abort("failed")

    page.route("**/api/ops/cases/reconcile", accepted_response_lost, times=1)
    submit.click()
    expect(form.get_by_test_id("reconciliation-error")).to_contain_text("same submission identifier is retained")
    assert client.call("/api/ops/cases?limit=20")["total"] == 1
    confirmation.uncheck()
    confirmation.check()
    with page.expect_response(lambda res: res.url.endswith("/api/ops/cases/reconcile") and res.request.method == "POST") as created:
        submit.click()
    assert created.value.status == 202 and created.value.json()["created"] is False
    submitted = created.value.request.post_data_json
    assert submitted == first["body"] and submitted["synthetic_confirmed"] is True and submitted["stress"] is True
    assert submitted["dossier"] == chosen["dossier"]
    case_id = created.value.json()["case"]["id"]
    assert case_id == first["response"]["case"]["id"]
    drawer, detail = completed_case(page, client, case_id, output, "source-case.json", expect)
    mode = "fixture" if urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"} else "bedrock"
    proof = audit_detail(detail, submitted["dossier"], mode, boot["runtime"]["commit"])
    assert proof["correction_count"] > 0 and any(row["verdict"] == "refused" for row in detail["result"]["receipts"])
    expect(drawer.get_by_role("button", name="Release clarification card", exact=True)).to_be_disabled()
    drawer.get_by_role("tab", name="Clarification", exact=True).click()
    card = drawer.get_by_test_id("clarification-card")
    expect(card.get_by_test_id("regimen-withheld")).to_contain_text("not a new prescription")
    expect(card.get_by_role("heading", name="Confirmed structural checks", exact=True)).to_be_visible()
    expect(card.get_by_role("heading", name="AI context proposals", exact=True)).to_be_visible()
    voice = drawer.get_by_role("button", name="Read aloud with device voice", exact=True)
    expect(voice).to_be_disabled()
    expect(drawer.locator(".reading-controls")).to_contain_text("only after the clarification card is released")
    group = card.get_by_test_id("citation-group").first
    group.locator(":scope > summary").click()
    quote = group.get_by_test_id("source-quotation").first
    first_issue = next(item for item in detail["result"]["artifact"]["structural_issues"] if item["classification"] == "structural_check")
    first_statement = next(item for item in detail["result"]["artifact"]["source_statements"] if item["statement_id"] == first_issue["statement_ids"][0])
    expect(quote.locator("blockquote")).to_have_text(first_statement["citation"]["quote"])
    quote.get_by_role("button", name=re.compile(r"^Open original ")).click()
    expect(drawer.get_by_role("tab", name="Source", exact=True)).to_have_attribute("aria-selected", "true")
    expect(drawer.locator(".reconciliation-source-card:focus")).to_be_visible()
    drawer.get_by_role("tab", name="Safety", exact=True).click()
    expect(drawer.locator(".receipt-card.refused")).to_be_visible()
    expect(drawer.locator(".receipt-card.verified")).to_be_visible()
    page.screenshot(path=str(output / "reconciliation-stress-gate.png"), full_page=True)
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "reviewer", expect)
    page.goto(base + f"/console/cases?case={case_id}", wait_until="networkidle")
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    drawer.get_by_label("Review comment", exact=True).fill("Reviewed exact sources and unresolved questions. Release clarification only, never a medication regimen.")
    with page.expect_response(lambda res: res.url.endswith(f"/cases/{case_id}/review") and res.request.method == "POST") as released:
        drawer.get_by_role("button", name="Release clarification card", exact=True).click()
    assert released.value.status == 200
    review = released.value.request.post_data_json
    assert review["expected_version"] == detail["version"] and review["source_hash"] == detail["source_hash"] and review["artifact_hash"] == detail["artifact_hash"]
    expect(drawer.get_by_role("link", name="Open patient view", exact=True)).to_be_visible()
    released_detail = client.call(f"/api/ops/cases/{case_id}")
    assert released_detail["status"] == "released" and released_detail["result"]["patient_regimen"] is None
    drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "operator", expect)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(base + f"/console/cases?case={case_id}", wait_until="networkidle")
    drawer = page.get_by_role("dialog", name="Case details", exact=True)
    expect(drawer.get_by_test_id("revise-reconciliation")).to_be_enabled()
    drawer.get_by_test_id("revise-reconciliation").click()
    revision = page.get_by_role("dialog", name="Revise sources and reverify", exact=True)
    expect(revision).to_be_visible()
    expect(revision.get_by_test_id("reconciliation-source-1")).to_have_value(chosen["dossier"]["documents"][0]["text"])
    expect(revision.get_by_label("Confirm invented reconciliation sources", exact=True)).not_to_be_checked()
    revision.get_by_label("Clarification card language", exact=True).select_option("el")
    revision.get_by_label("Confirm invented reconciliation sources", exact=True).check()
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(output / "reconciliation-revision-mobile-375.png"), full_page=True)
    page.route("**/api/ops/cases/reconcile", lambda route: route.fulfill(status=413, json={"detail": "INJECTED_PROVIDER_SECRET_MUST_NOT_RENDER"}), times=1)
    revision.get_by_test_id("start-reconciliation").click()
    expect(revision.get_by_test_id("reconciliation-error")).to_contain_text("Nothing was silently truncated")
    expect(page.locator("body")).not_to_contain_text("INJECTED_PROVIDER_SECRET_MUST_NOT_RENDER")
    expect(revision.get_by_test_id("reconciliation-source-1")).to_have_value(chosen["dossier"]["documents"][0]["text"])
    assert client.call("/api/ops/cases?limit=20")["total"] == 1
    with page.expect_response(lambda res: res.url.endswith("/api/ops/cases/reconcile") and res.request.method == "POST") as revised:
        revision.get_by_test_id("start-reconciliation").click()
    assert revised.value.status == 202 and revised.value.request.post_data_json["parent_case_id"] == case_id
    revised_id = revised.value.json()["case"]["id"]
    assert revised_id != case_id
    revised_drawer, revised_detail = completed_case(page, client, revised_id, output, "revised-case.json", expect)
    revision_proof = audit_detail(revised_detail, revised.value.request.post_data_json["dossier"], mode, boot["runtime"]["commit"])
    assert revised_detail["parent_case_id"] == case_id
    assert client.call(f"/api/ops/cases/{case_id}")["source"] == chosen["dossier"]
    assert revised_detail["result"]["artifact"]["language"] == "el"
    assert any(re.search(r"[\u0370-\u03ff]", row["question_text"]) for row in revised_detail["result"]["artifact"]["structural_issues"])
    revised_drawer.get_by_role("tab", name="Clarification", exact=True).click()
    expect(revised_drawer.get_by_test_id("clarification-card")).to_be_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(output / "reconciliation-greek-mobile-375.png"), full_page=True)
    revised_drawer.get_by_role("button", name="Close dialog", exact=True).click()
    change_role(page, "reviewer", expect)
    page.goto(base + f"/console/cases?case={revised_id}", wait_until="networkidle")
    revised_drawer = page.get_by_role("dialog", name="Case details", exact=True)
    revised_drawer.get_by_label("Review comment", exact=True).fill("Greek clarification questions reviewed; English source quotations unchanged. Medication regimen remains withheld.")
    with page.expect_response(lambda res: res.url.endswith(f"/cases/{revised_id}/review") and res.request.method == "POST") as revision_release:
        revised_drawer.get_by_role("button", name="Release clarification card", exact=True).click()
    assert revision_release.value.status == 200
    expect(revised_drawer.get_by_role("link", name="Open patient view", exact=True)).to_be_visible()
    patient_path = client.call(f"/api/ops/cases/{revised_id}")["patient_url"]
    share_id = patient_path.rsplit("/", 1)[1]
    public = Client(url).call(f"/api/ops/patient/{share_id}")
    assert public["source"] == revised_detail["source"]
    public_result_fields = {"status", "source_profile", "source_hash", "artifact_kind", "artifact", "artifact_hash",
                            "patient_regimen", "explanation_text", "trace", "receipts", "correction_count", "model_mode"}
    assert set(public["result"]) == public_result_fields, "patient results must use the fixed publication field set"
    assert public["result"] == {field: revised_detail["result"][field] for field in public_result_fields}, "every published artifact, source binding, text and receipt must exactly equal the released private result"
    assert not {"runtime", "session_id", "error", "commit"}.intersection(public["result"]), "private execution capabilities must not be published"
    # Explicit simulated voice capability, separate from the real SDK journey.
    page.add_init_script(DEVICE_VOICE_SIMULATION)
    page.goto(base + f"/console/cases?case={revised_id}", wait_until="networkidle")
    revised_drawer = page.get_by_role("dialog", name="Case details", exact=True)
    revised_drawer.get_by_role("tab", name="Clarification", exact=True).click()
    revised_drawer.get_by_role("button", name="Read aloud with device voice", exact=True).click()
    revised_drawer.get_by_role("tab", name="Source", exact=True).click()
    assert [row["action"] for row in page.evaluate("window.__medplainVoiceTest")] == ["speak", "cancel"]
    patient_context = browser.new_context(viewport={"width": 375, "height": 812})
    try:
        patient_context.add_init_script(DEVICE_VOICE_SIMULATION)
        patient = patient_context.new_page()
        patient.goto(urljoin(base + "/", patient_path), wait_until="networkidle")
        expect(patient.get_by_test_id("clarification-card")).to_be_visible()
        expect(patient.get_by_test_id("regimen-withheld")).to_be_visible()
        pinned = patient.get_by_test_id("reference-panel")
        pinned.locator("summary").first.click()
        expect(pinned.get_by_test_id("reference-snapshot-matched")).to_be_visible()
        primary = next(card for card in examples["reference_material"]["references"]["cards"] if card["id"] == "dailymed-warfarin")
        reference_card = pinned.locator(f"[data-reference-id='{primary['id']}']")
        expect(reference_card.get_by_role("link", name="Open primary source: " + primary["publisher"], exact=True)).to_have_attribute("href", primary["url"])
        expect(reference_card.get_by_test_id("reference-summary")).to_have_text(primary["excerpt"])
        expect(reference_card).to_contain_text("Reference summary (paraphrased)")
        expect(reference_card).to_contain_text(examples["reference_material"]["references"]["observed_on"])
        assert patient.evaluate("window.__medplainVoiceTest") == [], "no autoplay is permitted"
        controls = patient.get_by_role("region", name="Reading and accessibility controls", exact=True)
        controls.get_by_role("button", name="Larger text", exact=True).click()
        controls.get_by_role("button", name="High contrast", exact=True).click()
        expect(patient.locator("html")).to_have_attribute("data-reading-large", "true")
        expect(patient.locator("html")).to_have_attribute("data-reading-contrast", "true")
        assert patient.get_by_test_id("regimen-withheld").locator("p").evaluate("element => parseFloat(getComputedStyle(element).fontSize)") >= 18
        controls.get_by_role("button", name="Read aloud with device voice", exact=True).click()
        assert patient.evaluate("window.__medplainVoiceTest[0].text") == revised_detail["result"]["explanation_text"]
        controls.get_by_role("button", name="Pause voice", exact=True).click()
        controls.get_by_role("button", name="Resume device voice", exact=True).click()
        controls.get_by_role("button", name="Stop voice", exact=True).click()
        assert [row["action"] for row in patient.evaluate("window.__medplainVoiceTest")] == ["speak", "pause", "resume", "cancel"]
        assert_patient_width(patient, output)
        patient.screenshot(path=str(output / "clarification-patient-accessible-mobile-375.png"), full_page=True)
        patient.evaluate("sessionStorage.setItem('medplain.test.voice.unavailable', '1')")
        patient.goto(urljoin(base + "/", patient_path), wait_until="networkidle")
        expect(patient.get_by_role("button", name="Read aloud with device voice", exact=True)).to_be_disabled()
        expect(patient.locator(".reading-controls")).to_contain_text("No local Greek device voice is available")
        # Explicit metadata response injections test withholding only. The real
        # released artifact and its source/receipt hashes remain unchanged.
        patient_context.route("**/api/ops/reconciliation-examples", lambda route: route.fulfill(status=200, json={
            **examples, "reference_material": {**examples["reference_material"], "snapshot_sha256": "0" * 64}}), times=1)
        patient.reload(wait_until="networkidle")
        pinned = patient.get_by_test_id("reference-panel")
        pinned.locator("summary").first.click()
        expect(pinned.get_by_test_id("reference-snapshot-mismatch")).to_be_visible()
        expect(pinned.get_by_role("link")).to_have_count(0)
        expect(pinned.get_by_test_id("reference-summary")).to_have_count(0)
        patient.screenshot(path=str(output / "reference-snapshot-mismatch-mobile-375.png"), full_page=True)
        missing_material = {**examples["reference_material"], "references": {**examples["reference_material"]["references"],
            "cards": [card for card in examples["reference_material"]["references"]["cards"] if card["id"] != primary["id"]]}}
        patient_context.route("**/api/ops/reconciliation-examples", lambda route: route.fulfill(status=200, json={
            **examples, "reference_material": missing_material}), times=1)
        patient.reload(wait_until="networkidle")
        pinned = patient.get_by_test_id("reference-panel")
        pinned.locator("summary").first.click()
        expect(pinned.get_by_test_id("reference-id-unavailable")).to_contain_text(primary["id"])
        expect(pinned.locator(f"[data-reference-id='{primary['id']}']")).to_have_count(0)
    finally:
        patient_context.close()
    assert client.call("/api/ops/cases?limit=20")["total"] == 2
    # These are explicitly injected publication states, not measured model results.
    page.route("**/api/reconciliation-evaluations/live", lambda route: route.fulfill(status=200, json={
        "status": "not_measured", "source_commit": boot["runtime"]["commit"],
        "reason": "CI interface-state injection: no actual Bedrock measurement is published."}), times=1)
    page.goto(base + "/console/settings", wait_until="networkidle")
    panel = page.get_by_test_id("reconciliation-evaluation-panel")
    offline = panel.get_by_test_id("reconciliation-fixture-evidence")
    expect(offline.get_by_role("table")).to_be_visible()
    expect(offline).to_contain_text("fixture planner replays the deterministic baseline")
    live_panel = panel.get_by_test_id("reconciliation-live-evidence")
    expect(live_panel).to_contain_text("Not measured for this source")
    expect(live_panel.get_by_role("table")).to_have_count(0)
    page.screenshot(path=str(output / "reconciliation-evidence-not-measured-mobile.png"), full_page=True)
    injected_manifest = {
        "status": "published", "profile": PROFILE, "mode": "bedrock", "source_commit": boot["runtime"]["commit"],
        "report_sha256": "a" * 64, "corpus_sha256": "b" * 64, "ruler_sha256": "c" * 64,
        "manifest_sha256": "d" * 64,
        "report_url": "https://github.com/upgradedev/atropos-medplain-aws/releases/download/ci-interface-fixture/not-a-measurement.json",
        "arms": {"strands": {"case_count": 2, "verified_count": 1, "runtime_error_count": 1},
                 "baseline": {"case_count": 2, "verified_count": 2, "runtime_error_count": 0}},
    }
    page.route("**/api/reconciliation-evaluations/live", lambda route: route.fulfill(status=200, json=injected_manifest), times=1)
    page.reload(wait_until="networkidle")
    panel = page.get_by_test_id("reconciliation-evaluation-panel")
    live_panel = panel.get_by_test_id("reconciliation-live-evidence")
    expect(live_panel.get_by_role("table")).to_be_visible()
    verified_row = live_panel.get_by_role("row", name="Verified clarification cards 1 2", exact=True)
    expect(verified_row).to_be_visible()
    expect(live_panel.get_by_role("row", name="Runtime errors 1 0", exact=True)).to_be_visible()
    expect(live_panel.get_by_role("row", name="Workflow refusals Not reported Not reported", exact=True)).to_be_visible()
    expect(live_panel.get_by_role("link", name="Inspect full published Bedrock JSON report", exact=True)).to_have_attribute("href", injected_manifest["report_url"])
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(output / "reconciliation-evidence-injected-published-mobile.png"), full_page=True)
    page.route("**/api/reconciliation-evaluations/live", lambda route: route.fulfill(status=200, json={**injected_manifest, "source_commit": "f" * 40}), times=1)
    page.reload(wait_until="networkidle")
    live_panel = page.get_by_test_id("reconciliation-live-evidence")
    expect(live_panel.get_by_role("heading", name="Bedrock comparison unavailable", exact=True)).to_be_visible()
    expect(live_panel.get_by_role("table")).to_have_count(0)
    # Reuse the verified case; inspecting its architecture must not queue a job.
    page.goto(base + "/console/maps", wait_until="networkidle")
    page.get_by_label("Recorded case", exact=True).select_option(case_id)
    workflow = page.get_by_test_id("workflow-map")
    expect(workflow).to_have_attribute("data-source-profile", PROFILE)
    expect(workflow).to_have_attribute("aria-label", re.compile(r"Evidence indexer.*clarification planner"))
    node_detail = page.locator(".node-detail")
    expect(node_detail.get_by_role("heading", name="Index original sources", exact=True)).to_be_visible()
    expect(node_detail.locator(".receipt-card").first).to_be_visible()
    workflow.get_by_role("button", name="Inspect Plan clarification", exact=True).click()
    expect(node_detail.get_by_role("heading", name="Plan clarification", exact=True)).to_be_visible()
    expect(node_detail.get_by_text("CrossDocumentClarificationPlannerAgent", exact=True)).to_be_visible()
    expect(node_detail.locator(".receipt-card").first).to_be_visible()
    publisher = workflow.get_by_role("button", name="Inspect Build clarification", exact=True)
    publisher.focus()
    publisher.press("Enter")
    expect(node_detail.get_by_role("heading", name="Build clarification", exact=True)).to_be_visible()
    gate = workflow.get_by_role("button", name="Inspect Reconciliation gate", exact=True)
    gate.focus()
    gate.press("Space")
    expect(node_detail.get_by_text("AtroposReconciliationSafetyGate", exact=True)).to_be_visible()
    expect(node_detail.locator(".receipt-card").first).to_be_visible()
    expect(workflow.get_by_role("button", name="Inspect Parse source", exact=True)).to_have_count(0)
    assert client.call("/api/ops/cases?limit=20")["total"] == 2
    page.screenshot(path=str(output / "reconciliation-recorded-workflow-map.png"), full_page=True)
    page.get_by_label("Recorded case", exact=True).select_option("")
    expect(workflow).to_have_attribute("data-source-profile", "simple-discharge")
    expect(node_detail.get_by_role("heading", name="Parse source", exact=True)).to_be_visible()
    old_gate = workflow.get_by_role("button", name="Inspect Atropos safety gate", exact=True)
    old_gate.focus()
    old_gate.press("Enter")
    expect(node_detail.get_by_role("heading", name="Atropos safety gate", exact=True)).to_be_visible()
    return {"source_case": proof, "revision_case": revision_proof, "mobile_width": 375,
            "source_quote_bytes_independently_checked": True, "source_and_question_languages_separate": True,
            "exact_hash_review": True, "patient_regimen_always_withheld": True,
            "revision_creates_new_case_parent_unchanged": True, "lost_response_one_idempotent_job": True,
            "injected_errors": [413], "voice_test": "Simulated local device voice, explicit user start/pause/resume/stop and unmount cancellation; no native speech-quality claim.",
            "evaluation_panel_test": "Explicit HTTP interface-state injections: not measured, published two-case counts with missing metrics, and stale-source rejection. Injected counts are not model results.",
            "profile_specific_recorded_workflow": "Existing reconciliation case overlays its actual agents and gate. Click/keyboard selection and profile-change fallback checked; no additional job created.",
            "reference_followthrough": "Real released card resolves its bound pinned DailyMed URL/paraphrased summary/date. Explicit metadata-only mismatch/missing-ID injections withhold affected links and summaries; no clinical artifact was substituted.",
            "pinned_reference_snapshot_sha256": examples["reference_material"]["snapshot_sha256"]}


def check(url: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        host = urlparse(url).hostname
        external = []
        context.on("request", lambda request: external.append(request.url) if urlparse(request.url).hostname not in {host, None} else None)
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == host else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            proof = exercise(page, browser, url, output, expect)
            assert not errors, f"JavaScript errors: {errors}"
            assert not external, "the UI must not require a live third-party request to render pinned references"
            (output / "results.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return proof
        except Exception as exc:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "failure.json").write_text(json.dumps({"error": str(exc), "page_errors": errors}, indent=2) + "\n", encoding="utf-8")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/reconciliation-browser"))
    args = parser.parse_args()
    process = None
    log = None
    try:
        url = args.url or "http://127.0.0.1:8768"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", MEDPLAIN_OPS_MEMORY="1", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8768"], env=environment, stdout=log, stderr=subprocess.STDOUT)
            wait_for_server(url)
        print(json.dumps(check(url, args.output), ensure_ascii=False, indent=2))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if log:
            log.close()
