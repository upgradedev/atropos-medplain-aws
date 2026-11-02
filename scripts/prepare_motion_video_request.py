"""Validate root-reviewed motion-code/input pins; no request means source-only CI."""
import hashlib
import json
import os
from pathlib import Path
import re

FILES = {"graphics_sha256": "scripts/motion_graphics.py", "renderer_sha256": "scripts/motion_video.py",
         "checker_sha256": "scripts/check_motion_video_source.py", "request_validator_sha256": "scripts/prepare_motion_video_request.py",
         "workflow_sha256": ".github/workflows/motion-video.yml", "audio_helpers_sha256": "scripts/pitch_video.py",
         "legacy_helpers_sha256": "scripts/demo_video.py", "live_readiness_sha256": "scripts/check_demo_video_live.py"}


def main():
    assert os.environ["GITHUB_REPOSITORY"] == "upgradedev/atropos-medplain-aws", "Wrong repository"
    event = os.environ["GITHUB_EVENT_NAME"]
    assert event in {"push", "workflow_dispatch"}, "Unsupported motion-video event"
    path = Path("video/motion-render-request.json")
    if event == "push" and not path.exists():
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("render=false\n")
        print("Source-only motion checks; no root request, rendering, synthesis or publication.")
        return
    value = json.loads(path.read_bytes())
    assert value["status"] == "root_requested_after_independent_source_and_input_review"
    assert re.fullmatch(r"[0-9a-f]{64}", value["independent_review_sha256"])
    assert value["mode"] in {"prototype", "full"}
    for field, name in FILES.items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == value[field], "Reviewed motion code changed: " + name
    if event == "workflow_dispatch":
        assert os.environ["MOTION_MODE"] == value["mode"], "Dispatch changed reviewed rendering mode"
    pins = {"MOTION_MODE": value["mode"]}
    if value["mode"] == "full":
        pins.update(SOURCE_TAG=value["source_tag"], SOURCE_ARCHIVE_SHA=value["source_archive_sha256"],
                    SOURCE_MANIFEST_SHA=value["source_manifest_sha256"])
        assert re.fullmatch(r"medplain-motion-inputs-[a-zA-Z0-9._-]{1,60}", pins["SOURCE_TAG"])
        assert all(re.fullmatch(r"[0-9a-f]{64}", pins[key]) for key in ("SOURCE_ARCHIVE_SHA", "SOURCE_MANIFEST_SHA"))
        if event == "workflow_dispatch":
            assert all(os.environ[key] == value for key, value in pins.items()), "Dispatch changed reviewed motion inputs"
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in pins.items()))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("render=true\n")
    print(json.dumps({"status": "validated_root_reviewed_motion_request", **pins,
                      "synthesis_authorized": False, "publication_authorized": False}))


if __name__ == "__main__":
    main()
