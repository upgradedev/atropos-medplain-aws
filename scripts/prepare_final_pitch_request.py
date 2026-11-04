"""No request means source-only. Rendering needs exact root-reviewed immutable pins."""
import hashlib
import json
import os
from pathlib import Path
import re

FILES = {"renderer": "scripts/final_pitch.py", "graphics": "scripts/final_pitch_graphics.py",
         "capture": "scripts/final_pitch_capture.py", "checker": "scripts/check_final_pitch_source.py",
         "request_validator": "scripts/prepare_final_pitch_request.py", "workflow": ".github/workflows/final-pitch.yml",
         "audio_helpers": "scripts/pitch_video.py", "legacy_helpers": "scripts/demo_video.py",
         "retained_graphics": "scripts/motion_graphics.py", "retained_motion": "scripts/motion_video.py"}


def validate(root, value):
    assert value["status"] == "root_requested_after_independent_source_and_input_review"
    assert re.fullmatch(r"[0-9a-f]{64}", value["independent_review_sha256"])
    assert value["mode"] in {"capture", "full"}
    for key, relative in FILES.items():
        with (root / relative).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == value["code_sha256"][key], "Reviewed code drift: " + relative
    assert value["application_source"] == "3cbe1dc18ca4e148ca4ee1d80a6305f4fbee7571"
    pins = {"FINAL_MODE": value["mode"]}
    if value["mode"] == "full":
        assert re.fullmatch(r"medplain-final-inputs-[a-zA-Z0-9._-]{1,60}", value["source_tag"])
        assert all(re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in ("source_archive_sha256", "source_manifest_sha256"))
        pins.update(SOURCE_TAG=value["source_tag"], SOURCE_ARCHIVE_SHA=value["source_archive_sha256"], SOURCE_MANIFEST_SHA=value["source_manifest_sha256"])
    return pins


def main():
    assert os.environ["GITHUB_REPOSITORY"] == "upgradedev/atropos-medplain-aws"
    assert os.environ["GITHUB_EVENT_NAME"] in {"push", "workflow_dispatch"}
    path = Path("video/final-pitch-render-request.json")
    if not path.exists():
        assert os.environ["GITHUB_EVENT_NAME"] == "push", "Explicit dispatch requires a reviewed committed request"
        pins, render = {}, False
    else:
        pins, render = validate(Path.cwd(), json.loads(path.read_bytes())), True
        if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
            assert pins["FINAL_MODE"] == os.environ["FINAL_MODE"], "Dispatch changed reviewed mode"
    if render:
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in pins.items()))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("render=" + str(render).lower() + "\n")
    print(json.dumps({"render": render, "pins": pins, "synthesis_authorized": False, "publication_authorized": False}))


if __name__ == "__main__":
    main()
