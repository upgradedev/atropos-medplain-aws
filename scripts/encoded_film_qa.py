"""CI-only pixel extraction from an admitted completed film, without recomposition or replay."""
from __future__ import annotations

import argparse
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

SOURCE = "db0a25ad4865a36d2f8196be26e2f365abded2b4"
RECORDER = "4a5cfd5ed26955c408c0e2a54c1f93943a5e11796d7e506768b99ab353a168db"
NATIVE_ZIP = "878dd1653638752b1cfcbe208822c781d8b00d5bdc7eb9f0017ff8425c37833c"
NATIVE_PROOF = "485d924caedfdca809ea3f47915d102a3e6080cf216af3db92236c9be1aedad2"
HASH = re.compile(r"[0-9a-f]{64}")


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def admission(value, script_sha):
    if value["status"] == "BLOCKED_requires_completed_composition_and_root_admission":
        return False
    require(value["status"] == "root_requested_existing_encoded_film_qa", "Explicit root read-only QA admission required")
    require(value["application_source"] == SOURCE and value["qa_source_sha256"] == script_sha, "Exact source bindings required")
    for name in ("archive_sha256", "movie_sha256", "render_proof_sha256", "media_gate_sha256"):
        require(HASH.fullmatch(value[name] or ""), "Missing actual completed artifact hash: " + name)
    require(type(value["compose_run_id"]) is int and value["compose_run_id"] > 0
            and type(value["artifact_id"]) is int and value["artifact_id"] > 0
            and re.fullmatch(r"[0-9a-f]{40}", value["compose_head"] or "")
            and value["artifact_name"] == "medplain-realtime-composition-" + value["compose_head"],
            "Exact actual composition run/artifact/head required")
    return True


def safe_zip(path, expected):
    require(digest(path) == expected, "Accepted archive bytes changed")
    with zipfile.ZipFile(path) as archive:
        rows = archive.infolist()
        require(0 < len(rows) <= 150 and sum(row.file_size for row in rows) < 300_000_000, "Archive exceeds bounded QA scope")
        require(len({row.filename for row in rows}) == len(rows), "Duplicate archive paths")
        for row in rows:
            relative = PurePosixPath(row.filename)
            require(not relative.is_absolute() and ".." not in relative.parts and ":" not in row.filename
                    and "\\" not in row.filename and (row.external_attr >> 16) & 0o170000 != 0o120000,
                    "Unsafe archived path")
            with archive.open(row) as stream:
                while stream.read(1024 * 1024):
                    pass  # Read to CRC-checked EOF, without retaining unrelated audio copies.


