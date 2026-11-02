"""Meaningful dependency-free controls for the separate continuous media path."""
from array import array
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

import demo_video as legacy
import pitch_video as pitch
from prepare_pitch_video_request import FILES


def rejected(label, function):
    try:
        function()
    except (AssertionError, ValueError):
        return label
    raise AssertionError("Negative control did not fail: " + label)


controls = []
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / "capture.png").write_bytes(b"\x89PNG\r\n\x1a\nCONSTRUCTED_SOURCE_CONTROL")
    (root / "narration.mp3").write_bytes(b"ID3CONSTRUCTED_SOURCE_CONTROL_NOT_ACTUAL_AUDIO")
    parts = ["One two three four five.", "Six seven eight nine ten.",
             "Eleven twelve thirteen fourteen fifteen.", "Sixteen seventeen eighteen nineteen twenty."]
    text = "\n".join(parts)
    starts = [round(0.4 + index * 0.02 + (1.2 if index >= 16 else 0), 4) for index in range(len(text))]
    ends = [round(value + 0.01, 4) for value in starts]
    aligned = {"alignment": {"characters": list(text), "character_start_times_seconds": starts,
                              "character_end_times_seconds": ends}}
    (root / "alignment.json").write_bytes(legacy.encoded(aligned))
    frame = {"image": "capture.png", "sha256": legacy.digest(root / "capture.png"),
             "kind": "historical_actual", "source_commit": "38313f8" + "a" * 33,
             "provenance_label": "Historical38313f8 actual Bedrock, CONSTRUCTED SOURCE CONTROL",
             "evidence_reference": "CONSTRUCTED CONTROL, not evidence of live execution"}
    segments, cursor = [], 0
    for index, part in enumerate(parts):
        end = cursor + len(part) + (index < len(parts) - 1)
        segments.append({"id": f"segment_{index}", "title": "Constructed source control", "start_char": cursor,
                         "end_char": end, "frames": [deepcopy(frame)]})
        cursor = end
    plan = {"schema": pitch.SCHEMA, "runtime_commit": legacy.RUNTIME,
            "repository_commit": legacy.REPOSITORY_COMMIT, "representation": pitch.REPRESENTATION,
            "audio_policy": deepcopy(pitch.POLICY),
            "narration": {"audio": "narration.mp3", "audio_sha256": legacy.digest(root / "narration.mp3"),
                          "alignment": "alignment.json", "alignment_sha256": legacy.digest(root / "alignment.json"),
                          "text": text}, "segments": segments}

    def load(value):
        (root / "manifest.json").write_bytes(legacy.encoded(value))
        return pitch.load_plan(root, legacy.digest(root / "manifest.json"))

    observed, mapping = load(plan)
    cues = pitch.aligned_captions(text, mapping)
    assert cues[0][0] == 0.4, "Caption invented a zero start instead of the actual first word time"
    assert cues[0][1] == ends[text.index(" thirteen") - 1], "Caption end does not use the actual last character"
    assert " ".join(cue[2].replace("\n", " ") for cue in cues) == " ".join(text.split())
    seconds = ends[-1] + 0.37
    count, timeline = pitch.visual_timeline(observed, mapping, seconds)
    assert timeline[0]["start_frame"] == 0
    assert timeline[1]["start_frame"] == round(starts[segments[1]["start_char"]] * pitch.FPS)
    assert 0 <= count / pitch.FPS - seconds < 1 / pitch.FPS
    assert sum(item["frames"] for item in timeline) == count
    for label, mutate in [
        ("inserted_beat_padding", lambda value: value["audio_policy"].update(padding_seconds=1.25)),
        ("speech_speed_change", lambda value: value["audio_policy"].update(speed=1.1)),
        ("background_effect", lambda value: value["audio_policy"].update(effects=["noise"])),
        ("old_fixed_visual_budget", lambda value: value["segments"][0]["frames"][0].update(duration_seconds=22)),
        ("missing_narration_range", lambda value: value["segments"][1].update(start_char=26)),
        ("narration_tail_dropped", lambda value: value["segments"][-1].update(end_char=len(text) - 1)),
        ("character_text_changed", lambda value: value["narration"].update(text=text + " extra")),
        ("source_capture_tampered", lambda value: value["segments"][0]["frames"][0].update(sha256="0" * 64)),
        ("source_kind_promoted_to_current", lambda value: value["segments"][0]["frames"][0].update(kind="current_ui")),
        ("escaping_capture_path", lambda value: value["segments"][0]["frames"][0].update(image="../capture.png")),
    ]:
        value = deepcopy(plan)
        mutate(value)
        controls.append(rejected(label, lambda: load(value)))
    backwards = deepcopy(aligned)
    backwards["alignment"]["character_start_times_seconds"][10] = 0
    controls.append(rejected("backwards_character_timing", lambda: pitch.alignment_data(backwards, text)))
    controls.append(rejected("alignment_beyond_actual_audio", lambda: pitch.visual_timeline(plan, mapping, ends[-1] - 0.1)))

    # A deliberately offset constant fixture can detect a DC measurement accidentally forced to zero.
    pcm = root / "dc-control.wav"
    with wave.open(str(pcm), "wb") as stream:
        stream.setparams((1, 2, pitch.RATE, 0, "NONE", "not compressed"))
        samples = array("h", [512] * 1200 + [0] * 800 + [-32768, 32767])
        if sys.byteorder != "little":
            samples.byteswap()
        stream.writeframes(samples.tobytes())
    signal = pitch.pcm_signal_metrics(pcm)
    assert signal["dc_offset_fraction_fs"] == (512 * 1200 - 1) / 2002 / 32768
    assert signal["rail_clipped_sample_count"] == 2
    assert signal["longest_exact_zero_run_seconds"] == 800 / pitch.RATE
    silent_pcm = root / "silent-control.wav"
    with wave.open(str(silent_pcm), "wb") as stream:
        stream.setparams((1, 2, pitch.RATE, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * pitch.RATE)
    controls.append(rejected("entirely_silent_pcm", lambda: pitch.pcm_signal_metrics(silent_pcm)))
    quality = pitch.parse_audio_reports("silence_start: 10\nsilence_end: 14\n",
                                       '{"input_i":"-19.1","input_tp":"-0.5"}', 25)
    assert quality["longest_internal_silence_seconds"] == 4
    assert quality["quality_review_flags"] == ["interior_silence_at_least_1_5_seconds"]
    controls.append(rejected("missing_loudness_measurement", lambda: pitch.parse_audio_reports("", "", 25)))
    controls.append(rejected("entirely_silent_loudness", lambda: pitch.parse_audio_reports(
        "silence_start: 0\nsilence_end: 25\n", '{"input_i":"-inf","input_tp":"-inf"}', 25)))
    clean_signal = {"dc_offset_fraction_fs": 0, "rail_clipped_sample_count": 0}

    def quality_gate(silence="", source_signal=None, final_signal=None, intervals=None):
        measured = pitch.parse_audio_reports(silence, '{"input_i":"-19.1","input_tp":"-0.5"}', 25)
        measured["source_pcm_signal"] = source_signal or deepcopy(clean_signal)
        measured["final_pcm_signal"] = final_signal or deepcopy(clean_signal)
        return pitch.quality_verdict(measured, intervals if intervals is not None else [(0, 12), (12, 25)])

    assert quality_gate()["quality_ready"]
    for name, value in [
        ("leading_gap_blocks_ready", quality_gate("silence_start: 0\nsilence_end: 1.5\n")),
        ("interior_gap_blocks_ready", quality_gate("silence_start: 10\nsilence_end: 11.5\n")),
        ("trailing_gap_blocks_ready", quality_gate("silence_start: 23.5\n")),
        ("source_dc_bias_blocks_ready", quality_gate(source_signal={**clean_signal, "dc_offset_fraction_fs": 0.00101})),
        ("negative_final_dc_bias_blocks_ready", quality_gate(final_signal={**clean_signal, "dc_offset_fraction_fs": -0.00101})),
        ("rail_clipped_source_blocks_ready", quality_gate(source_signal={**clean_signal, "rail_clipped_sample_count": 1})),
        ("rail_clipped_final_blocks_ready", quality_gate(final_signal={**clean_signal, "rail_clipped_sample_count": 1})),
        ("orphan_tail_blocks_ready", quality_gate(intervals=[(0, 24.8), (24.8, 25)])),
        ("short_interior_caption_blocks_ready", quality_gate(intervals=[(0, 0.7), (0.7, 25)])),
    ]:
        assert value["quality_ready"] is False and value["status"] == "failed_video_quality_gate_review_required", name
        assert value["quality_review_flags"], name
        controls.append(name)
    orphan_text = "Longword " * 12 + "Done."
    orphan_starts = [index * 0.025 for index in range(len(orphan_text))]
    orphan_mapping = {"character_start_times_seconds": orphan_starts,
                      "character_end_times_seconds": [value + 0.01 for value in orphan_starts]}
    repaired = pitch.aligned_captions(orphan_text, orphan_mapping, orphan_starts[-1] + 0.1)
    assert len(repaired) == 1 and repaired[0][1] - repaired[0][0] >= 1
    assert repaired[0][2].replace("\n", " ") == " ".join(orphan_text.split())
    # When a short single cue can fit only by holding into real available silence, use that gap.
    one_word = pitch.aligned_captions("Done.", {"character_start_times_seconds": [0.4] * 5,
                                                "character_end_times_seconds": [0.6] * 5}, 1)
    assert one_word == [(0.0, 1.0, "Done.")]

source_root = Path(__file__).resolve().parent.parent
request_controls = []
with tempfile.TemporaryDirectory() as temporary:
    for name, success in [("source_only_no_render", True), ("pinned_root_request", True),
                          ("unreviewed_request", False), ("changed_helpers", False),
                          ("wrong_repository", False), ("unsafe_tag", False),
                          ("dispatch_without_request", False), ("pinned_dispatch", True),
                          ("dispatch_changed_inputs", False)]:
        case = Path(temporary) / name
        case.mkdir()
        for relative in FILES.values():
            target = case / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_root / relative).read_bytes())
        pins = {field: hashlib.sha256((case / relative).read_bytes()).hexdigest() for field, relative in FILES.items()}
        value = {"status": "root_requested_after_independent_source_and_input_review",
                 "independent_review_sha256": "a" * 64, "source_tag": "medplain-pitch-inputs-CONSTRUCTED-CONTROL",
                 "source_archive_sha256": "b" * 64, "source_manifest_sha256": "c" * 64, **pins}
        if name == "unreviewed_request":
            value["status"] = "draft"
        if name == "changed_helpers":
            value["legacy_helpers_sha256"] = "0" * 64
        if name == "unsafe_tag":
            value["source_tag"] += "\nINJECTED=value"
        if name not in {"source_only_no_render", "dispatch_without_request"}:
            (case / "video").mkdir()
            (case / "video/pitch-render-request.json").write_bytes(legacy.encoded(value))
        env = {**os.environ, "GITHUB_REPOSITORY": "wrong/repository" if name == "wrong_repository" else "upgradedev/atropos-medplain-aws",
               "GITHUB_EVENT_NAME": "workflow_dispatch" if "dispatch" in name else "push",
               "GITHUB_ENV": str(case / "environment"), "GITHUB_OUTPUT": str(case / "output"),
               "SOURCE_TAG": value["source_tag"], "SOURCE_ARCHIVE_SHA": "b" * 64,
               "SOURCE_MANIFEST_SHA": "0" * 64 if name == "dispatch_changed_inputs" else "c" * 64}
        outcome = subprocess.run([sys.executable, "-B", "scripts/prepare_pitch_video_request.py"],
                                 cwd=case, env=env, capture_output=True, timeout=10, check=False)
        assert (outcome.returncode == 0) == success, "Actual request CLI control failed: " + name
        if success:
            assert (case / "output").read_text(encoding="utf-8").strip() == (
                "render=false" if name == "source_only_no_render" else "render=true")
        else:
            assert not (case / "environment").exists() and not (case / "output").exists()
        request_controls.append(name)

print(json.dumps({"status": "passed_dependency_free_continuous_pitch_controls",
                  "negative_controls": controls, "request_controls": request_controls,
                  "actual_render_or_synthesis_claimed": False, "network_calls": 0, "model_calls": 0}))
