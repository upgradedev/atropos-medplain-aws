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
    'assert not urlparse(patient.url).fragment, "response capability must be stripped after tab-scoped storage"': ("release", "patient"),
    'expect(feedback.get_by_label("Your synthetic response", exact=True)).to_have_value("Yes, I understand.")': ("response", "patient"),
    'assert len(attempts) == 1 and attempts[0]["status"] == "assessed"': ("assessment", "page"),
    'expect(feedback.get_by_text("Reviewer recorded follow-up", exact=False)).to_be_visible()': ("follow_up", "patient"),
    'wait_attempt(patient, feedback, expect, count=2)': ("recheck", "patient"),
}
FOCUS = {
    "preview": ("test", "careflow-preview-notice"), "card": ("test", "careflow-next-action"),
    "assigned": ("button", "Accept responsibility"), "accepted": ("css", "#careflow-task-title"),
    "reply": ("button", "Prepare clarified source revision"), "revision": ("test", "start-reconciliation"),
    "linked": ("button", "Confirm resolution and close"), "closed": ("text", "Resolution recorded"),
    "evidence": ("css", '[data-testid="careflow-evidence"] .careflow-evidence > article:has(a[href="https://www.ahrq.gov/patient-safety/settings/hospital/match/chapter-3.html"])'),
    "release": ("text", "Released from a new offline fixture verification run."),
    "response": ("label", "Your synthetic response"), "assessment": ("attempt", "Response 1"),
    "follow_up": ("text", "Reviewer recorded follow-up"), "recheck": ("attempt", "Response 2"),
}


def public_capture_fragment(fragment):
    """Only the exact ordinary patient-section anchor is eligible for public pixels."""
    return fragment in {"", "teachback"}


def focus_clip(viewport, bounds, required=()):
    """Contain actual semantic regions; never silently crop off the claimed confirmation."""
    regions = list(required) or [bounds]
    media.require(all(box is not None for box in regions), "Required capture region is not visible")
    left, top = min(box["x"] for box in regions), min(box["y"] for box in regions)
    right = max(box["x"] + box["width"] for box in regions)
    bottom = max(box["y"] + box["height"] for box in regions)
    if required:
        media.require(0 <= left < right <= viewport["width"] and 110 <= top < bottom <= viewport["height"],
                      "Required stage/source content is outside the visible drawer; retain failure")
        width = min(980, viewport["width"], max(520, right - left + 48))
        height = min(560, viewport["height"], max(320, bottom - top + 48))
        media.require(right - left <= width and bottom - top <= height, "Required semantic regions exceed bounded readable crop")
    else:
        width, height = min(780, viewport["width"]), min(440, viewport["height"])
    clip = {"x": max(0, min(viewport["width"] - width, (left + right - width) / 2)),
            "y": max(0, min(viewport["height"] - height, (top + bottom - height) / 2)),
            "width": round(width), "height": round(height)}
    if required:
        media.require(clip["x"] <= left and clip["y"] <= top and clip["x"] + clip["width"] >= right
                      and clip["y"] + clip["height"] >= bottom, "Crop clips claimed task/source content")
    return clip