def extract(args, request):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Media decoding must run in CI")
    require(not args.output.exists(), "Preserve prior output evidence")
    safe_zip(args.archive, request["archive_sha256"])
    safe_zip(args.native_archive, NATIVE_ZIP)
    with zipfile.ZipFile(args.native_archive) as archive:
        raw = archive.read("realtime-ui/recording-proof.json")
    require(hashlib.sha256(raw).hexdigest() == NATIVE_PROOF, "Actual native proof changed")
    native = json.loads(raw)
    require(native["application_source"] == SOURCE and native["recorder_sha256"] == RECORDER, "Actual native identity changed")
    with zipfile.ZipFile(args.archive) as archive:
        proof_raw = archive.read("realtime-pitch/render-proof.json")
        gate_raw = archive.read("realtime-pitch/media-gate.json")
        require(hashlib.sha256(proof_raw).hexdigest() == request["render_proof_sha256"]
                and hashlib.sha256(gate_raw).hexdigest() == request["media_gate_sha256"], "Actual render/gate evidence changed")
        proof, gate = json.loads(proof_raw), json.loads(gate_raw)
        require(proof["runtime_commit"] == proof["recorded_application_source"] == proof["requested_application_source"] == SOURCE
                and proof["recording_proof_sha256"] == NATIVE_PROOF and proof["reviewed_recorder_sha256"] == RECORDER
                and proof["no_time_compression"] is True and proof["publication"] is False,
                "Completed film does not bind the reviewed actual UI source and clock")
        require(gate["quality_ready"] is True and proof["video_sha256"] == gate["video_sha256"] == request["movie_sha256"],
                "Actual film quality gate/hash has not passed")
        args.output.mkdir(parents=True)
        movie = args.output / "source-encoded-film.mp4"
        with archive.open("realtime-pitch/atropos-medplain-realtime.mp4") as source, movie.open("xb") as target:
            while block := source.read(1024 * 1024):
                target.write(block)
    require(digest(movie) == request["movie_sha256"], "Encoded movie bytes changed")
    info = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(movie)],
                          capture_output=True, check=True, timeout=30).stdout
    (args.output / "ffprobe.json").write_bytes(info)
    metadata = json.loads(info)
    video = next(row for row in metadata["streams"] if row["codec_type"] == "video")
    rate = Fraction(video["avg_frame_rate"])
    duration = float(metadata["format"]["duration"])
    require(rate == 30 and 140 < duration < 180 and int(video["nb_frames"]) == proof["expected_frames"],
            "Expected completed constant-rate film required")
    samples = [{"reason": "context_history_closing", "requested_seconds": float(t)} for t in (0, 8, 15, 111, 118, 130, 142, 148)]
    offset, start = proof["ui_film_offset"], proof["ui_clock_start"]
    for event in native["events"]:
        if event["kind"] == "milestone":
            samples.append({"reason": "actual_native_milestone", "label": event.get("name", event.get("label", "milestone")),
                            "page": event["page"], "requested_seconds": offset + event["seconds"] - start})
        elif event["kind"] == "active_page":
            for delta in (-.15, .15):
                samples.append({"reason": "actual_page_switch_boundary", "page": event["page"], "delta": delta,
                                "requested_seconds": offset + event["seconds"] - start + delta})
    for sample in samples:
        sample["encoded_frame_index"] = max(0, min(proof["expected_frames"] - 1, math.floor(sample["requested_seconds"] * rate)))
        sample["encoded_frame_seconds"] = sample["encoded_frame_index"] / float(rate)
    indices = sorted({row["encoded_frame_index"] for row in samples})
    require(len(indices) <= 100, "Bounded semantic frame selection required")
    expression = "+".join(f"eq(n\\,{number})" for number in indices)
    commands = [
        ["ffmpeg", "-nostdin", "-v", "info", "-i", str(movie), "-an", "-vf", "select=" + expression + ",showinfo",
         "-vsync", "0", str(args.output / "encoded-%03d.png")],
        ["ffmpeg", "-nostdin", "-v", "info", "-i", str(movie), "-an", "-vf",
         "fps=1,scale=480:270,drawtext=text='%{pts\\:hms}':x=8:y=8:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.8,tile=4x3:padding=4:margin=4",
         "-vsync", "vfr", str(args.output / "contact-%02d.png")]]
    for number, command in enumerate(commands):
        with (args.output / f"extract-{number}.log").open("wb") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=360)
    frames = sorted(args.output.glob("encoded-*.png"))
    require(len(frames) == len(indices), "Encoded-frame sample coverage incomplete")
    mapping = dict(zip(indices, [frame.name for frame in frames]))
    for sample in samples:
        sample["file"] = mapping[sample["encoded_frame_index"]]
    receipt = {"status": "actual_encoded_pixels_extracted_requires_independent_final_review_and_owner_listening",
               "compose_run_id": request["compose_run_id"], "artifact_id": request["artifact_id"],
               "qa_source_sha256": digest(Path(__file__)), "request_sha256": digest(args.request),
               "archive_sha256": request["archive_sha256"], "movie_sha256": request["movie_sha256"],
               "render_proof_sha256": request["render_proof_sha256"], "native_recording_proof_sha256": NATIVE_PROOF,
               "application_source": SOURCE, "duration_seconds": duration, "frames": proof["expected_frames"],
               "extraction_commands": commands, "samples": samples,
               "representation": "Pixels decoded from the actual accepted MP4, including its source labels and captions. No recomposition, speed change or replay.",
               "limitation": "Selected frames and one-fps contacts do not establish every-frame privacy or audio naturalness.",
               "provider_calls": 0, "capture_replayed": False, "publication_authorized": False}
    receipt["files"] = [{"path": row.name, "sha256": digest(row)} for row in sorted(args.output.iterdir()) if row.is_file()]
    (args.output / "encoded-qa-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def self_check():
    assert not admission({"status": "BLOCKED_requires_completed_composition_and_root_admission"}, "a" * 64)
    value = {"status": "root_requested_existing_encoded_film_qa", "application_source": SOURCE,
             "qa_source_sha256": "a" * 64, "compose_run_id": 1, "artifact_id": 2, "compose_head": "b" * 40,
             "artifact_name": "medplain-realtime-composition-" + "b" * 40,
             **{key: "c" * 64 for key in ("archive_sha256", "movie_sha256", "render_proof_sha256", "media_gate_sha256")}}
    assert admission(value, "a" * 64)
    for key, changed in (("status", "draft"), ("qa_source_sha256", "d" * 64), ("application_source", "main"),
                         ("movie_sha256", None), ("artifact_name", "latest"), ("compose_run_id", 0)):
        bad = deepcopy(value)
        bad[key] = changed
        try:
            admission(bad, "a" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe admission: " + key)
    print("PASS: blocked default and six negative admission controls; no actual media admission or decoding claimed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--request", type=Path, default=Path("video/encoded-film-qa-request.json"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--native-archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    request = json.loads(args.request.read_bytes())
    allowed = admission(request, digest(Path(__file__)))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("admitted=" + str(allowed).lower() + "\n")
    if not allowed:
        print("BLOCKED: no completed composition and exact root admission; no artifact request made")
        return
    if args.output:
        extract(args, request)


if __name__ == "__main__":
    main()
