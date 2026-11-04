"""Render the final careflow pitch in CI using strict retained audio contracts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

import demo_video as media
import final_pitch_graphics as graphics
import motion_video as old_motion
import pitch_video as audio

RUNTIME = "3cbe1dc18ca4e148ca4ee1d80a6305f4fbee7571"
PACKAGE = "fd517414dc5dc80bc0856bb5bf4614a6bbb6bcc0e5c4f919377a9cb4c15befa5"
SCHEMA = "medplain-final-pitch-v1"
REPRESENTATION = "Continuous English pitch with animated layered CGI illustrations and source-labelled captured application screenshots; no continuous screen recording, new provider efficacy or clinical outcome claimed."
MANDATORY = {"home", "intro", "architecture", "process", "gate", "benchmark", "h12", "closing", "ui"}
SLOTS = {"preview", "card", "assigned", "accepted", "reply", "revision", "linked", "closed", "evidence", "release", "response", "assessment", "follow_up", "recheck"}
FPS = media.FPS
require = media.require


def image_asset(root, value):
    path = media.local_file(root, value["image"], {".png", ".jpg", ".jpeg"})
    require(media.HASH.fullmatch(value["sha256"]) and media.digest(path) == value["sha256"], "Reviewed image bytes changed")
    with path.open("rb") as stream:
        magic = stream.read(8)
    require((path.suffix.lower() == ".png" and magic == b"\x89PNG\r\n\x1a\n")
            or (path.suffix.lower() in {".jpg", ".jpeg"} and magic.startswith(b"\xff\xd8\xff")), "Wrong image file signature")
    require(path.stat().st_size < 16_000_000, "Unbounded image asset")
    return path


def capture_asset(root, value):
    image_asset(root, value)
    require(value["kind"] == "current_fixture" and value["source_commit"] == RUNTIME, "Careflow footage must bind current fixture source")
    label = value["provenance_label"]
    require(old_motion.english_copy(label) and len(label) <= 180 and all(word in label.lower() for word in ("3cbe1dc", "fixture", "offline")),
            "Always-visible current source and offline fixture label required")
    require(isinstance(value.get("evidence_reference"), str) and value["evidence_reference"].strip(), "Capture receipt is required")
    crop = value.get("crop")
    require(crop is None or (len(crop) == 4 and all(type(item) is int and item >= 0 for item in crop)
                            and crop[2] > 0 and crop[3] > 0), "Unsafe screenshot crop")


def load_plan(root, expected, name="manifest.json", allow_capture_slots=False):
    require(name in {"manifest.json", "derived-manifest.json"}, "Unapproved final manifest path")
    path = root / name
    require(media.HASH.fullmatch(expected or "") and media.digest(path) == expected, "Reviewed final manifest digest changed")
    plan = json.loads(path.read_bytes())
    require(plan["schema"] == SCHEMA and plan["runtime_commit"] == RUNTIME and plan["runtime_package_sha256"] == PACKAGE, "Wrong frozen application identity")
    require(re.fullmatch(r"[0-9a-f]{40}", plan["repository_commit"] or ""), "Exact media repository identity required")
    require(plan["representation"] == REPRESENTATION and plan["audio_policy"] == audio.POLICY, "Representation or strict continuous audio contract changed")
    require(media.HASH.fullmatch(plan["english_review_sha256"] or ""), "Independent English/copy asset review binding required")
    voice = plan["narration"]
    for field, suffixes in (("audio", {".mp3", ".wav"}), ("alignment", {".json"})):
        asset = media.local_file(root, voice[field], suffixes)
        require(media.HASH.fullmatch(voice[field + "_sha256"]) and media.digest(asset) == voice[field + "_sha256"], "Continuous voice or character alignment changed")
    text = voice["text"]
    require(old_motion.english_copy(text), "English narration without em dashes required")
    aligned = audio.alignment_data(json.loads((root / voice["alignment"]).read_bytes()), text)
    shots = plan["shots"]
    require(isinstance(shots, list) and 14 <= len(shots) <= 24, "Require a bounded final shot list")
    require(shots[0]["kind"] == "home" and shots[-1]["kind"] == "closing" and MANDATORY <= {shot["kind"] for shot in shots}, "Missing required character, architecture, process, safety, evidence or closing scene")
    previous_char, previous_time = 0, 0.0
    identifiers, slots = [], set()
    for shot in shots:
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", shot["id"]) and shot["kind"] in graphics.KINDS, "Unsafe shot identity")
        identifiers.append(shot["id"])
        start, end = shot["start_char"], shot["end_char"]
        require(type(start) is int and type(end) is int and start == previous_char and start < end <= len(text), "Shots must cover original narration characters exactly")
        require(start == 0 or text[start - 1].isspace() or text[start].isspace(), "Shot cuts a spoken word")
        start_time = 0.0 if start == 0 else aligned["character_start_times_seconds"][start]
        end_time = aligned["character_start_times_seconds"][end] if end < len(text) else max(aligned["character_end_times_seconds"])
        require(all(audio.finite_number(shot[key]) for key in ("start_seconds", "end_seconds"))
                and abs(shot["start_seconds"] - start_time) < 0.002 and abs(shot["end_seconds"] - end_time) < 0.002
                and abs(start_time - previous_time) < 0.002 and 0.7 <= end_time - start_time <= 35,
                "Visual timing must follow actual character alignment")
        for key in ("title", "subtitle", "honesty_label", "process_note"):
            require(old_motion.english_copy(shot.get(key, "")), "English-only display copy without em dashes required")
        require(shot["title"].strip() and shot["honesty_label"].strip(), "Visible title and provenance label required")
        for key in ("bullets", "nodes"):
            require(isinstance(shot.get(key, []), list) and len(shot.get(key, [])) <= 4
                    and all(old_motion.english_copy(item) and len(item) <= 150 for item in shot.get(key, [])), "Unbounded display copy")
        if shot["kind"] in {"home", "intro", "closing"}:
            layers = shot["layers"]
            require(2 <= len(layers) <= 4 and {"background", "subject"} <= {layer["role"] for layer in layers}, "Character scenes require separate environment and subject layers")
            require("illustration" in shot["honesty_label"].lower(), "Generated character imagery must remain labelled illustration")
            for layer in layers:
                image_asset(root, layer)
                require(layer["role"] in {"background", "subject", "foreground"} and audio.finite_number(layer["depth"])
                        and 0 <= layer["depth"] <= 1, "Invalid illustration depth layer")
                require(isinstance(layer.get("anchor", [0.5, 0.5]), list) and len(layer.get("anchor", [0.5, 0.5])) == 2
                        and all(audio.finite_number(x) and 0 <= x <= 1 for x in layer.get("anchor", [0.5, 0.5])), "Unsafe layer anchor")
                require(old_motion.english_copy(layer.get("provenance", "")) and layer.get("provenance", "").strip(), "Generated asset provenance required")
                if layer["role"] != "background":
                    require(Path(layer["image"]).suffix.lower() == ".png", "Foreground character layer must preserve PNG alpha")
        if shot["kind"] == "ui":
            slot = shot.get("capture_slot")
            require(slot in SLOTS, "Unapproved careflow capture slot")
            slots.add(slot)
            if shot.get("capture"):
                capture_asset(root, shot["capture"])
            else:
                require(allow_capture_slots, "Final UI pixels have not been captured and bound")
        previous_char, previous_time = end, end_time
    require(previous_char == len(text) and len(identifiers) == len(set(identifiers)), "Dropped narration tail or duplicate shot")
    require({"assigned", "reply", "closed", "release", "assessment", "recheck", "evidence"} <= slots, "New careflow journey must appear in the pitch")
    require(80 < previous_time < 180 and math.ceil(previous_time * FPS) / FPS < 180, "Final continuous narration exceeds video budget")
    return plan, aligned


def derive(root, parent_sha, captures, capture_sha):
    plan, _ = load_plan(root, parent_sha, allow_capture_slots=True)
    receipt = captures / "capture-proof.json"
    require(media.digest(receipt) == capture_sha, "Capture receipt changed")
    proof = json.loads(receipt.read_bytes())
    require(proof["runtime_commit"] == RUNTIME and proof["status"] == "completed_asserted_loopback_fixture_journey"
            and proof["live_provider_calls"] == 0, "Wrong careflow capture execution")
    rows = {row["slot"]: row for row in proof["screenshots"]}
    destination = root / "careflow-captures"
    require(not destination.exists() and not (root / "derived-manifest.json").exists(), "Preserve previous derivations")
    destination.mkdir()
    shutil.copyfile(receipt, destination / receipt.name)
    for shot in plan["shots"]:
        if shot["kind"] != "ui":
            continue
        row = rows[shot["capture_slot"]]
        capture_asset(captures, row)
        output = destination / Path(row["image"]).name
        if not output.exists():
            shutil.copyfile(captures / row["image"], output)
        shot["capture"] = {**row, "image": "careflow-captures/" + output.name,
                           "evidence_reference": "careflow-captures/capture-proof.json SHA256 " + capture_sha}
    plan["derivation"] = {"parent_manifest_sha256": parent_sha, "capture_proof_sha256": capture_sha,
                          "renderer_sha256": media.digest(Path(__file__)), "transformation": "Add exact current offline fixture screenshot bindings only; parent preserved."}
    target = root / "derived-manifest.json"
    target.write_bytes(media.encoded(plan))
    load_plan(root, media.digest(target), "derived-manifest.json")
    print(json.dumps({"derived_manifest_sha256": media.digest(target), "parent_preserved": media.digest(root / "manifest.json") == parent_sha}))


def stream(root, shots, frames, output, cues):
    canvas = graphics.FinalCanvas(root)
    samples, at = [], {}
    (output / "qa").mkdir()
    for shot in shots:
        for phase in (0.2, 0.7):
            frame = min(frames - 1, round((shot["start_seconds"] + (shot["end_seconds"] - shot["start_seconds"]) * phase) * FPS))
            at[frame] = (shot, phase)
    with (output / "visual-encoder.log").open("xb") as log:
        process = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
                                    "-video_size", "1920x1080", "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
                                    "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "final-visuals.mp4"],
                                   cwd=output, stdin=subprocess.PIPE, stderr=log, stdout=subprocess.DEVNULL)
        try:
            for number in range(frames):
                image = canvas.frame(shots, number / FPS, cues)
                process.stdin.write(image.tobytes())
                if number in at:
                    shot, phase = at[number]
                    plain = canvas.scene(shot, number / FPS).convert("RGB")
                    samples.append({"shot": shot["id"], "phase": phase, "frame": number,
                                    "uncaptioned_scene_sha256": hashlib.sha256(plain.tobytes()).hexdigest()})
                    if phase == 0.7:
                        image.resize((960, 540), canvas.Image.Resampling.LANCZOS).save(output / "qa" / (shot["id"] + ".png"))
            process.stdin.close()
            require(process.wait(timeout=180) == 0, "Final encoder failed; retain partial media")
        except BaseException:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            raise
    for shot in shots:
        pair = [item for item in samples if item["shot"] == shot["id"]]
        require(len(pair) == 2 and pair[0]["uncaptioned_scene_sha256"] != pair[1]["uncaptioned_scene_sha256"], "A scene has no actual visual motion")
    return samples


def check(video, captions, proof_path, output):
    """Use unchanged media/audio quality algorithms, with truthful current runtime metadata."""
    require(not output.exists(), "Preserve old media gate")
    proof = json.loads(proof_path.read_bytes())
    require(proof["runtime_commit"] == RUNTIME and proof["audio_policy"] == audio.POLICY
            and 0 <= proof["visual_rounding_seconds"] < 1 / FPS, "Final source/audio policy changed")
    require(media.digest(video) == proof["video_sha256"] and media.digest(captions) == proof["captions_sha256"], "Rendered bytes changed")
    info = media.probe(video)
    intervals = media.parse_srt(captions)
    result = media.validate_media(info, intervals, proof["expected_frames"])
    silence = audio.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af", "silencedetect=noise=-45dB:d=0.8", "-f", "null", "-"], report=output.with_name("audio-silence.log"))
    loudness = audio.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], report=output.with_name("audio-loudness.log"))
    qa = audio.parse_audio_reports(silence.stderr.decode(errors="replace"), loudness.stderr.decode(errors="replace"), result["audio_seconds"])
    final_pcm = output.with_name("final-audio-decoded.wav")
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(audio.RATE), "-c:a", "pcm_s16le", str(final_pcm)], report=output.with_name("final-audio-decode.log"))
    qa.update({key: proof[key] for key in ("source_pcm_signal", "normalized_pcm_signal", "normalization")})
    qa["final_pcm_signal"] = audio.pcm_signal_metrics(final_pcm)
    result.update(audio.quality_verdict(qa, intervals))
    result.update(runtime_commit=RUNTIME, runtime_package_sha256=PACKAGE, audio_qa=qa, ffprobe=info,
                  source_manifest_sha256=proof["manifest_sha256"], video_sha256=media.digest(video), captions_sha256=media.digest(captions),
                  human_review="Voice naturalness, pixels, claims and creative quality require actual owner review; metrics do not establish them.")
    output.write_bytes(media.encoded(result))
    return result


def render(root, expected, output):
    plan, aligned = load_plan(root, expected, "derived-manifest.json")
    require(not output.exists(), "Preserve prior final renders and failure evidence")
    output.mkdir(parents=True)
    output = output.resolve()
    raw = output / "continuous-narration.wav"
    original = root / plan["narration"]["audio"]
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original), "-vn", "-ac", "1", "-ar", str(audio.RATE), "-c:a", "pcm_s16le", str(raw)], report=output / "audio-decode.log")
    seconds = float(media.probe(raw)["format"]["duration"])
    require(abs(seconds - plan["shots"][-1]["end_seconds"]) <= 1 / FPS, "No voice truncation or tail padding permitted")
    frames = math.ceil(seconds * FPS)
    require(frames / FPS < 180, "Final MP4 must remain strictly under three minutes")
    normalized, normalization = audio.normalize_audio(raw, output, seconds)
    cues = audio.aligned_captions(plan["narration"]["text"], aligned, seconds)
    samples = stream(root, plan["shots"], frames, output, cues)
    video = output / "atropos-medplain-final.mp4"
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", "final-visuals.mp4", "-i", normalized.name, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(audio.RATE), "-movflags", "+faststart", video.name], cwd=output, report=output / "final-mux.log")
    captions = output / "atropos-medplain-final.srt"
    captions.write_text("\n\n".join(f"{i}\n{media.clock(a)} --> {media.clock(b)}\n{text}" for i, (a, b, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")
    proof = {"schema": SCHEMA, "status": "rendered_pending_technical_and_human_review", "manifest_sha256": expected,
             "runtime_commit": RUNTIME, "runtime_package_sha256": PACKAGE, "repository_commit": plan["repository_commit"],
             "representation": REPRESENTATION, "english_review_sha256": plan["english_review_sha256"], "audio_policy": audio.POLICY,
             "source_audio_sha256": media.digest(original), "continuous_pcm_sha256": media.digest(raw), "continuous_audio_seconds": seconds,
             "source_pcm_signal": audio.pcm_signal_metrics(raw), "normalized_pcm_signal": audio.pcm_signal_metrics(normalized),
             "normalization": normalization, "expected_frames": frames, "visual_rounding_seconds": frames / FPS - seconds,
             "motion_samples": samples, "shots": plan["shots"], "video_sha256": media.digest(video), "captions_sha256": media.digest(captions),
             "model_calls_by_renderer": 0, "application_requests_by_renderer": 0, "publication": False}
    proof_path = output / "render-proof.json"
    proof_path.write_bytes(media.encoded(proof))
    verdict = check(video, captions, proof_path, output / "media-gate.json")
    print(json.dumps({"quality_ready": verdict["quality_ready"], "status": verdict["status"], "seconds": frames / FPS, "video_sha256": proof["video_sha256"]}))
    require(verdict["quality_ready"], "Existing strict audio/caption quality gate failed; preserve MP4 and reports")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    derivation = sub.add_parser("derive")
    derivation.add_argument("--inputs", type=Path, required=True)
    derivation.add_argument("--manifest-sha256", required=True)
    derivation.add_argument("--captures", type=Path, required=True)
    derivation.add_argument("--capture-proof-sha256", required=True)
    rendering = sub.add_parser("render")
    rendering.add_argument("--inputs", type=Path, required=True)
    rendering.add_argument("--manifest-sha256", required=True)
    rendering.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "derive":
        derive(args.inputs.resolve(), args.manifest_sha256, args.captures.resolve(), args.capture_proof_sha256)
    else:
        render(args.inputs.resolve(), args.manifest_sha256, args.output)
