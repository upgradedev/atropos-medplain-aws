"""Dependency-free motion/English/source controls; real pixel checks run in CI."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import demo_video as legacy
import motion_graphics as graphics
import motion_video as motion
import pitch_video as pitch
from prepare_motion_video_request import FILES


def rejected(name, function):
    try:
        function()
    except (AssertionError, ValueError):
        return name
    raise AssertionError("Motion control unexpectedly accepted: " + name)


controls = []
assert motion.english_copy("Source-bound questions. Original evidence.")
assert not motion.english_copy("Greek: \u0395\u03be\u03b9\u03c4\u03ae\u03c1\u03b9\u03bf")
assert not motion.english_copy("Hidden\u2014punctuation")
for kind in sorted(graphics.KINDS):
    first = graphics.motion_state(kind, 0.5, 8)
    last = graphics.motion_state(kind, 5.5, 8)
    assert first != last and first["packet_phase"] != last["packet_phase"], "Static object scene: " + kind
assert graphics.card_quad((0, 0), 100, 200, 0, 0, 0) == [(-50, -100), (50, -100), (50, 100), (-50, 100)]
coefficients = graphics.perspective_coefficients([(0, 0), (100, 0), (100, 200), (0, 200)],
                                                [(0, 0), (100, 0), (100, 200), (0, 200)])
assert all(abs(a - b) < 1e-9 for a, b in zip(coefficients, (1, 0, 0, 0, 1, 0, 0, 0)))
assert graphics.graph_labels("process", "release") == (["Verified artifact", "Human review", "Patient view"],
                                                         ["Source-bound version", "Exact artifact decision", "Read-only after release"])
assert graphics.selected_capture({"capture": "normal", "capture_alt": "large"}, 0.49) == "normal"
assert graphics.selected_capture({"capture": "normal", "capture_alt": "large"}, 0.5) == "large"
controls.append(rejected("degenerate_3d_card", lambda: graphics.perspective_coefficients([(0, 0)] * 4, [(0, 0)] * 4)))

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / "capture.png").write_bytes(b"\x89PNG\r\n\x1a\nCONSTRUCTED_CONTROL_NOT_A_REAL_CAPTURE")
    (root / "narration.mp3").write_bytes(b"ID3CONSTRUCTED_CONTROL_NOT_REAL_AUDIO")
    text = "Evidence remains tied to its original source. " * 5
    alignment = {"alignment": {"characters": list(text),
                               "character_start_times_seconds": [index * 0.08 for index in range(len(text))],
                               "character_end_times_seconds": [index * 0.08 + 0.02 for index in range(len(text))]}}
    (root / "alignment.json").write_bytes(legacy.encoded(alignment))
    duration = alignment["alignment"]["character_end_times_seconds"][-1]
    kinds = ["intro", "sources", "ui", "architecture", "identity", "process", "ui", "gate", "ui", "benchmark", "h12", "closing"]
    frame = {"image": "capture.png", "sha256": legacy.digest(root / "capture.png"), "kind": "historical_actual",
             "source_commit": "38313f8" + "a" * 33, "provenance_label": "Historical38313f8 actual Bedrock / CONSTRUCTED CONTROL",
             "evidence_reference": "CONSTRUCTED CONTROL, no actual model evidence"}
    shots = []
    for index, kind in enumerate(kinds):
        shot = {"id": f"shot_{index}", "kind": kind, "start_seconds": duration * index / len(kinds),
                "end_seconds": duration * (index + 1) / len(kinds), "title": "Constructed motion source control",
                "subtitle": "No actual rendering or model execution", "bullets": [], "honesty_label": "CGI illustration / constructed control"}
        if kind == "ui":
            shot["capture"] = deepcopy(frame)
        shots.append(shot)
    plan = {"schema": motion.SCHEMA, "runtime_commit": legacy.RUNTIME, "repository_commit": "b" * 40,
            "representation": motion.REPRESENTATION, "english_review_sha256": "c" * 64, "audio_policy": deepcopy(pitch.POLICY),
            "narration": {"audio": "narration.mp3", "audio_sha256": legacy.digest(root / "narration.mp3"),
                          "alignment": "alignment.json", "alignment_sha256": legacy.digest(root / "alignment.json"), "text": text},
            "shots": shots}

    def load(value):
        (root / "manifest.json").write_bytes(legacy.encoded(value))
        return motion.load_plan(root, legacy.digest(root / "manifest.json"))

    assert len(load(plan)[0]["shots"]) == 12
    parent_digest = legacy.digest(root / "manifest.json")
    derived = root / "derived-manifest.json"
    derived.write_bytes(legacy.encoded(plan))
    derived_digest = legacy.digest(derived)
    assert len(motion.load_plan(root, derived_digest, "derived-manifest.json")[0]["shots"]) == 12
    assert legacy.digest(root / "manifest.json") == parent_digest
    controls.append(rejected("manifest_name_path_escape", lambda: motion.load_plan(root, parent_digest, "../manifest.json")))
    controls.append(rejected("unapproved_manifest_name", lambda: motion.load_plan(root, parent_digest, "alternate.json")))
    derived.write_bytes(legacy.encoded({**plan, "status": "CONSTRUCTED_HASH_DRIFT"}))
    controls.append(rejected("derived_manifest_hash_drift", lambda: motion.load_plan(root, derived_digest, "derived-manifest.json")))
    assert legacy.digest(root / "manifest.json") == parent_digest
    alternate = deepcopy(plan)
    alternate["shots"][2]["capture_alt"] = deepcopy(frame)
    assert len(load(alternate)[0]["shots"]) == 12
    for name, mutate in [
        ("missing_intro", lambda value: value["shots"][0].update(kind="sources")),
        ("missing_closing", lambda value: value["shots"][-1].update(kind="sources")),
        ("missing_architecture", lambda value: value["shots"][3].update(kind="sources")),
        ("missing_agent_process", lambda value: value["shots"][5].update(kind="sources")),
        ("hidden_timeline_gap", lambda value: value["shots"][2].update(start_seconds=value["shots"][2]["start_seconds"] + 0.1)),
        ("silent_tail_added", lambda value: value["shots"][-1].update(end_seconds=duration + 2)),
        ("unreviewed_english_pixels", lambda value: value.update(english_review_sha256=None)),
        ("greek_judge_copy", lambda value: value["shots"][0].update(title="\u0395\u03be\u03b9\u03c4\u03ae\u03c1\u03b9\u03bf")),
        ("prohibited_em_dash", lambda value: value["shots"][0].update(title="Unsafe\u2014copy")),
        ("ui_placeholder_not_footage", lambda value: value["shots"][2].pop("capture")),
        ("invented_capture_digest", lambda value: value["shots"][2]["capture"].update(sha256="0" * 64)),
        ("capture_path_escape", lambda value: value["shots"][2]["capture"].update(image="../capture.png")),
        ("audio_policy_relaxed", lambda value: value["audio_policy"].update(padding_seconds=1.25)),
        ("current_source_promoted", lambda value: value["shots"][2]["capture"].update(kind="current_ui")),
        ("alternate_capture_tampered", lambda value: value["shots"][2].update(capture_alt={**frame, "sha256": "0" * 64})),
        ("captured_release_event_implied", lambda value: value["shots"][5].update(id="release", capture=deepcopy(frame))),
    ]:
        value = deepcopy(plan)
        mutate(value)
        controls.append(rejected(name, lambda: load(value)))

source_root = Path(__file__).resolve().parent.parent
request_controls = []
with tempfile.TemporaryDirectory() as temporary:
    for name, success in [("source_only", True), ("reviewed_prototype", True), ("unreviewed_prototype", False),
                          ("changed_graphics_code", False), ("dispatch_changed_mode", False), ("full_without_inputs", False)]:
        case = Path(temporary) / name
        case.mkdir()
        for relative in FILES.values():
            target = case / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_root / relative).read_bytes())
        pins = {field: hashlib.sha256((case / relative).read_bytes()).hexdigest() for field, relative in FILES.items()}
        value = {"status": "root_requested_after_independent_source_and_input_review", "independent_review_sha256": "a" * 64,
                 "mode": "full" if name == "full_without_inputs" else "prototype", **pins}
        if name == "unreviewed_prototype":
            value["status"] = "draft"
        if name == "changed_graphics_code":
            value["graphics_sha256"] = "0" * 64
        if name != "source_only":
            (case / "video").mkdir()
            (case / "video/motion-render-request.json").write_bytes(legacy.encoded(value))
        env = {**os.environ, "GITHUB_REPOSITORY": "upgradedev/atropos-medplain-aws",
               "GITHUB_EVENT_NAME": "workflow_dispatch" if name == "dispatch_changed_mode" else "push",
               "MOTION_MODE": "full", "GITHUB_ENV": str(case / "environment"), "GITHUB_OUTPUT": str(case / "output")}
        outcome = subprocess.run([sys.executable, "-B", "scripts/prepare_motion_video_request.py"], cwd=case,
                                 env=env, capture_output=True, timeout=10, check=False)
        assert (outcome.returncode == 0) == success, "Motion request control: " + name
        if not success:
            assert not (case / "environment").exists() and not (case / "output").exists()
        request_controls.append(name)
print(json.dumps({"status": "passed_dependency_free_motion_source_controls", "negative_controls": controls,
                  "request_controls": request_controls, "procedural_kinds_checked": sorted(graphics.KINDS),
                  "actual_pixel_render_claimed": False, "model_calls": 0, "network_calls": 0}))
