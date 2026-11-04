"""Stdlib source/privacy controls; actual native recording runs only in reviewed CI."""
import ast
from copy import deepcopy
import json
from pathlib import Path

import demo_video as media
from prepare_realtime_request import FILES, validate
import realtime_careflow as recording
import realtime_pitch as composition


def source_contract(text):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        media.require(not any(key.arg in {"bypass_csp", "java_script_enabled"} for key in node.keywords), "Browser security cannot be bypassed")
        if isinstance(node.func, ast.Attribute):
            media.require(node.func.attr not in {"add_style_tag", "add_script_tag", "add_init_script", "evaluate", "evaluate_handle", "fulfill", "set_content"}, "No fake or rewritten application pixels/responses")
            if node.func.attr == "call":
                media.require(len(node.args) == 1 and not node.keywords, "Fixture API access must remain read-only; all mutations use UI")
            media.require(node.func.attr not in {"post", "put", "patch", "delete", "set_input_files"}, "No hidden request mutation")


def main():
    root = Path(__file__).resolve().parent.parent
    text = (root / "scripts/realtime_careflow.py").read_text(encoding="utf-8")
    source_contract(text)
    controls = []
    for name, source in [("offscreen_case_creation", 'client.call("/api/cases", {"source":"hidden"})'),
                         ("hidden_request_post", 'page.request.post("/api/cases")'),
                         ("fake_success_response", 'route.fulfill(status=200, json={"verified":True})'),
                         ("csp_bypass", 'browser.new_context(bypass_csp=True)'),
                         ("fake_dom", 'page.evaluate("document.body.innerHTML=pretend")')]:
        try:
            source_contract(source)
        except ValueError:
            controls.append(name)
        else:
            raise AssertionError("Unsafe recording source accepted: " + name)
    assert recording.MAX_SECONDS == 120 and recording.VIEW == {"width": 1280, "height": 720}
    constructed = {"status": "completed_actual_ui_fixture_journey_pending_video_privacy_and_pixel_review",
                   "journey": {"all_mutations_via_actual_ui": True, "provider_calls": 0}, "wall_seconds": 100.0,
                   "events": [{"kind": "active_page", "page": "console", "seconds": 0.1},
                              {"kind": "active_page", "page": "patient", "seconds": 40.0},
                              {"kind": "active_page", "page": "console", "seconds": 60.0},
                              {"kind": "milestone", "page": "console", "seconds": 99.9}]}
    start, end, windows = composition.chronology(constructed)
    assert start == 0.1 and end == 99.9 and len(windows) == 3
    assert abs(sum(row["end"] - row["start"] for row in windows) - 99.8) < 1e-6
    origin = composition.native_origin({"created_before_seconds": 0.0, "created_after_seconds": 0.1,
                                        "closed_before_seconds": 100.0, "closed_after_seconds": 100.1}, 100.0, 0.04)
    assert origin["uncertainty_seconds"] <= 0.5
    for name, change in [("recording_clock_reversal", lambda value: value["events"][2].update(seconds=39.0)),
                         ("recording_hidden_api_creation", lambda value: value["journey"].update(all_mutations_via_actual_ui=False)),
                         ("recording_paid_model_substitution", lambda value: value["journey"].update(provider_calls=1)),
                         ("recording_actual_budget_exceeded", lambda value: value.update(wall_seconds=121))]:
        bad = deepcopy(constructed)
        change(bad)
        try:
            composition.chronology(bad)
        except ValueError:
            controls.append(name)
        else:
            raise AssertionError(name)
    try:
        composition.native_origin({"created_before_seconds": 0, "created_after_seconds": 0.1,
                                   "closed_before_seconds": 100, "closed_after_seconds": 100.1}, 80, 0.04)
    except ValueError:
        controls.append("native_origin_unsubstantiated_by_actual_duration")
    else:
        raise AssertionError("Unmeasured native synchronization accepted")
    original = root / "scripts/careflow_browser_smoke.py"
    original_capture = root / "scripts/final_pitch_capture.py"
    assert original.is_file() and original_capture.is_file()
    request = {"status": "root_requested_after_independent_source_review", "independent_review_sha256": "a" * 64,
               "mode": "capture", "application_source": recording.FROZEN, "reading_seconds": 0,
               "code_sha256": {key: media.digest(root / value) for key, value in FILES.items()}}
    assert validate(root, request)["READING_SECONDS"] == "0"
    for name, mutate in [("unreviewed_source", lambda value: value.update(status="draft")),
                         ("code_pin_drift", lambda value: value["code_sha256"].update(recorder="0" * 64)),
                         ("unbounded_reading_pause", lambda value: value.update(reading_seconds=100)),
                         ("unknown_app_source", lambda value: value.update(application_source="main"))]:
        bad = deepcopy(request)
        mutate(bad)
        try:
            validate(root, bad)
        except ValueError:
            controls.append(name)
        else:
            raise AssertionError(name)
    print(json.dumps({"status": "passed_stdlib_realtime_source_controls", "negative_controls": controls,
                      "native_video_or_actual_journey_claimed": False, "network_provider_publication_calls": 0}))


if __name__ == "__main__":
    main()
