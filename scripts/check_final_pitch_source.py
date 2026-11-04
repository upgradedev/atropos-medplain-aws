"""Bounded stdlib-only controls; no pixel/audio render, installs, network or model calls."""
from copy import deepcopy
import ast
import json
from pathlib import Path
import tempfile

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


def main():
    source = Path(__file__).resolve().parent.parent
    controls = []
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
