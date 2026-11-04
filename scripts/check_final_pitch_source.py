"""Bounded stdlib-only controls; no pixel/audio render, installs, network or model calls."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import demo_video as media
import final_pitch as final
import final_pitch_capture as capture
import final_pitch_graphics as graphics
import pitch_video as audio
from prepare_final_pitch_request import FILES, validate


def rejected(name, function):
    try:
        function()
    except (AssertionError, KeyError, ValueError):
        return name
    raise AssertionError("Control unexpectedly passed: " + name)


def capture_security_contract(source):
    """A capture helper cannot rewrite the document, transport headers or browser CSP."""
    tree = ast.parse(source)
    prohibited = {"add_style_tag", "add_script_tag", "add_init_script", "evaluate", "evaluate_handle", "set_content",
                  "set_extra_http_headers", "route", "fulfill"}
    screenshots = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        assert not any(keyword.arg == "bypass_csp" for keyword in node.keywords), "Capture must preserve browser CSP enforcement"
        if isinstance(node.func, ast.Attribute):
            assert node.func.attr not in prohibited, "Capture must preserve document and transport security"
            if node.func.attr == "screenshot":
                options = {keyword.arg: keyword.value for keyword in node.keywords}
                assert "style" not in options, "Screenshot CSS injection is prohibited"
                assert "mask" in options and "mask_color" in options, "Native screenshot privacy mask required"
                assert not isinstance(options["mask"], (ast.List, ast.Tuple)) or options["mask"].elts, "Empty privacy mask is prohibited"
                screenshots.append(node)
    assert screenshots, "Actual screenshot call must enforce its privacy mask"


def main():
    source = Path(__file__).resolve().parent.parent
    controls = []
    for allowed in ("http://127.0.0.1/patient/example", "http://127.0.0.1/patient/example#teachback"):
        assert capture.public_capture_fragment(urlparse(allowed).fragment)
    for fragment in ("teachback=secret", "teachback%3Dsecret", "other", "other=secret", "teachback&other=secret", "Teachback", "teachback%00"):
        assert not capture.public_capture_fragment(urlparse("http://127.0.0.1/patient/example#" + fragment).fragment), "Private or unregistered fragment accepted"
        controls.append("reject_capture_fragment_" + fragment)
    capture_security_contract((source / "scripts/final_pitch_capture.py").read_text(encoding="utf-8"))
    for name, unsafe in [
        ("capture_csp_bypass", 'context = browser.new_context(bypass_csp=True)\npage.screenshot(mask=[secret], mask_color="#111111")'),
        ("capture_inline_css_injection", 'page.add_style_tag(content="a {visibility:hidden}")\npage.screenshot(mask=[secret], mask_color="#111111")'),
        ("capture_document_script_mutation", 'page.evaluate("document.body.innerHTML = replacement")\npage.screenshot(mask=[secret], mask_color="#111111")'),
        ("capture_header_rewrite", 'page.set_extra_http_headers(headers)\npage.screenshot(mask=[secret], mask_color="#111111")'),
        ("capture_screenshot_style_injection", 'page.screenshot(mask=[secret], mask_color="#111111", style="a {display:none}")'),
        ("capture_unmasked_secret_pixels", 'page.screenshot(path="frame.png")'),
        ("capture_empty_privacy_mask", 'page.screenshot(mask=[], mask_color="#111111")'),
    ]:
        controls.append(rejected(name, lambda: capture_security_contract(unsafe)))
    tree, hooks = capture.instrument((source / "scripts/careflow_browser_smoke.py").read_text(encoding="utf-8"))
    assert len(hooks) == 14 and isinstance(tree, ast.Module)
    drift = (source / "scripts/careflow_browser_smoke.py").read_text(encoding="utf-8").replace("assert assigned.value.status == 200", "assert assigned.value.status == 201")
    controls.append(rejected("capture_anchor_drift", lambda: capture.instrument(drift)))
    for depth in (0.0, 0.5, 1.0):
        assert graphics.camera_state(0.5, 8, depth) != graphics.camera_state(6, 8, depth)
    assert graphics.camera_state(4, 8, 0) != graphics.camera_state(4, 8, 1)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        image = root / "image.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nCONSTRUCTED_CONTROL_NOT_APPLICATION_PIXELS")
        (root / "voice.mp3").write_bytes(b"ID3CONSTRUCTED_CONTROL_NOT_SPEECH")
        chunk = "Original evidence remains tied to its named source. "
        kinds = ["home", "intro", "architecture", "process", "ui", "ui", "ui", "ui", "ui", "ui", "ui", "gate", "benchmark", "h12", "closing"]
        text = chunk * len(kinds)
        aligned = {"characters": list(text), "character_start_times_seconds": [i * 0.12 for i in range(len(text))],
                   "character_end_times_seconds": [i * 0.12 + 0.12 for i in range(len(text))]}
        (root / "alignment.json").write_bytes(media.encoded({"alignment": aligned}))
        layer = {"image": "image.png", "sha256": media.digest(image), "role": "background", "depth": 0.0,
                 "provenance": "Constructed source control, not generated or rendered imagery"}
        frame = {"image": "image.png", "sha256": media.digest(image), "kind": "current_fixture", "source_commit": final.RUNTIME,
                 "provenance_label": "Current 3cbe1dc | offline fixture constructed control", "evidence_reference": "Constructed control only"}
        slots = iter(("assigned", "reply", "closed", "release", "assessment", "recheck", "evidence"))
        shots = []
        for index, kind in enumerate(kinds):
            shot = {"id": f"shot_{index}", "kind": kind, "start_char": index * len(chunk), "end_char": (index + 1) * len(chunk),
                    "start_seconds": index * len(chunk) * 0.12, "end_seconds": (index + 1) * len(chunk) * 0.12,
                    "title": "Constructed source control", "subtitle": "No rendering or clinical success claimed",
                    "honesty_label": "CGI illustration | constructed source control"}
            if kind in {"home", "intro", "closing"}:
                shot["layers"] = [deepcopy(layer), {**layer, "role": "subject", "depth": 0.7}]
            if kind == "ui":
                shot.update(capture_slot=next(slots), capture=deepcopy(frame))
            shots.append(shot)
        plan = {"schema": final.SCHEMA, "runtime_commit": final.RUNTIME, "runtime_package_sha256": final.PACKAGE,
                "repository_commit": "b" * 40, "representation": final.REPRESENTATION, "english_review_sha256": "c" * 64,
                "audio_policy": deepcopy(audio.POLICY), "narration": {"audio": "voice.mp3", "audio_sha256": media.digest(root / "voice.mp3"),
                "alignment": "alignment.json", "alignment_sha256": media.digest(root / "alignment.json"), "text": text}, "shots": shots}

        def load(value):
            path = root / "manifest.json"
            path.write_bytes(media.encoded(value))
            return final.load_plan(root, media.digest(path))

        assert len(load(plan)[0]["shots"]) == 15
        for name, mutate in [
            ("old_runtime", lambda value: value.update(runtime_commit=media.RUNTIME)),
            ("changed_package", lambda value: value.update(runtime_package_sha256="0" * 64)),
            ("missing_home", lambda value: value["shots"][0].update(kind="process")),
            ("missing_architecture", lambda value: value["shots"][2].update(kind="process")),
            ("missing_h12", lambda value: value["shots"][-2].update(kind="benchmark")),
            ("missing_closing", lambda value: value["shots"][-1].update(kind="process")),
            ("missing_layered_character", lambda value: value["shots"][0].update(layers=[deepcopy(layer)])),
            ("unlabelled_cgi", lambda value: value["shots"][0].update(honesty_label="Actual patient success")),
            ("greek_display_copy", lambda value: value["shots"][0].update(title="\u0395\u03be\u03b9\u03c4\u03ae\u03c1\u03b9\u03bf")),
            ("em_dash_copy", lambda value: value["shots"][0].update(title="Bad\u2014copy")),
            ("character_coverage_gap", lambda value: value["shots"][3].update(start_char=value["shots"][3]["start_char"] + 1)),
            ("estimated_visual_timing", lambda value: value["shots"][3].update(start_seconds=value["shots"][3]["start_seconds"] + 0.5)),
            ("artificial_padding", lambda value: value["audio_policy"].update(padding_seconds=2)),
            ("speech_speed_change", lambda value: value["audio_policy"].update(speed=1.1)),
            ("unsupported_audio_effect", lambda value: value["audio_policy"].update(effects=["denoise"])),
            ("missing_normalization", lambda value: value["audio_policy"].update(effects=[])),
            ("unbound_ui", lambda value: value["shots"][4].pop("capture")),
            ("fixture_promoted_to_bedrock", lambda value: value["shots"][4]["capture"].update(kind="historical_actual")),
            ("capture_wrong_source", lambda value: value["shots"][4]["capture"].update(source_commit=media.RUNTIME)),
            ("capture_hash_drift", lambda value: value["shots"][4]["capture"].update(sha256="0" * 64)),
            ("asset_path_escape", lambda value: value["shots"][0]["layers"][0].update(image="../image.png")),
            ("asset_depth_nonfinite", lambda value: value["shots"][0]["layers"][0].update(depth=2)),
            ("missing_english_review", lambda value: value.update(english_review_sha256=None)),
        ]:
            changed = deepcopy(plan)
            mutate(changed)
            controls.append(rejected(name, lambda: load(changed)))
        load(plan)
        sha = media.digest(root / "manifest.json")
        controls.append(rejected("manifest_name_escape", lambda: final.load_plan(root, sha, "../manifest.json")))
        controls.append(rejected("manifest_hash_drift", lambda: final.load_plan(root, "0" * 64)))
        request = {"status": "root_requested_after_independent_source_and_input_review", "independent_review_sha256": "d" * 64,
                   "mode": "capture", "application_source": final.RUNTIME, "code_sha256": {key: media.digest(source / relative) for key, relative in FILES.items()}}
        assert validate(source, request) == {"FINAL_MODE": "capture"}
        wrong = deepcopy(request)
        wrong["code_sha256"]["capture"] = "0" * 64
        controls.append(rejected("request_code_drift", lambda: validate(source, wrong)))
        wrong = deepcopy(request)
        wrong["status"] = "draft"
        controls.append(rejected("unreviewed_render_request", lambda: validate(source, wrong)))
        wrong = deepcopy(request)
        wrong["mode"] = "full"
        controls.append(rejected("full_without_bound_inputs", lambda: validate(source, wrong)))
    print(json.dumps({"status": "passed_stdlib_final_pitch_source_controls", "negative_controls": controls,
                      "original_journey_assertions_and_actions_preserved": True, "capture_hooks": hooks,
                      "pixel_or_audio_render_claimed": False, "network_model_or_publication_calls": 0}))


if __name__ == "__main__":
    main()
