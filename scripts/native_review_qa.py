"""Read-only CI sampling of the one already-spent, hash-pinned native recording."""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

RUN = 41821289683
ARTIFACT = 13318070109
ARCHIVE_SHA = "878dd1653638752b1cfcbe208822c781d8b00d5bdc7eb9f0017ff8425c37833c"
PROOF_SHA = "485d924caedfdca809ea3f47915d102a3e6080cf216af3db92236c9be1aedad2"
APP_SOURCE = "db0a25ad4865a36d2f8196be26e2f365abded2b4"
RECORDER_SHA = "4a5cfd5ed26955c408c0e2a54c1f93943a5e11796d7e506768b99ab353a168db"


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_proof(path, expected=ARCHIVE_SHA):
    require(digest(path) == expected, "Retained complete archive hash changed")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        require(0 < len(members) <= 100 and sum(row.file_size for row in members) < 100_000_000,
                "Archive exceeds read-only evidence bounds")
        names = [row.filename for row in members]
        require(len(names) == len(set(names)), "Duplicate archive paths")
        for row in members:
            relative = PurePosixPath(row.filename)
            require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in row.filename
                    and ":" not in row.filename and (row.external_attr >> 16) & 0o170000 != 0o120000,
                    "Unsafe archived path")
            with archive.open(row) as stream:
                while stream.read(1024 * 1024):
                    pass
        raw = archive.read("realtime-ui/recording-proof.json")
        require(hashlib.sha256(raw).hexdigest() == PROOF_SHA, "Actual recording proof changed")
        proof = json.loads(raw)
        require(proof["application_source"] == APP_SOURCE and proof["recorder_sha256"] == RECORDER_SHA,
                "Actual application/recorder identity changed")
        require(proof["journey"]["provider_calls"] == 0 and proof["journey"]["all_mutations_via_actual_ui"],
                "Only already-recorded UI fixture evidence is allowed")
        require(len(proof["page_videos"]) == 2, "Expected two recorded page contexts")
        for row in proof["page_videos"]:
            name = "realtime-ui/" + row["native_video"]
            with archive.open(name) as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == row["sha256"],
                        "Actual native video hash changed")
    return proof


def run(command, log):
    with log.open("wb") as stream:
        subprocess.run(command, check=True, stdout=stream, stderr=subprocess.STDOUT, timeout=180)


def extract(path, output):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Heavy native decoding is CI-only")
    proof = archive_proof(path)
    output.mkdir(parents=True, exist_ok=False)
    records = []
    with zipfile.ZipFile(path) as archive:
        for row in proof["page_videos"]:
            alias = row["alias"]
            require(alias in {"console", "patient"}, "Unexpected page alias")
            folder = output / alias
            folder.mkdir()
            native = folder / "source.webm"
            with archive.open("realtime-ui/" + row["native_video"]) as source, native.open("xb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(native)],
                                   check=True, capture_output=True, timeout=30).stdout
            (folder / "ffprobe.json").write_bytes(probe)
            metadata = json.loads(probe)
            duration = float(metadata["format"]["duration"])
            rate = float(Fraction(metadata["streams"][0]["avg_frame_rate"]))
            require(0 < duration < 120 and 10 <= rate <= 60, "Unexpected native duration/rate")
            # Exact same measured creation/closure bounds; this is a sampling estimate, not subframe synchronization.
            low = max(row["created_before_seconds"] - 1 / rate, row["closed_before_seconds"] - duration - 1 / rate)
            high = min(row["created_after_seconds"] + 1 / rate, row["closed_after_seconds"] - duration + 1 / rate)
            require(low <= high and high - low <= .5, "Unbounded native clock")
            origin = (low + high) / 2
            run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(native), "-an", "-vf", "fps=1,showinfo",
                 "-frames:v", "120", str(folder / "second-%03d.png")], folder / "one-fps.log")
            run(["ffmpeg", "-nostdin", "-v", "error", "-framerate", "1", "-i", str(folder / "second-%03d.png"),
                 "-vf", "scale=480:270,tile=4x3:padding=4:margin=4", "-vsync", "vfr", str(folder / "sheet-%02d.png")],
                folder / "contact-sheets.log")
            sensitive = [event for event in proof["events"] if event.get("page") == alias and
                         (event["kind"] == "active_page" or event["kind"] == "milestone")]
            seeks = sorted({round(max(0, min(duration - 1 / rate, event["seconds"] - origin + delta)), 6)
                            for event in sensitive for delta in (-.15, .15)})
            for index, seek in enumerate(seeks):
                run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(seek), "-i", str(native), "-frames:v", "1",
                     str(folder / f"boundary-{index:03d}.png")], folder / f"boundary-{index:03d}.log")
            records.append({"page": alias, "source_sha256": row["sha256"], "duration": duration,
                            "native_origin_lower": low, "native_origin_upper": high, "selected_sampling_origin": origin,
                            "boundary_requested_native_seconds": seeks,
                            "one_fps_selection": "FFmpeg fps=1; review one-fps.log for filtered PTS; no source speed change",
                            "contact_sheets": "4 by 3, ordered full native recording; individual PNGs retain 1280x720 pixels"})
            native.unlink()  # Exact input remains in complete retained ZIP; QA artifact avoids duplicating it.
    receipt = {"status": "actual_native_frames_extracted_REQUIRES_INDEPENDENT_PRIVACY_AND_PIXEL_REVIEW",
               "source_run": RUN, "source_artifact": ARTIFACT, "archive_sha256": ARCHIVE_SHA,
               "recording_proof_sha256": PROOF_SHA, "application_source": APP_SOURCE, "recorder_sha256": RECORDER_SHA,
               "clinical_provider_calls": 0, "capture_replayed": False, "public_publication_authorized": False,
               "sampling_limitation": "One-fps and boundary samples cannot establish every-frame privacy; review both original native streams too.",
               "pages": records}
    receipt["files"] = [{"path": str(item.relative_to(output)).replace("\\", "/"), "sha256": digest(item)}
                        for item in sorted(output.rglob("*")) if item.is_file()]
    (output / "native-qa-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        extract(args.archive, args.output)
    else:
        proof = archive_proof(args.archive)
        print(json.dumps({"already_spent_archive_valid": True, "application_source": proof["application_source"],
                          "native_videos": len(proof["page_videos"]), "decode_performed": False}))


if __name__ == "__main__":
    main()
