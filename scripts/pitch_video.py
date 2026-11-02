"""Render one continuous reviewed narration over hash-bound captured UI pixels.

FFmpeg runs in CI only. This module does not synthesize, call the application,
perform inference, upload, or change any clinical/evaluation contract.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import wave
import zipfile

import demo_video as legacy

SCHEMA = "medplain-continuous-pitch-v1"
POLICY = {"continuous": True, "padding_seconds": 0, "speed": 1, "music": False, "effects": []}
REPRESENTATION = "Narrated walkthrough assembled from captured UI screenshots; no continuous screen recording claimed."
FPS, RATE = legacy.FPS, legacy.RATE


def require(condition, message):
    legacy.require(condition, message)


def finite_number(value):
    return type(value) in {int, float} and math.isfinite(value)


def alignment_data(value, text):
    """Use the supplied original-character mapping, never fabricate timings."""
    require(isinstance(value, dict) and "audio_base64" not in value, "Remove inline audio from alignment JSON")
    alignment = value.get("alignment", value)
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    require(isinstance(chars, list) and 1 <= len(chars) <= 8_000, "Unbounded character alignment")
    require(all(isinstance(char, str) and len(char) == 1 for char in chars), "Require individual original characters")
    require("".join(chars) == text, "Original alignment must exactly cover reviewed narration text")
    require(len(chars) == len(starts) == len(ends), "Incomplete character alignment")
    previous_start = previous_end = 0.0
    for start, end in zip(starts, ends):
        require(finite_number(start) and finite_number(end) and 0 <= start <= end < 180,
                "Invalid character time")
        require(start >= previous_start and end >= previous_end, "Character time moves backwards")
        previous_start, previous_end = start, end
    words = list(re.finditer(r"\S+", text))
    require(len(words) >= 20 and all(ends[word.end() - 1] > starts[word.start()] for word in words),
            "Each spoken word needs an actual positive aligned interval")
    return {"characters": chars, "character_start_times_seconds": starts,
            "character_end_times_seconds": ends}


def validate_frame(root, frame):
    image = legacy.local_file(root, frame["image"], {".png", ".jpg", ".jpeg"})
    require(legacy.HASH.fullmatch(frame["sha256"]) and legacy.digest(image) == frame["sha256"],
            "Captured UI image changed")
    magic = image.read_bytes()[:8]
    require((image.suffix.lower() == ".png" and magic == b"\x89PNG\r\n\x1a\n")
            or (image.suffix.lower() in {".jpg", ".jpeg"} and magic.startswith(b"\xff\xd8\xff")),
            "Image extension does not match captured bytes")
    kind, source, label = frame["kind"], frame["source_commit"], frame["provenance_label"]
    require(kind in legacy.KINDS and re.fullmatch(r"[0-9a-f]{40}", source), "Invalid source image identity")
    require(isinstance(label, str) and 12 <= len(label) <= 180 and "\u2014" not in label and source[:7] in label,
            "Always-visible exact source label required")
    lower = label.lower()
    if kind.startswith("current"):
        require(source == legacy.RUNTIME and "current" in lower, "Current capture must bind live EFE")
    if kind == "current_prepared":
        require("prepared" in lower and "fixture" in lower, "Prepared capture must say fixture")
    if kind == "current_fixture":
        require("fixture" in lower and "offline" in lower, "Fixture capture must say offline fixture")
    if kind.startswith("historical"):
        require("historical" in lower, "Historical output cannot appear current")
    if kind == "historical_actual":
        require("bedrock" in lower, "Actual historical model capture must say Bedrock")
    require(isinstance(frame["evidence_reference"], str) and frame["evidence_reference"],
            "Capture evidence reference required")
    crop = frame.get("crop")
    require(crop is None or (len(crop) == 4 and all(type(value) is int and value >= 0 for value in crop)
                             and crop[2] > 0 and crop[3] > 0), "Invalid captured-pixel crop")
    require(finite_number(frame.get("weight", 1)) and 0 < frame.get("weight", 1) <= 100,
            "Invalid proportional image hold")


def load_plan(root, expected):
    path = root / "manifest.json"
    require(legacy.HASH.fullmatch(expected or "") and legacy.digest(path) == expected,
            "Exact independently reviewed manifest digest required")
    plan = json.loads(path.read_bytes())
    require(plan["schema"] == SCHEMA, "Unknown continuous-pitch schema")
    require(plan["runtime_commit"] == legacy.RUNTIME and plan["repository_commit"] == legacy.REPOSITORY_COMMIT,
            "Frozen capture/runtime identities required")
    require(plan["representation"] == REPRESENTATION, "Screenshot representation must remain explicit")
    require(plan["audio_policy"] == POLICY, "Padding, speed changes, music or effects are not permitted")
    audio = plan["narration"]
    require(set(("audio", "audio_sha256", "alignment", "alignment_sha256", "text")) <= set(audio),
            "Missing continuous narration binding")
    for field, suffixes in (("audio", {".mp3", ".wav"}), ("alignment", {".json"})):
        asset = legacy.local_file(root, audio[field], suffixes)
        require(legacy.HASH.fullmatch(audio[field + "_sha256"]) and legacy.digest(asset) == audio[field + "_sha256"],
                "Continuous narration asset changed")
    text = audio["text"]
    require(isinstance(text, str) and text.strip() and "\u2014" not in text, "Reviewed zero-em-dash narration required")
    alignment = alignment_data(json.loads((root / audio["alignment"]).read_bytes()), text)
    segments = plan["segments"]
    require(isinstance(segments, list) and 4 <= len(segments) <= 9, "Require bounded narration segments")
    previous = 0
    ids = []
    for segment in segments:
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,23}", segment["id"]), "Unsafe segment identifier")
        ids.append(segment["id"])
        require(isinstance(segment["title"], str) and segment["title"].strip() and "\u2014" not in segment["title"],
                "Reviewed zero-em-dash segment title required")
        start, end = segment["start_char"], segment["end_char"]
        require(type(start) is int and type(end) is int and start == previous and start < end <= len(text),
                "Segment character ranges must cover the complete narration contiguously")
        require(text[start:end].strip(), "Empty narration segment")
        require(start == 0 or text[start - 1].isspace() or text[start].isspace(), "Segment splits a spoken word")
        require(isinstance(segment["frames"], list) and 1 <= len(segment["frames"]) <= 5, "Bounded capture list required")
        for frame in segment["frames"]:
            require("duration_seconds" not in frame, "Old fixed beat times are forbidden in the continuous edit")
            validate_frame(root, frame)
        previous = end
    require(previous == len(text) and len(ids) == len(set(ids)), "Missing narration tail or duplicate segment")
    return plan, alignment


def aligned_captions(text, alignment):
    """Caption words use their provider times rather than word-count estimates."""
    starts, ends = alignment["character_start_times_seconds"], alignment["character_end_times_seconds"]
    words = list(re.finditer(r"\S+", text))
    result, group = [], []
    for word in words:
        proposed = " ".join(item.group() for item in group + [word])
        if group and (len(group) >= 12 or len(proposed) > 142
                      or ends[word.end() - 1] - starts[group[0].start()] > 6):
            result.append((starts[group[0].start()], ends[group[-1].end() - 1],
                           "\n".join(textwrap.wrap(" ".join(item.group() for item in group), 74))))
            group = []
        group.append(word)
    if group:
        result.append((starts[group[0].start()], ends[group[-1].end() - 1],
                       "\n".join(textwrap.wrap(" ".join(item.group() for item in group), 74))))
    require(all(start < end and (index == 0 or start >= result[index - 1][1])
                for index, (start, end, _) in enumerate(result)), "Overlapping or empty aligned captions")
    return result


def visual_timeline(plan, alignment, audio_seconds):
    require(finite_number(audio_seconds) and 0 < audio_seconds < 180, "Continuous speech exceeds finite video budget")
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    require(max(ends) <= audio_seconds + 0.02, "Character alignment exceeds decoded audio")
    total_frames = math.ceil(audio_seconds * FPS)
    require(total_frames / FPS < 180, "Final complete visual frames must remain strictly under three minutes")
    segments = plan["segments"]
    boundaries = [0] + [round(starts[segment["start_char"]] * FPS) for segment in segments[1:]] + [total_frames]
    result = []
    for index, segment in enumerate(segments):
        start, end = boundaries[index:index + 2]
        require(end > start, "Segment has no visible frame interval")
        weights = [frame.get("weight", 1) for frame in segment["frames"]]
        cumulative, previous = 0.0, start
        for image_index, (frame, weight) in enumerate(zip(segment["frames"], weights)):
            cumulative += weight
            stop = end if image_index == len(weights) - 1 else start + round((end - start) * cumulative / sum(weights))
            require(stop > previous, "Captured source image receives no visible frames")
            result.append({"segment": segment["id"], "title": segment["title"],
                           "start_frame": previous, "frames": stop - previous, "source": frame})
            previous = stop
    require(result[0]["start_frame"] == 0 and sum(item["frames"] for item in result) == total_frames,
            "Visual timeline has gaps or missing frames")
    return total_frames, result


def unpack(archive, expected, output):
    require(legacy.HASH.fullmatch(expected or "") and legacy.digest(archive) == expected, "Input archive digest differs")
    require(not output.exists(), "Preserve previous extraction and failure evidence")
    with zipfile.ZipFile(archive) as source:
        files = source.infolist()
        require(len(files) <= 100 and sum(item.file_size for item in files) < 100_000_000, "Unbounded media archive")
        require(len({item.filename for item in files}) == len(files), "Duplicate media archive member")
        for item in files:
            name = item.filename
            destination = (output / name).resolve()
            require(destination.is_relative_to(output.resolve()) and "\\" not in name and ":" not in name
                    and ".." not in Path(name).parts and not Path(name).is_absolute(), "Unsafe archive member")
            require((item.external_attr >> 16) & 0o170000 != 0o120000, "Archive symlink forbidden")
            require(item.is_dir() or destination.suffix.lower() in {".json", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".txt"},
                    "Unsupported media archive asset")
        output.mkdir(parents=True)
        source.extractall(output)


def run(argv, cwd=None, timeout=180, report=None):
    outcome = subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=True, check=False)
    if report is not None:
        report.write_bytes(outcome.stdout + outcome.stderr)
    require(outcome.returncode == 0, "Media tool failed; retain its logs and partial outputs")
    return outcome


def pcm_signal_metrics(path):
    """Bounded 16-bit PCM measurements; these do not substitute for listening."""
    count = total = squared = clipped = zeros = longest_zero = 0
    minimum, maximum = 0, 0
    with wave.open(str(path), "rb") as source:
        require(source.getnchannels() == 1 and source.getsampwidth() == 2 and source.getframerate() == RATE,
                "Signal QA requires the decoded mono 48 kHz PCM stream")
        require(0 < source.getnframes() < RATE * 180, "Unbounded or empty decoded narration")
        while raw := source.readframes(RATE):
            samples = array("h")
            samples.frombytes(raw)
            if sys.byteorder != "little":
                samples.byteswap()
            count += len(samples)
            total += sum(samples)
            squared += sum(value * value for value in samples)
            minimum, maximum = min(minimum, min(samples)), max(maximum, max(samples))
            for value in samples:
                clipped += value in {-32768, 32767}
                zeros = zeros + 1 if value == 0 else 0
                longest_zero = max(longest_zero, zeros)
    peak = max(abs(minimum), abs(maximum)) / 32768
    require(count > 0 and peak > 0, "Narration PCM is empty or entirely digital silence")
    return {"sample_count": count, "sample_rate": RATE, "sample_width_bits": 16,
            "dc_offset_fraction_fs": total / count / 32768,
            "rms_fraction_fs": math.sqrt(squared / count) / 32768,
            "peak_fraction_fs": peak, "peak_dbfs": 20 * math.log10(peak),
            "negative_peak_pcm": minimum, "positive_peak_pcm": maximum,
            "rail_clipped_sample_count": clipped, "rail_clipped_sample_fraction": clipped / count,
            "longest_exact_zero_run_seconds": longest_zero / RATE,
            "scope": "Decoded signal statistics; no absence-of-noise, naturalness or listening verdict."}


def render(root, expected, output):
    plan, alignment = load_plan(root, expected)
    require(not output.exists(), "Preserve prior render and failed partials")
    output.mkdir(parents=True)
    output = output.resolve()
    original_audio = legacy.local_file(root, plan["narration"]["audio"], {".mp3", ".wav"})
    # A single decode of the complete narration; no trimming, stitching or silence insertion.
    master = output / "continuous-narration.wav"
    run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original_audio), "-vn", "-ac", "1", "-ar", str(RATE),
         "-c:a", "pcm_s16le", str(master)], report=output / "audio-decode.log")
    audio_info = legacy.probe(master)
    audio_seconds = float(audio_info["format"]["duration"])
    pcm_signal = pcm_signal_metrics(master)
    total_frames, timeline = visual_timeline(plan, alignment, audio_seconds)
    cues = aligned_captions(plan["narration"]["text"], alignment)
    clips = []
    for index, item in enumerate(timeline):
        name = f"capture-{index:02}"
        ass = output / (name + ".ass")
        frame = item["source"]
        offset, duration = item["start_frame"] / FPS, item["frames"] / FPS
        legacy.write_ass(ass, item["title"], frame["provenance_label"], cues, offset, duration)
        image = legacy.local_file(root, frame["image"], {".png", ".jpg", ".jpeg"})
        prefix = ""
        if frame.get("crop"):
            x, y, width, height = frame["crop"]
            dimensions = legacy.probe(image)["streams"][0]
            require(x + width <= dimensions["width"] and y + height <= dimensions["height"], "Crop exceeds captured pixels")
            prefix = f"crop={width}:{height}:{x}:{y},"
        filters = prefix + f"scale=1824:740:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:140:color=0x0c131b,setsar=1,subtitles={ass.name}"
        run(["ffmpeg", "-nostdin", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(image),
             "-vf", filters, "-frames:v", str(item["frames"]), "-an", "-c:v", "libx264", "-preset", "fast",
             "-crf", "20", "-pix_fmt", "yuv420p", name + ".mp4"], cwd=output, timeout=600,
            report=output / (name + ".log"))
        clips.append(name + ".mp4")
    (output / "concat.txt").write_text("\n".join(f"file '{name}'" for name in clips) + "\n", encoding="utf-8")
    video = output / "medplain-pitch.mp4"
    # The audio ends naturally. Only the last visual frame is rounded up, by less than one frame.
    run(["ffmpeg", "-nostdin", "-v", "error", "-f", "concat", "-safe", "1", "-i", "concat.txt",
         "-i", master.name, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-ar", str(RATE), "-movflags", "+faststart", video.name], cwd=output, report=output / "final-mux.log")
    captions = output / "medplain-pitch.srt"
    captions.write_text("\n\n".join(f"{index}\n{legacy.clock(start)} --> {legacy.clock(end)}\n{text}"
                                   for index, (start, end, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")
    proof = {"status": "rendered_pending_technical_audio_and_human_review", "schema": SCHEMA,
             "manifest_sha256": expected, "runtime_commit": legacy.RUNTIME,
             "repository_commit": legacy.REPOSITORY_COMMIT, "representation": plan["representation"],
             "audio_policy": plan["audio_policy"], "source_audio_sha256": legacy.digest(original_audio),
             "continuous_pcm_sha256": legacy.digest(master), "continuous_audio_seconds": audio_seconds,
             "source_pcm_signal": pcm_signal,
             "expected_frames": total_frames, "visual_rounding_seconds": total_frames / FPS - audio_seconds,
             "timeline": timeline, "video_sha256": legacy.digest(video), "captions_sha256": legacy.digest(captions),
             "model_calls": 0, "application_requests": 0, "publication": False}
    (output / "render-proof.json").write_bytes(legacy.encoded(proof))
    check(video, captions, output / "render-proof.json", output / "media-gate.json")
    (output / "qa").mkdir()
    for index, item in enumerate(timeline):
        moment = (item["start_frame"] + min(FPS, item["frames"] / 2)) / FPS
        run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(moment), "-i", str(video), "-frames:v", "1",
             "-vf", "scale=960:-2", str(output / "qa" / f"{index:02}-{item['segment']}.png")])
    print(json.dumps({"status": "rendered_and_technical_gate_passed_human_review_pending",
                      "seconds": total_frames / FPS, "video_sha256": proof["video_sha256"]}))


def parse_audio_reports(silence_log, loudness_log, duration):
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", silence_log)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", silence_log)]
    require(len(ends) in {len(starts), len(starts) - 1}, "Malformed silence tool report")
    if len(ends) < len(starts):
        ends.append(duration)
    intervals = list(zip(starts, ends))
    require(all(0 <= start <= end <= duration + 0.05 for start, end in intervals), "Invalid silence interval")
    internal = [(start, end) for start, end in intervals if start > 0.1 and end < duration - 0.1]
    candidates = re.findall(r"\{\s*\"input_i\".*?\}", loudness_log, re.S)
    require(candidates, "Loudness tool did not return its measured input report")
    level = json.loads(candidates[-1])
    integrated, peak = float(level["input_i"]), float(level["input_tp"])
    require(math.isfinite(integrated) and math.isfinite(peak), "Silent or non-finite narration levels")
    longest_internal = max((end - start for start, end in internal), default=0)
    return {"silence_threshold_db": -45, "silence_minimum_seconds": 0.8,
            "silence_intervals": [{"start": start, "end": end} for start, end in intervals],
            "longest_internal_silence_seconds": longest_internal,
            "integrated_loudness_lufs": integrated, "true_peak_dbtp": peak,
            "quality_review_flags": [name for name, condition in (
                ("internal_silence_over_1_5_seconds", longest_internal > 1.5),
                ("input_loudness_outside_minus30_to_minus10_lufs", not -30 <= integrated <= -10),
                ("true_peak_above_zero_dbtp", peak > 0),
            ) if condition],
            "loudness_input_measurements": level,
            "scope": "Signal measurements do not establish voice naturalness, absence of audible noise, semantic correctness or listening approval."}


def check(video, captions, render_proof, output):
    require(not output.exists(), "Preserve previous technical media verdict")
    proof = json.loads(render_proof.read_bytes())
    require(proof["audio_policy"] == POLICY and 0 <= proof["visual_rounding_seconds"] < 1 / FPS,
            "Continuous audio policy or final frame rounding changed")
    require(legacy.digest(video) == proof["video_sha256"] and legacy.digest(captions) == proof["captions_sha256"],
            "Rendered media bytes changed")
    info = legacy.probe(video)
    result = legacy.validate_media(info, legacy.parse_srt(captions), proof["expected_frames"])
    silence = output.with_name("audio-silence.log")
    loudness = output.with_name("audio-loudness.log")
    silence_run = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af",
                       "silencedetect=noise=-45dB:d=0.8", "-f", "null", "-"], report=silence)
    loudness_run = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af",
                        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], report=loudness)
    # loudnorm is measurement-only here; its processed output is discarded, never used in the video.
    audio_qa = parse_audio_reports(silence_run.stderr.decode(errors="replace"),
                                   loudness_run.stderr.decode(errors="replace"), result["audio_seconds"])
    decoded_final = output.with_name("final-audio-decoded.wav")
    require(not decoded_final.exists(), "Preserve previous decoded signal evidence")
    run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(RATE),
         "-c:a", "pcm_s16le", str(decoded_final)], report=output.with_name("final-audio-decode.log"))
    audio_qa["source_pcm_signal"] = proof["source_pcm_signal"]
    audio_qa["final_pcm_signal"] = pcm_signal_metrics(decoded_final)
    result.update(status="passed_technical_continuity_gate_human_audio_pixel_and_claim_review_required",
                  source_manifest_sha256=proof["manifest_sha256"], video_sha256=legacy.digest(video),
                  captions_sha256=legacy.digest(captions), audio_qa=audio_qa, ffprobe=info,
                  continuous_audio_seconds=proof["continuous_audio_seconds"],
                  visual_rounding_seconds=proof["visual_rounding_seconds"], runtime_commit=legacy.RUNTIME,
                  source_truth_claims_review="Required independently; this gate establishes no clinical benefit or model efficacy.")
    output.write_bytes(legacy.encoded(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    unzip = sub.add_parser("unpack")
    unzip.add_argument("--archive", type=Path, required=True)
    unzip.add_argument("--sha256", required=True)
    unzip.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate-inputs")
    render_command = sub.add_parser("render")
    for command in (validate, render_command):
        command.add_argument("--inputs", type=Path, required=True)
        command.add_argument("--manifest-sha256", required=True)
    render_command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "unpack":
        unpack(args.archive, args.sha256, args.output)
    elif args.action == "validate-inputs":
        plan, alignment = load_plan(args.inputs.resolve(), args.manifest_sha256)
        print(json.dumps({"status": "validated_pinned_continuous_inputs_no_render", "segments": len(plan["segments"]),
                          "aligned_characters": len(alignment["characters"]), "model_calls": 0}))
    else:
        render(args.inputs.resolve(), args.manifest_sha256, args.output)


if __name__ == "__main__":
    main()
