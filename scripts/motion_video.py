"""Stream a reviewed cinematic pitch into FFmpeg. Heavy work runs in CI only."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import unicodedata

import demo_video as legacy
import motion_graphics as graphics
import pitch_video as pitch

SCHEMA = "medplain-motion-pitch-v1"
REPRESENTATION = ("Continuous narrated pitch combining labelled CGI illustrations and source-labelled captured UI pixels; "
                  "animated framing is not continuous screen recording or a new model execution.")
MANDATORY = {"intro", "architecture", "process", "gate", "benchmark", "closing"}
FPS = legacy.FPS


def require(value, message):
    legacy.require(value, message)


def english_copy(value):
    return (isinstance(value, str) and "\u2014" not in value
            and all(not char.isalpha() or "LATIN" in unicodedata.name(char, "") for char in value))


def load_plan(root, expected, manifest_name="manifest.json"):
    require(manifest_name in {"manifest.json", "derived-manifest.json"}, "Unapproved motion manifest name")
    manifest = root / manifest_name
    require(legacy.HASH.fullmatch(expected or "") and legacy.digest(manifest) == expected, "Reviewed motion manifest changed")
    plan = json.loads(manifest.read_bytes())
    require(plan["schema"] == SCHEMA and plan["runtime_commit"] == legacy.RUNTIME, "Wrong motion/runtime identity")
    require(re.fullmatch(r"[0-9a-f]{40}", plan["repository_commit"] or ""), "Root must bind the reviewed repository commit")
    require(plan["representation"] == REPRESENTATION, "CGI/capture representation must remain explicit")
    require(legacy.HASH.fullmatch(plan["english_review_sha256"] or ""), "Independent English pixel/copy review binding required")
    require(plan["audio_policy"] == pitch.POLICY, "Existing continuous audio and normalization policy is unchanged")
    audio = plan["narration"]
    for field, suffixes in (("audio", {".mp3", ".wav"}), ("alignment", {".json"})):
        asset = legacy.local_file(root, audio[field], suffixes)
        require(legacy.HASH.fullmatch(audio[field + "_sha256"]) and legacy.digest(asset) == audio[field + "_sha256"],
                "Original continuous voice/alignment changed")
    require(english_copy(audio["text"]), "English-only zero-em-dash narration required")
    alignment = pitch.alignment_data(json.loads((root / audio["alignment"]).read_bytes()), audio["text"])
    shots = plan["shots"]
    require(isinstance(shots, list) and 12 <= len(shots) <= 20, "Require a bounded cinematic shot list")
    require(shots[0]["kind"] == "intro" and shots[-1]["kind"] == "closing", "Opening and closing scenes are mandatory")
    require(MANDATORY <= {shot["kind"] for shot in shots}, "Architecture, agent process, safety and measured evidence scenes are mandatory")
    previous = 0.0
    identifiers, captures = [], 0
    for shot in shots:
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", shot["id"]), "Unsafe motion shot identifier")
        identifiers.append(shot["id"])
        require(shot["kind"] in graphics.KINDS, "Unknown procedural scene kind")
        start, end = shot["start_seconds"], shot["end_seconds"]
        require(pitch.finite_number(start) and pitch.finite_number(end) and abs(start - previous) < 0.002
                and 1 <= end - start <= 24 and end < 180, "Shot timeline must be finite and contiguous")
        require(all(english_copy(shot.get(field, "")) for field in ("title", "subtitle", "honesty_label")),
                "English-only zero-em-dash display copy required")
        require(shot["title"].strip() and shot["honesty_label"].strip(), "Visible shot title and truth label required")
        require(isinstance(shot.get("bullets", []), list) and len(shot.get("bullets", [])) <= 4
                and all(english_copy(item) for item in shot.get("bullets", [])), "Bounded English annotation text required")
        if "narration_exact" in shot:
            require(audio["text"][shot["start_char"]:shot["end_char"]] == shot["narration_exact"],
                    "Storyboard narration differs from original voice text")
        if shot.get("capture"):
            pitch.validate_frame(root, shot["capture"])
            require(english_copy(shot["capture"]["provenance_label"]), "Captured source label must be English")
            require(Path(shot["capture"]["image"]).name != "clarification-patient-accessible-mobile-375.png",
                    "Known Greek revised patient capture is not an English replacement")
            captures += 1
        elif shot["kind"] == "ui":
            raise ValueError("Actual UI scene is missing its root-reviewed English capture")
        if shot.get("capture_alt"):
            require(bool(shot.get("capture")), "Alternate capture requires its original view")
            pitch.validate_frame(root, shot["capture_alt"])
            require(english_copy(shot["capture_alt"]["provenance_label"]), "Alternate view source label must be English")
            require(Path(shot["capture_alt"]["image"]).name != "clarification-patient-accessible-mobile-375.png",
                    "Greek revised patient capture cannot be an alternate English view")
        if shot["id"] == "release":
            require(shot["kind"] == "process" and not shot.get("capture"), "Release is a CGI process illustration, not a historical release event")
        previous = end
    require(captures >= 2 and len(identifiers) == len(set(identifiers)), "Need actual UI evidence and unique shots")
    require(abs(previous - max(alignment["character_end_times_seconds"])) <= 1 / FPS,
            "No lead/tail padding or dropped voice is permitted")
    return plan, alignment


def stream_frames(root, shots, frames, output, cues=()):
    """Constant frame memory; only bounded QA images are written, never a PNG sequence."""
    canvas = graphics.CinematicCanvas(root)
    sample_frames = {}
    for index, shot in enumerate(shots):
        duration = shot["end_seconds"] - shot["start_seconds"]
        for phase in (0.2, 0.7):
            frame = min(frames - 1, round((shot["start_seconds"] + duration * phase) * FPS))
            sample_frames[frame] = (shot["id"], phase)
    qa_indices = {round(index * (len(shots) - 1) / max(1, min(11, len(shots) - 1))) for index in range(min(12, len(shots)))}
    qa_frames = {min(frames - 1, round((shot["start_seconds"] + (shot["end_seconds"] - shot["start_seconds"]) / 2) * FPS)):
                 f"{index:02}-{shot['id']}.png" for index, shot in enumerate(shots) if index in qa_indices}
    (output / "qa").mkdir()
    samples = []
    log = output / "motion-encoder.log"
    with log.open("xb") as stderr:
        process = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                                    "-pixel_format", "rgb24", "-video_size", "1920x1080", "-framerate", str(FPS),
                                    "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", "motion-visuals.mp4"],
                                   cwd=output, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            for number in range(frames):
                seconds = number / FPS
                image = canvas.frame(shots, seconds, cues)
                raw = image.tobytes()
                process.stdin.write(raw)
                if number in sample_frames:
                    identifier, phase = sample_frames[number]
                    shot = next(item for item in shots if item["id"] == identifier)
                    # Sample the uncaptioned scene as well, so changing subtitles cannot fake motion.
                    scene = canvas.scene(shot, seconds).convert("RGB")
                    samples.append({"shot": identifier, "phase": phase, "frame": number,
                                    "frame_rgb_sha256": hashlib.sha256(raw).hexdigest(),
                                    "uncaptioned_scene_rgb_sha256": hashlib.sha256(scene.tobytes()).hexdigest(),
                                    "object_state": graphics.motion_state(shot["kind"], seconds - shot["start_seconds"],
                                                                          shot["end_seconds"] - shot["start_seconds"])})
                if number in qa_frames:
                    image.resize((960, 540), canvas.Image.Resampling.LANCZOS).save(output / "qa" / qa_frames[number])
            process.stdin.close()
            require(process.wait(timeout=180) == 0, "Motion encoder failed; preserve raw log and partial video")
        except BaseException:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            raise
    for shot in shots:
        pair = [item for item in samples if item["shot"] == shot["id"]]
        require(len(pair) == 2 and pair[0]["uncaptioned_scene_rgb_sha256"] != pair[1]["uncaptioned_scene_rgb_sha256"],
                "Procedural motion is absent from a scene")
        require(pair[0]["object_state"] != pair[1]["object_state"], "Object geometry did not advance")
    return samples


def prototype(output):
    require(not output.exists(), "Preserve prior design prototype")
    output.mkdir(parents=True)
    output = output.resolve()
    shot = graphics.prototype_shot()
    samples = stream_frames(output, [shot], 8 * FPS, output)
    proof = {"status": "rendered_silent_visual_design_prototype_not_submission_video", "seconds": 8, "fps": FPS,
             "representation": "Procedural CGI illustration. No application footage or model execution.",
             "video_sha256": legacy.digest(output / "motion-visuals.mp4"), "motion_samples": samples,
             "model_calls": 0, "application_requests": 0, "publication": False}
    (output / "motion-proof.json").write_bytes(legacy.encoded(proof))
    print(json.dumps({"status": proof["status"], "video_sha256": proof["video_sha256"]}))


def render(root, expected, output, manifest_name="manifest.json"):
    plan, alignment = load_plan(root, expected, manifest_name)
    require(not output.exists(), "Preserve earlier motion render and failed partials")
    output.mkdir(parents=True)
    output = output.resolve()
    original = legacy.local_file(root, plan["narration"]["audio"], {".mp3", ".wav"})
    raw_master = output / "continuous-narration.wav"
    pitch.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original), "-vn", "-ac", "1", "-ar", str(pitch.RATE),
               "-c:a", "pcm_s16le", str(raw_master)], report=output / "audio-decode.log")
    seconds = float(legacy.probe(raw_master)["format"]["duration"])
    require(abs(seconds - plan["shots"][-1]["end_seconds"]) <= 1 / FPS, "Motion edit drops or pads original voice")
    frames = math.ceil(seconds * FPS)
    require(140 < frames / FPS < 180, "Full motion pitch must be bounded and strictly under three minutes")
    source_signal = pitch.pcm_signal_metrics(raw_master)
    normalized, normalization = pitch.normalize_audio(raw_master, output, seconds)
    cues = pitch.aligned_captions(plan["narration"]["text"], alignment, seconds)
    samples = stream_frames(root, plan["shots"], frames, output, cues)
    video = output / "medplain-motion.mp4"
    pitch.run(["ffmpeg", "-nostdin", "-v", "error", "-i", "motion-visuals.mp4", "-i", normalized.name,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-ar", str(pitch.RATE), "-movflags", "+faststart", video.name], cwd=output, report=output / "final-mux.log")
    captions = output / "medplain-motion.srt"
    captions.write_text("\n\n".join(f"{index}\n{legacy.clock(a)} --> {legacy.clock(b)}\n{text}"
                                   for index, (a, b, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")
    proof = {"status": "rendered_motion_pitch_pending_quality_and_human_review", "schema": SCHEMA,
             "manifest_sha256": expected, "manifest_name": manifest_name,
             "runtime_commit": legacy.RUNTIME, "repository_commit": plan["repository_commit"],
             "representation": REPRESENTATION, "english_review_sha256": plan["english_review_sha256"],
             "audio_policy": plan["audio_policy"], "source_audio_sha256": legacy.digest(original),
             "continuous_pcm_sha256": legacy.digest(raw_master), "continuous_audio_seconds": seconds,
             "source_pcm_signal": source_signal, "normalized_pcm_signal": pitch.pcm_signal_metrics(normalized),
             "normalization": normalization, "expected_frames": frames, "visual_rounding_seconds": frames / FPS - seconds,
             "shots": plan["shots"], "motion_samples": samples, "written_qa_frames": min(12, len(plan["shots"])),
             "video_sha256": legacy.digest(video), "captions_sha256": legacy.digest(captions),
             "model_calls": 0, "application_requests": 0, "publication": False}
    (output / "render-proof.json").write_bytes(legacy.encoded(proof))
    gate = pitch.check(video, captions, output / "render-proof.json", output / "media-gate.json")
    print(json.dumps({"status": gate["status"], "quality_ready": gate["quality_ready"],
                      "seconds": frames / FPS, "video_sha256": proof["video_sha256"]}))
    require(gate["quality_ready"], "Motion media failed existing audio/caption gates; all partials remain")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    intro = sub.add_parser("prototype")
    intro.add_argument("--output", type=Path, required=True)
    full = sub.add_parser("render")
    full.add_argument("--inputs", type=Path, required=True)
    full.add_argument("--manifest-sha256", required=True)
    full.add_argument("--manifest-name", choices=("manifest.json", "derived-manifest.json"), default="manifest.json")
    full.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prototype":
        prototype(args.output)
    else:
        render(args.inputs.resolve(), args.manifest_sha256, args.output, args.manifest_name)


if __name__ == "__main__":
    main()
