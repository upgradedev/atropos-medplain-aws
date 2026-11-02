"""Require an exact root-reviewed continuous-pitch request before any render.

A source-only push without video/pitch-render-request.json runs checks only.
No synthesis, application job, AWS operation, or publication is authorized here.
"""
import hashlib
import json
import os
from pathlib import Path
import re

FILES = {
    "renderer_sha256": "scripts/pitch_video.py",
    "legacy_helpers_sha256": "scripts/demo_video.py",
    "checker_sha256": "scripts/check_pitch_video_source.py",
    "live_readiness_sha256": "scripts/check_demo_video_live.py",
    "request_validator_sha256": "scripts/prepare_pitch_video_request.py",
    "workflow_sha256": ".github/workflows/pitch-video.yml",
}


def main():
    assert os.environ["GITHUB_REPOSITORY"] == "upgradedev/atropos-medplain-aws", "Wrong repository"
    event = os.environ["GITHUB_EVENT_NAME"]
    assert event in {"push", "workflow_dispatch"}, "Unsupported pitch-video event"
    request = Path("video/pitch-render-request.json")
    if event == "push" and not request.exists():
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("render=false\n")
        print("Source-only continuous-pitch checks. No root-reviewed request; no render, synthesis or publication.")
        return
    assert request.is_file(), "Dispatch requires the committed reviewed pitch request"
    value = json.loads(request.read_bytes())
    assert value["status"] == "root_requested_after_independent_source_and_input_review", "Unreviewed pitch request"
    assert re.fullmatch(r"[0-9a-f]{64}", value["independent_review_sha256"]), "Missing independent review binding"
    for field, name in FILES.items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == value[field], "Reviewed pitch code changed: " + name
    pins = {"SOURCE_TAG": value["source_tag"], "SOURCE_ARCHIVE_SHA": value["source_archive_sha256"],
            "SOURCE_MANIFEST_SHA": value["source_manifest_sha256"]}
    assert re.fullmatch(r"medplain-pitch-inputs-[a-zA-Z0-9._-]{1,60}", pins["SOURCE_TAG"]), "Unsafe input tag"
    assert all(re.fullmatch(r"[0-9a-f]{64}", pins[key]) for key in ("SOURCE_ARCHIVE_SHA", "SOURCE_MANIFEST_SHA")), "Invalid input digests"
    if event == "workflow_dispatch":
        assert all(os.environ[key] == value for key, value in pins.items()), "Dispatch differs from reviewed committed request"
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in pins.items()))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("render=true\n")
    print(json.dumps({"status": "validated_root_reviewed_continuous_pitch_inputs", **pins,
                      "synthesis_authorized": False, "publication_authorized": False}))


if __name__ == "__main__":
    main()