def instrument(source):
    # Actual dismissal of success toasts is eligible only while the original journey has no toast assertions.
    literals = [node.value.lower() for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    media.require(not any("toast" in value or "notification" in value for value in literals),
                  "Original journey now observes notifications; re-review screenshot dismissal")
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

    def record_capture(page, name, original_viewport):
        media.require(urlparse(page.url).hostname in {"127.0.0.1", "localhost"}, "Capture target escaped loopback")
        media.require(public_capture_fragment(urlparse(page.url).fragment), "Only an empty fragment or the public teachback anchor may be captured")
        path = output / (name + ".png")
        media.require(not path.exists(), "Duplicate accepted screenshot milestone")
        page.wait_for_load_state("networkidle")
        dismissed = []
        # Dismiss through the real user control, retaining each success message as provenance.
        media.require(page.locator(".toast-error").count() == 0, "Error toast must remain visible and fail capture")
        success_toasts = page.locator(".toast:not(.toast-error)")
        for _ in range(4):
            if not success_toasts.count():
                break
            toast = success_toasts.nth(0)
            dismissed.append(toast.locator(":scope > span").inner_text())
            toast.get_by_role("button", name="Dismiss notification", exact=True).click()
        media.require(success_toasts.count() == 0, "Success notifications still obscure capture")
        kind, value = FOCUS[name]
        locator = (page.get_by_role("list", name="Teach-back attempt history", exact=True).locator(":scope > li")
                   .filter(has=page.get_by_text(value, exact=True)) if kind == "attempt"
                   else page.get_by_test_id(value) if kind == "test" else page.get_by_role("button", name=value, exact=True)
                   if kind == "button" else page.get_by_label(value, exact=True) if kind == "label"
                   else page.locator(value) if kind == "css"
                   else page.get_by_text(value, exact=(name != "follow_up")))
        media.require(locator.count() == 1, "Ambiguous capture focus; do not choose arbitrary UI pixels")
        anchor = locator
        if name == "assessment":
            anchor = locator.locator(":scope > .careflow-feedback > .careflow-facts")
        elif name == "recheck":
            anchor = locator.locator(":scope > .careflow-feedback > strong")
        media.require(anchor.count() == 1, "Actual attempt assessment anchor is missing or ambiguous")
        anchor.scroll_into_view_if_needed()
        bounds = anchor.bounding_box()
        media.require(bounds is not None, "Capture focus is not visible")
        viewport = page.viewport_size
        required, semantics = [], {}
        if name == "accepted":
            selected = page.locator('section[aria-labelledby="careflow-task-title"]')
            status = selected.locator(".careflow-title > .careflow-status")
            progress = selected.locator('ol[aria-label="Resolution progress"]')
            facts = selected.locator(":scope > .careflow-facts")
            media.require(selected.count() == status.count() == progress.count() == facts.count() == 1,
                          "Acceptance capture needs one selected task and its actual status/facts")
            media.require(status.inner_text().strip().lower() == "accepted"
                          and facts.get_by_text("Awaiting the assigned actor", exact=True).count() == 0,
                          "Acceptance is not confirmed in the actual task state")
            required = [locator.bounding_box(), status.bounding_box(), progress.bounding_box(), facts.bounding_box()]
            semantics = {"task_heading": locator.inner_text(), "task_status": status.inner_text(),
                         "required_regions": ["task heading", "accepted status", "resolution progress", "explicit acceptance facts"]}
        elif name == "evidence":
            corpus = json.loads((app / "src/medplain/data/careflow_evidence_v1.json").read_bytes())
            document = next(row for row in corpus["documents"] if row["source_id"] == "ahrq-match-roles")
            link, quote = locator.get_by_role("link", name=document["title"], exact=True), locator.locator("blockquote")
            media.require(link.count() == quote.count() == 1 and link.get_attribute("href") == document["uri"]
                          and quote.inner_text().strip() == document["spans"][0]["quote"]
                          # text_content preserves source spelling; inner_text applies the label's CSS uppercase.
                          and document["publisher"] in (locator.locator(".careflow-label").text_content() or ""),
                          "Actual returned AHRQ card must contain its publisher/title and exact quote")
            required = [bounds]
            semantics = {"evidence_uri": document["uri"], "evidence_title": document["title"],
                         "evidence_quote_sha256": document["spans"][0]["sha256"],
                         "required_regions": ["actual returned publisher", "source title", "exact blockquote", "source metadata"]}
        elif name == "release":
            media.require(locator.text_content().strip() == "Released from a new offline fixture verification run.",
                          "Patient view must show its actual released fixture origin")
            required = [bounds]
            semantics = {"actual_release_origin": locator.text_content().strip(),
                         "required_regions": ["actual released offline fixture origin in the patient view"]}
        elif name in {"assessment", "recheck"}:
            heading = locator.locator(":scope > .careflow-title")
            response = locator.locator(":scope > blockquote")
            provisional = locator.locator(":scope > .careflow-feedback > strong")
            status = heading.locator(".careflow-status")
            media.require(heading.count() == response.count() == provisional.count() == status.count() == 1
                          and status.text_content().strip() == "Assessed"
                          and provisional.text_content().strip() == "Provisional assessment, reviewer required"
                          and response.text_content().strip(), "Actual numbered response and provisional assessment must be present")
            required = [heading.bounding_box(), response.bounding_box(), provisional.bounding_box()]
            semantics = {"response_number": 1 if name == "assessment" else 2,
                         "response_text": response.text_content().strip(), "actual_status": status.text_content().strip(),
                         "assessment_heading": provisional.text_content().strip(),
                         "required_regions": [value + " heading/status", "actual raw response", "actual provisional assessment heading"]}
            if name == "assessment":
                partition = locator.locator(":scope > .careflow-feedback > .careflow-facts")
                media.require(partition.count() == 1 and all(partition.get_by_text(label, exact=True).count() == 1 for label in
                              ("Source points covered", "Source points not demonstrated", "Possible misunderstanding")),
                              "Reviewer capture must expose the actual provisional partition")
                required.append(partition.bounding_box())
                semantics["actual_partition_text"] = partition.text_content().strip()
                semantics["required_regions"].append("actual covered/missing/misunderstood partition")
            else:
                follow_up = locator.locator(":scope > .careflow-feedback > p:not(.control-reason)")
                media.require(follow_up.count() == 1 and follow_up.text_content().strip(), "Second response needs its actual provisional follow-up output")
                required.append(follow_up.bounding_box())
                semantics["actual_follow_up_output"] = follow_up.text_content().strip()
                semantics["required_regions"].append("actual second-response follow-up output")
        clip = focus_clip(viewport, bounds, required)
        width, height = clip["width"], clip["height"]
        focused = output / (name + "-focus.png")
        # Native screenshot masks redact pixels without mutating the document or its CSP.
        private_links = page.locator('a[href*="#teachback="]')
        redaction = {"method": "Playwright native screenshot mask", "selector": 'a[href*="#teachback="]',
                     "mask_color": "#0f1720", "matched_elements": private_links.count(),
                     "scope": "Privacy mask only; actual user notification dismissals are recorded separately",
                     "application_dom_or_csp_changed": False}
        page.screenshot(path=str(path), full_page=False, mask=[private_links], mask_color="#0f1720")
        # Crop the retained privacy-masked viewport pixels, avoiding scroll-coordinate ambiguity.
        from PIL import Image
        with Image.open(path) as original:
            left, top = round(clip["x"]), round(clip["y"])
            original.crop((left, top, left + width, top + height)).save(focused)
        rows.append({"slot": name, "image": focused.name, "sha256": media.digest(focused),
                     "kind": "current_fixture", "source_commit": RUNTIME,
                     "provenance_label": "Current 3cbe1dc | actual offline fixture API + Strands SDK, privacy-masked pixels, no new Bedrock execution",
                     "evidence_reference": "capture-proof.json / original asserted careflow browser journey",
                     "viewport": viewport, "original_viewport_image": path.name,
                     "original_journey_viewport": original_viewport,
                     "capture_viewport_action": "Native browser resize for readable attempt capture, restored afterward" if viewport != original_viewport else "Original journey viewport retained",
                     "original_viewport_sha256": media.digest(path), "original_pixel_clip": clip,
                     "original_viewport_label": "Privacy-masked original viewport capture",
                     "privacy_redaction": redaction, "semantic_framing": semantics,
                     "dismissed_success_toasts": {"method": "Actual Dismiss notification button clicks", "messages": dismissed}})

    def record(page, name):
        original_viewport = page.viewport_size
        if name in {"assessment", "recheck"}:
            page.set_viewport_size({"width": 1440, "height": 1000})
        try:
            record_capture(page, name, original_viewport)
        finally:
            if page.viewport_size != original_viewport:
                page.set_viewport_size(original_viewport)

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
                  "representation": "Original application screenshot pixels from actual offline fixture execution, with explicit native screenshot privacy masks and bounded crops; no provider efficacy or clinical outcome claim."}
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
