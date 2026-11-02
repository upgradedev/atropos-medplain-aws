"""Render a narrated walkthrough from reviewed, hash-bound shipped UI pixels.

No browser, application API, AWS, inference, upload or dependency installation.
Run rendering in CI. Windows SAPI narration is prepared separately by root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import textwrap
import wave
import zipfile

RUNTIME = "efe6c3e7c596697b308fffa758ae377b6422e03f"
REPOSITORY_COMMIT = "1375b218a8d025f88489dc422a7cb413a0d42af4"
FPS = 30
WIDTH, HEIGHT, RATE = 1920, 1080, 48000
MAX_SECONDS = 180
HASH = re.compile(r"[0-9a-f]{64}")
KINDS = {"current_ui", "current_fixture", "current_prepared", "historical_actual", "historical_evidence"}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def local_file(root, relative, suffixes):
    require(isinstance(relative, str) and "\\" not in relative and ":" not in relative,
            "Use portable relative asset paths")
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()) and Path(relative).is_absolute() is False,
            "Asset escapes reviewed input directory")
    require(path.is_file() and path.suffix.lower() in suffixes, "Missing or unsupported asset")
    return path


def load_plan(root, expected):
    path = root / "manifest.json"
    require(HASH.fullmatch(expected or "") and digest(path) == expected, "Exact reviewed manifest digest required")
    plan = json.loads(path.read_bytes())
    require(plan["schema"] == "medplain-narrated-walkthrough-v1", "Unknown video schema")
    require(plan["runtime_commit"] == RUNTIME and plan["repository_commit"] == REPOSITORY_COMMIT,
            "Frozen live and repository identities must be explicit")
    require(plan["representation"] == "Narrated walkthrough assembled from captured UI screenshots; no continuous screen recording claimed.",
            "Screenshot representation must remain explicit")
    require(4 <= len(plan["beats"]) <= 9, "Require a bounded modular walkthrough")
    ids = []
    for beat in plan["beats"]:
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,23}", beat["id"]), "Unsafe beat identifier")
        ids.append(beat["id"])
        for field in ("title", "narration"):
            require(isinstance(beat[field], str) and beat[field].strip() and "\u2014" not in beat[field],
                    "Nonempty zero-em-dash title/narration required")
        require(5 <= beat["min_seconds"] <= 45, "Unbounded minimum beat time")
        if "max_seconds" in beat:
            require(beat["min_seconds"] <= beat["max_seconds"] <= 45, "Invalid reviewed beat budget")
        require(1 <= len(beat["frames"]) <= 5, "Require bounded source frames")
        fixed_frames = any("duration_seconds" in item for item in beat["frames"])
        if fixed_frames:
            require(all(type(item.get("duration_seconds")) in {int, float} and item["duration_seconds"] > 0
                        for item in beat["frames"]), "Require a positive duration for every explicitly timed frame")
            require(sum(item["duration_seconds"] for item in beat["frames"]) == beat["min_seconds"]
                    and beat.get("max_seconds") == beat["min_seconds"], "Explicit frame durations must fill the fixed beat")
        audio = local_file(root, beat["audio"], {".wav"})
        require(HASH.fullmatch(beat["audio_sha256"]) and digest(audio) == beat["audio_sha256"], "Narration WAV changed")
        for frame in beat["frames"]:
            image = local_file(root, frame["image"], {".png", ".jpg", ".jpeg"})
            require(HASH.fullmatch(frame["sha256"]) and digest(image) == frame["sha256"], "Captured UI image changed")
            with image.open("rb") as source:
                magic = source.read(8)
            require((image.suffix.lower() == ".png" and magic == b"\x89PNG\r\n\x1a\n")
                    or (image.suffix.lower() in {".jpg", ".jpeg"} and magic.startswith(b"\xff\xd8\xff")),
                    "Image extension and observed PNG/JPEG bytes differ")
            require(frame["kind"] in KINDS and re.fullmatch(r"[0-9a-f]{40}", frame["source_commit"]),
                    "Frame kind and full source identity required")
            label = frame["provenance_label"]
            require(isinstance(label, str) and 12 <= len(label) <= 180 and "\u2014" not in label,
                    "Always-visible source label required")
            require(frame["source_commit"][:7] in label, "Visible label omits source commit")
            lower = label.lower()
            if frame["kind"].startswith("current"):
                require(frame["source_commit"] == RUNTIME and "current" in lower, "Current UI must bind live EFE")
            if frame["kind"] == "current_prepared":
                require("prepared" in lower and "fixture" in lower, "Prepared UI must explicitly say fixture")
            if frame["kind"] == "current_fixture":
                require("fixture" in lower and "offline" in lower, "Source-CI UI must explicitly say offline fixture")
            if frame["kind"].startswith("historical"):
                require("historical" in lower, "Historical output cannot be presented as current")
            if frame["kind"] == "historical_actual":
                require("bedrock" in lower, "Actual historical model execution must be labelled")
            require(isinstance(frame["evidence_reference"], str) and frame["evidence_reference"],
                    "Captured frame provenance reference required")
            crop = frame.get("crop")
            require(crop is None or (len(crop) == 4 and all(type(v) is int and v >= 0 for v in crop)
                                     and crop[2] > 0 and crop[3] > 0), "Invalid source pixel crop")
    require(len(ids) == len(set(ids)), "Duplicate beat identifier")
    return plan


def unpack(archive, expected, output):
    require(HASH.fullmatch(expected or "") and digest(archive) == expected, "Input archive digest differs")
    require(not output.exists(), "Preserve prior extraction and partial results")
    with zipfile.ZipFile(archive) as z:
        files = z.infolist()
        require(len(files) <= 100 and sum(f.file_size for f in files) < 100_000_000, "Unbounded video inputs")
        require(len({f.filename for f in files}) == len(files), "Duplicate ZIP entry")
        for entry in files:
            destination = (output / entry.filename).resolve()
            require(destination.is_relative_to(output.resolve()) and "\\" not in entry.filename
                    and ":" not in entry.filename and ".." not in Path(entry.filename).parts,
                    "Unsafe ZIP path")
            require((entry.external_attr >> 16) & 0o170000 != 0o120000, "ZIP symlinks prohibited")
            require(entry.is_dir() or destination.suffix.lower() in {".json", ".png", ".jpg", ".jpeg", ".wav", ".txt"},
                    "Unexpected video source archive member")
        output.mkdir(parents=True)
        z.extractall(output)


def run(argv, cwd=None, timeout=180):
    subprocess.run(argv, cwd=cwd, check=True, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe(path):
    value = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                           check=True, capture_output=True, text=True, timeout=20)
    return json.loads(value.stdout)


def clock(seconds, ass=False):
    unit = 100 if ass else 1000
    count = round(seconds * unit)
    hours, count = divmod(count, 3600 * unit)
    minutes, count = divmod(count, 60 * unit)
    secs, frac = divmod(count, unit)
    return f"{hours}:{minutes:02}:{secs:02}.{frac:02}" if ass else f"{hours:02}:{minutes:02}:{secs:02},{frac:03}"


def captions(text, seconds):
    words = text.split()
    blocks = [" ".join(words[i:i + 17]) for i in range(0, len(words), 17)]
    result, elapsed = [], 0.0
    total = sum(len(block.split()) for block in blocks)
    for block in blocks:
        end = elapsed + seconds * len(block.split()) / total
        result.append((elapsed, end, "\n".join(textwrap.wrap(block, 78))))
        elapsed = end
    return result


def ass_text(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def write_ass(path, title, label, cues, clip_start, clip_seconds):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Title,DejaVu Sans,31,&H00F0F5FA,&H000000FF,&H00101820,&H00101820,1,0,0,0,100,100,0,0,1,1,0,7,48,48,28,1
Style: Source,DejaVu Sans,22,&H0082E1C5,&H000000FF,&H00101820,&H00101820,0,0,0,0,100,100,0,0,1,1,0,7,48,48,80,1
Style: Caption,DejaVu Sans,29,&H00FFFFFF,&H000000FF,&H00101820,&H00101820,0,0,0,0,100,100,0,0,3,2,0,2,80,80,45,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    end = clock(clip_seconds, True)
    lines = [f"Dialogue: 0,0:00:00.00,{end},Title,,0,0,0,,{ass_text(title)}",
             f"Dialogue: 0,0:00:00.00,{end},Source,,0,0,0,,{ass_text(label)}"]
    for start, stop, text in cues:
        a, b = max(0, start - clip_start), min(clip_seconds, stop - clip_start)
        if b > a:
            lines.append(f"Dialogue: 1,{clock(a, True)},{clock(b, True)},Caption,,0,0,0,,{ass_text(text)}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def render(root, expected, output):
    plan = load_plan(root, expected)
    require(not output.exists(), "Preserve a prior rendering, including failed partials")
    output.mkdir(parents=True)
    output = output.resolve()
    clips, proof, all_cues, frame_count = [], [], [], 0
    with wave.open(str(output / "master.wav"), "wb") as master:
        master.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        for beat in plan["beats"]:
            normalized = output / (beat["id"] + "-normalized.wav")
            run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(local_file(root, beat["audio"], {".wav"})),
                 "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(normalized)])
            with wave.open(str(normalized), "rb") as wav:
                audio_samples = wav.getnframes()
                audio = wav.readframes(audio_samples)
            seconds = audio_samples / RATE
            if "max_seconds" in beat:
                frames = math.ceil(beat["min_seconds"] * FPS)
                require(seconds <= frames / FPS <= beat["max_seconds"], "Narration exceeds reviewed beat budget; do not speed or truncate")
            else:
                frames = max(math.ceil((seconds + 1.25) * FPS), math.ceil(beat["min_seconds"] * FPS))
            require((frame_count + frames) / FPS < 179, "Source narration exceeds finite video budget")
            master.writeframes(audio)
            master.writeframes(b"\0\0" * (frames * (RATE // FPS) - audio_samples))
            cues = captions(beat["narration"], seconds)
            all_cues.extend((a + frame_count / FPS, b + frame_count / FPS, text) for a, b, text in cues)
            if all("duration_seconds" in item for item in beat["frames"]):
                counts = [round(item["duration_seconds"] * FPS) for item in beat["frames"]]
                require(sum(counts) == frames, "Frame-grid timings differ from reviewed beat")
            else:
                counts = [frames // len(beat["frames"])] * len(beat["frames"])
                counts[-1] += frames - sum(counts)
            offset = 0
            for index, (frame, count) in enumerate(zip(beat["frames"], counts)):
                name = f"{beat['id']}-{index:02}"
                ass = output / (name + ".ass")
                write_ass(ass, beat["title"], frame["provenance_label"], cues, offset / FPS, count / FPS)
                image = local_file(root, frame["image"], {".png", ".jpg", ".jpeg"})
                crop = frame.get("crop")
                if crop:
                    x, y, w, h = crop
                    size = probe(image)["streams"][0]
                    require(x + w <= size["width"] and y + h <= size["height"], "Crop exceeds captured source pixels")
                    prefix = f"crop={w}:{h}:{x}:{y},"
                else:
                    prefix = ""
                vf = prefix + f"scale=1824:740:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:140:color=0x0c131b,setsar=1,subtitles={ass.name}"
                run(["ffmpeg", "-nostdin", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(image),
                     "-vf", vf, "-frames:v", str(count), "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                     "-pix_fmt", "yuv420p", name + ".mp4"], cwd=output, timeout=600)
                clips.append(name + ".mp4")
                offset += count
            proof.append({"beat": beat["id"], "start_frame": frame_count, "frames": frames,
                          "audio_samples_48000": audio_samples, "narration_sha256": hashlib.sha256(beat["narration"].encode()).hexdigest(),
                          "frame_sources": beat["frames"]})
            frame_count += frames
    (output / "concat.txt").write_text("\n".join(f"file '{name}'" for name in clips) + "\n", encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-v", "error", "-f", "concat", "-safe", "1", "-i", "concat.txt", "-i", "master.wav",
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", str(RATE),
         "-t", str(frame_count / FPS), "-movflags", "+faststart", "medplain-demo.mp4"], cwd=output)
    srt = "\n\n".join(f"{i}\n{clock(a)} --> {clock(b)}\n{text}" for i, (a, b, text) in enumerate(all_cues, 1)) + "\n"
    (output / "medplain-demo.srt").write_text(srt, encoding="utf-8")
    metadata = {"status": "rendered_pending_media_and_human_review", "expected_frames": frame_count,
                "fps": FPS, "manifest_sha256": expected, "runtime_commit": RUNTIME, "repository_commit": REPOSITORY_COMMIT,
                "representation": plan["representation"], "beats": proof,
                "video_sha256": digest(output / "medplain-demo.mp4"), "captions_sha256": digest(output / "medplain-demo.srt"),
                "model_calls": 0, "application_requests": 0, "publication": False}
    (output / "render-proof.json").write_bytes(encoded(metadata))
    check(output / "medplain-demo.mp4", output / "medplain-demo.srt", output / "render-proof.json", output / "media-gate.json")
    (output / "qa").mkdir()
    for index, beat in enumerate(proof):
        run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(beat["start_frame"] / FPS + 1),
             "-i", str(output / "medplain-demo.mp4"), "-frames:v", "1", "-vf", "scale=960:-2",
             str(output / "qa" / f"{index + 1:02}-{beat['beat']}.png")])
    print(json.dumps({"video": str(output / "medplain-demo.mp4"), "frames": frame_count, "seconds": frame_count / FPS}))


def parse_srt(path):
    def seconds(value):
        h, m, sec = value.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    blocks = path.read_text(encoding="utf-8").strip().split("\n\n")
    result = []
    for i, block in enumerate(blocks, 1):
        lines = block.splitlines()
        require(len(lines) >= 3 and lines[0] == str(i), "Malformed caption numbering")
        require(re.fullmatch(r"\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}", lines[1]), "Malformed caption clock")
        start, end = map(seconds, lines[1].split(" --> "))
        require(0 <= start < end and (not result or start >= result[-1][1] - 0.001), "Invalid overlapping caption interval")
        require(1 <= len(lines[2:]) <= 3 and all(0 < len(x) <= 86 for x in lines[2:]), "Unreadable or empty captions")
        require("\u2014" not in "\n".join(lines[2:]), "Prohibited caption punctuation")
        result.append((start, end))
    return result


def validate_media(info, cues, expected_frames):
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    require(len(video) == len(audio) == 1, "Require exactly one video and audible narration stream")
    v, a = video[0], audio[0]
    require(v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and a["codec_name"] == "aac", "Unsupported public playback codecs")
    require(v["width"] == WIDTH and v["height"] == HEIGHT and v["avg_frame_rate"] == "30/1", "Unexpected video dimensions or frame rate")
    require(int(v["nb_frames"]) == expected_frames, "Rendered video frame count differs")
    vd, ad, duration = float(v["duration"]), float(a["duration"]), float(info["format"]["duration"])
    require(0 < vd < MAX_SECONDS and 0 < ad < MAX_SECONDS and duration < MAX_SECONDS, "Video/audio must be strictly under three minutes")
    require(abs(vd - ad) <= 1 / FPS, "Audio and video differ by more than one frame")
    require(abs(vd - expected_frames / FPS) <= 1 / FPS, "Video timing differs from immutable edit plan")
    require(cues and all(0 <= start < end <= min(vd, ad) for start, end in cues), "Caption exceeds video or audible narration stream")
    return {"video_seconds": vd, "audio_seconds": ad, "container_seconds": duration,
            "audio_video_difference_seconds": abs(vd - ad), "frame_tolerance_seconds": 1 / FPS,
            "frames": int(v["nb_frames"]), "caption_cues": len(cues)}


def check(video, captions_path, render_proof, output):
    require(not output.exists(), "Preserve an earlier media gate")
    proof = json.loads(render_proof.read_bytes())
    require(digest(video) == proof["video_sha256"] and digest(captions_path) == proof["captions_sha256"], "Rendered bytes changed")
    info = probe(video)
    result = validate_media(info, parse_srt(captions_path), proof["expected_frames"])
    result.update(status="passed_technical_media_gate_human_pixel_audio_and_publication_review_still_required",
                  source_manifest_sha256=proof["manifest_sha256"], video_sha256=digest(video),
                  captions_sha256=digest(captions_path), runtime_commit=RUNTIME, ffprobe=info,
                  source_truth_claims_review="Required separately; this technical gate does not verify clinical benefit or model efficacy.")
    output.write_bytes(encoded(result))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    unzip = sub.add_parser("unpack")
    unzip.add_argument("--archive", type=Path, required=True)
    unzip.add_argument("--sha256", required=True)
    unzip.add_argument("--output", type=Path, required=True)
    make = sub.add_parser("render")
    make.add_argument("--inputs", type=Path, required=True)
    make.add_argument("--manifest-sha256", required=True)
    make.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("check")
    for key in ("video", "captions", "render-proof", "output"):
        verify.add_argument("--" + key, type=Path, required=True)
    args = p.parse_args()
    if args.action == "unpack":
        unpack(args.archive, args.sha256, args.output)
    elif args.action == "render":
        render(args.inputs.resolve(), args.manifest_sha256, args.output)
    else:
        check(args.video, args.captions, args.render_proof, args.output)


if __name__ == "__main__":
    main()
