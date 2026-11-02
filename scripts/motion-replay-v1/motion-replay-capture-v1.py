"""CI-only Playwright capture of the transport-disabled, immutable historical replay."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import struct
import threading
import unicodedata
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

SLOTS = {"evidence": "cited-sources.png", "ownership": "overview.png", "teachback": "teachback.png"}
LABEL = "Historical ACTUAL Bedrock result | Source 38313f8 / run 41029208154 | CI read-only replay | review_required, NOT RELEASED"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def png_size(path):
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return list(struct.unpack(">II", data[16:24]))


def capture(args):
    from playwright.sync_api import sync_playwright

    spec = importlib.util.spec_from_file_location("motion_replay_stage", args.stager)
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    value = stage.verify_projection(args.projection, args.projection_sha256)
    stage_proof = json.loads(args.stage_proof.read_text())
    assert stage_proof["projection_sha256"] == args.projection_sha256
    staged_web = args.web_build.parent
    for name, expected in stage_proof["unchanged_display_files"].items():
        assert digest(staged_web / name) == expected
    assert digest(staged_web / "src/main.tsx") == stage_proof["staged_main_sha256"]
    assert digest(staged_web / "src/motion-replay-api.ts") == stage_proof["transport_adapter_sha256"]
    assert digest(staged_web / "src/motion-replay-data.json") == args.projection_sha256
    assert digest(staged_web / "vite.config.ts") == stage_proof["staged_vite_config_sha256"]
    assert digest(args.parent_manifest) == args.parent_sha256
    parent = json.loads(args.parent_manifest.read_text())
    assert parent["schema"] == "medplain-motion-pitch-v1"
    assert set(SLOTS) <= {shot["id"] for shot in parent["shots"]}
    for shot in parent["shots"]:
        if shot["id"] in SLOTS:
            assert "capture_requirement" in shot and "capture" not in shot
    assert not args.output.exists(), "Never overwrite prior screenshots or partial evidence"
    args.output.mkdir(parents=True)
    requests, blocked, responses, errors, frames = [], [], [], [], []
    report = {
        "schema": "medplain-historical-replay-capture-v1", "status": "started",
        "projection_sha256": args.projection_sha256, "parent_manifest_sha256": args.parent_sha256,
        "stage_proof_sha256": digest(args.stage_proof), "capture_script_sha256": digest(__file__),
        "stager_sha256": digest(args.stager), "harness_sha256": stage_proof["harness_sha256"],
        "transport_adapter_sha256": stage_proof["transport_adapter_sha256"],
        "unchanged_display_files": stage_proof["unchanged_display_files"],
        "historical_source_commit": stage.HISTORICAL_COMMIT,
        "current_component_runtime": stage.RUNTIME, "recorded_status": "review_required",
        "network_policy": "GET-only static loopback resources; no API paths, remote origins, service workers or transmitted mutations.",
        "new_model_calls": 0, "new_review_or_release_events": 0,
        "requests": requests, "blocked_requests": blocked, "responses": responses,
        "browser_errors": errors, "frames": frames,
    }

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path in {"/favicon.ico", "/console/favicon.ico"}:
                self.send_response(204)
                self.end_headers()
                return
            super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'none'; img-src 'self' data:; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'")
            super().end_headers()

        def translate_path(self, path):
            parsed = unquote(urlparse(path).path)
            if parsed == "/console" or parsed == "/console/":
                return str(args.web_build / "index.html")
            if not parsed.startswith("/console/"):
                return str(args.web_build / "__forbidden_route__")
            target = (args.web_build / parsed.removeprefix("/console/")).resolve()
            assert target.is_relative_to(args.web_build.resolve())
            return str(target)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler))
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1, locale="en-US", service_workers="block", reduced_motion="reduce")

            def route_request(route):
                request = route.request
                url = urlparse(request.url)
                row = {"method": request.method, "path": url.path, "origin": f"{url.scheme}://{url.netloc}", "resource_type": request.resource_type}
                allowed = request.method == "GET" and row["origin"] == base and (url.path in {"/console/", "/favicon.ico", "/console/favicon.ico"} or url.path.startswith("/console/assets/"))
                if not allowed:
                    blocked.append(row)
                    route.abort("blockedbyclient")
                    return
                requests.append(row)
                route.continue_()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"status": response.status, "path": urlparse(response.url).path}))
            page.goto(base + "/console/", wait_until="networkidle", timeout=30000)
            page.get_by_test_id("clarification-card").wait_for(state="visible")
            page.wait_for_function("() => (window.__MEDPLAIN_REPLAY_API_ATTEMPTS__ || []).length >= 1")
            page.get_by_test_id("historical-replay-band").wait_for(state="visible")

            def record(filename, full_page=False):
                text = page.locator("body").inner_text()
                non_latin = sorted({c for c in text if unicodedata.category(c).startswith("L") and "LATIN" not in unicodedata.name(c, "")})
                assert not non_latin, "Non-Latin letters in English-only replay: " + repr(non_latin)
                assert page.locator("html").get_attribute("lang") == "en"
                assert "review_required" in text and "NOT RELEASED" in text
                for document in value["source"]["documents"]:
                    assert document["issued_at"] in text
                    assert not document["effective_at"] or document["effective_at"] in text
                assert page.get_by_test_id("regimen-withheld").count() == 1
                assert page.locator("[lang='el']").count() == 0
                assert not blocked and not errors
                forbidden = re.compile(r"\b(run|revise|release|submit|start|create)\b", re.I)
                for button in page.get_by_role("button").all():
                    assert not (button.is_visible() and button.is_enabled() and forbidden.search(button.inner_text())), "A mutation control is visible in replay"
                image = args.output / filename
                page.screenshot(path=str(image), full_page=full_page, animations="disabled")
                frames.append({"image": filename, "sha256": digest(image), "dimensions": png_size(image), "mime_type": "image/png", "source_commit": stage.HISTORICAL_COMMIT, "kind": "historical_actual", "provenance_label": LABEL, "english_dom_non_latin_letters": non_latin, "visible_dom_text_sha256": hashlib.sha256(text.encode()).hexdigest(), "full_page": full_page})

            page.get_by_test_id("replay-artifact").evaluate("element => element.scrollIntoView({block:'start'})")
            record("overview.png")
            proposal = page.get_by_test_id("context-proposal").first
            first = proposal.get_by_test_id("citation-group").first if proposal.count() else page.get_by_test_id("citation-group").first
            first.locator("summary").click()
            if proposal.count():
                proposal.evaluate("element => element.scrollIntoView({block:'start'})")
            else:
                first.get_by_test_id("source-quotation").first.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0,-155)")
            record("cited-sources.png")
            first.locator("summary").click()
            page.evaluate("window.scrollTo(0,0)")
            teachback = page.locator("section[aria-label='Caregiver understanding questions']")
            teachback.wait_for(state="visible")
            teachback.evaluate("element => element.scrollIntoView({block:'start'})")
            assert all(question["text"] in teachback.inner_text() for question in value["artifact"]["teachback"])
            record("teachback.png")
            attempts = page.evaluate("window.__MEDPLAIN_REPLAY_API_ATTEMPTS__ || []")
            assert attempts and all(row == {"path": "/reconciliation-examples", "method": "GET", "transmitted": False} for row in attempts)
            assert not blocked and not errors and all(row["status"] == 200 or (row["status"] == 204 and row["path"] in {"/favicon.ico", "/console/favicon.ico"}) for row in responses)
            report.update({"local_denied_api_calls": attempts, "local_denied_api_call_count": len(attempts), "transmitted_api_requests": 0, "transmitted_remote_requests": 0, "transmitted_mutation_requests": 0, "viewport": {"width": 1440, "height": 1000}, "browser_version": browser.version, "citation_capture_prefers_actual_ai_proposal": bool(proposal.count()), "capture_scope": "Three real 1440 by 1000 viewports of the unchanged card/source components: overview with withheld regimen, actual AI proposal with expanded citation where available, and actual recorded English teach-back prompt. Original metadata remains in a collapsed details panel. No released patient view is depicted."})
            context.close()
            browser.close()
        derived = copy.deepcopy(parent)
        mapping = []
        for shot in derived["shots"]:
            if shot["id"] not in SLOTS:
                continue
            filename = SLOTS[shot["id"]]
            frame = next(item for item in frames if item["image"] == filename)
            del shot["capture_requirement"]
            shot["capture"] = {"image": f"{args.output.name}/{filename}", "sha256": frame["sha256"], "kind": "historical_actual", "source_commit": stage.HISTORICAL_COMMIT, "provenance_label": LABEL, "evidence_reference": f"{args.output.name}/capture-proof.json | projection {args.projection_sha256} | original run 41029208154", "crop": None, "weight": 1}
            mapping.append({"shot": shot["id"], "image": shot["capture"]["image"], "sha256": frame["sha256"]})
        for original, changed in zip(parent["shots"], derived["shots"]):
            if original["id"] not in SLOTS:
                assert original == changed
                continue
            restored = copy.deepcopy(changed)
            del restored["capture"]
            restored["capture_requirement"] = original["capture_requirement"]
            assert restored == original
        assert {key: value for key, value in parent.items() if key != "shots"} == {key: value for key, value in derived.items() if key != "shots"}
        assert digest(args.parent_manifest) == args.parent_sha256
        derived_path = args.output.parent / "derived-manifest.json"
        assert not derived_path.exists(), "Never overwrite a prior derived manifest"
        write(derived_path, derived)
        report.update({"status": "passed_ci_capture_pending_root_pixel_review", "allowed_transformation": "Only evidence/ownership/teachback capture_requirement objects replaced by actual capture dictionaries; every other parent field is unchanged. Release is a separately reviewed CGI illustration, never a replayed release event.", "mapping": mapping, "derived_manifest_sha256": digest(derived_path), "derived_manifest": derived_path.name, "not_a_clinical_or_publication_success": True})
    except BaseException as error:
        report.update({"status": "failed", "error": str(error)})
        raise
    finally:
        server.shutdown()
        server.server_close()
        write(args.output / "capture-proof.json", report)
    print(json.dumps({"status": report["status"], "derived_manifest_sha256": report["derived_manifest_sha256"], "capture_proof_sha256": digest(args.output / "capture-proof.json")}))


def main():
    parser = argparse.ArgumentParser()
    for key in ("web-build", "projection", "stage-proof", "parent-manifest", "output", "stager"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--projection-sha256", required=True)
    parser.add_argument("--parent-sha256", required=True)
    args = parser.parse_args()
    args.web_build = args.web_build.resolve()
    capture(args)


if __name__ == "__main__":
    main()
