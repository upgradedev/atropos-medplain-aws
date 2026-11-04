"""Admit only exact independently reviewed real-time capture source; absent request is source-only."""
import json
import os
from pathlib import Path
import re

import demo_video as media

FILES = {"recorder": "scripts/realtime_careflow.py", "checker": "scripts/check_realtime_source.py",
         "compositor": "scripts/realtime_pitch.py",
         "validator": "scripts/prepare_realtime_request.py", "workflow": ".github/workflows/realtime-pitch.yml",
         "media_helpers": "scripts/demo_video.py", "original_careflow_journey": "scripts/careflow_browser_smoke.py",
         "original_capture": "scripts/final_pitch_capture.py", "audio_helpers": "scripts/pitch_video.py",
         "final_renderer": "scripts/final_pitch.py", "final_graphics": "scripts/final_pitch_graphics.py",
         "motion_graphics": "scripts/motion_graphics.py", "motion_helpers": "scripts/motion_video.py"}


def validate(root, request):
    media.require(request["status"] == "root_requested_after_independent_source_review", "Real independent source review required")
    media.require(media.HASH.fullmatch(request["independent_review_sha256"] or ""), "Actual independent review hash required")
    media.require(request["mode"] in {"capture", "compose"} and re.fullmatch(r"[0-9a-f]{40}", request["application_source"]), "Exact root-reviewed application source required")
    media.require(type(request["reading_seconds"]) in {int, float} and 0 <= request["reading_seconds"] <= 4, "Bounded real reading time required")
    for name, relative in FILES.items():
        media.require(media.digest(root / relative) == request["code_sha256"][name], "Reviewed recording source changed: " + relative)
    pins = {"REALTIME_MODE": request["mode"], "APPLICATION_SOURCE": request["application_source"], "READING_SECONDS": str(request["reading_seconds"])}
    if request["mode"] == "compose":
        media.require(type(request["native_run_id"]) is int and request["native_run_id"] > 0
                      and re.fullmatch(r"medplain-realtime-private-evidence-[0-9a-f]{40}", request["native_artifact_name"]), "Exact actual recording artifact required")
        media.require(re.fullmatch(r"medplain-final-inputs-[a-zA-Z0-9._-]{1,60}", request["source_tag"]), "Reviewed unchanged voice/CGI release required")
        for key in ("native_proof_sha256", "recording_review_sha256", "source_archive_sha256", "source_manifest_sha256"):
            media.require(media.HASH.fullmatch(request[key] or ""), "Exact reviewed input hash required: " + key)
        review_path = root / "video/realtime-recording-review.json"
        media.require(media.digest(review_path) == request["recording_review_sha256"], "Actual recording review changed")
        review = json.loads(review_path.read_bytes())
        media.require(review["status"] == "approved_for_realtime_composition" and review["privacy_reviewed"] is True
                      and review["ui_pixels_reviewed"] is True and review["recording_proof_sha256"] == request["native_proof_sha256"], "Actual native privacy/pixel review is not approved")
        media.require(type(request["ui_start_seconds"]) in {int, float} and 0 <= request["ui_start_seconds"] <= 45, "Reviewed UI start required")
        pins.update(NATIVE_RUN_ID=str(request["native_run_id"]), NATIVE_ARTIFACT_NAME=request["native_artifact_name"],
                    NATIVE_PROOF_SHA=request["native_proof_sha256"], RECORDING_REVIEW_SHA=request["recording_review_sha256"],
                    SOURCE_TAG=request["source_tag"], SOURCE_ARCHIVE_SHA=request["source_archive_sha256"],
                    SOURCE_MANIFEST_SHA=request["source_manifest_sha256"], UI_START_SECONDS=str(request["ui_start_seconds"]))
    return pins


def main():
    media.require(os.environ["GITHUB_REPOSITORY"] == "upgradedev/atropos-medplain-aws", "Wrong recording repository")
    media.require(os.environ["GITHUB_EVENT_NAME"] in {"push", "workflow_dispatch"}, "Unsupported recording trigger")
    path = Path("video/realtime-render-request.json")
    if not path.exists():
        media.require(os.environ["GITHUB_EVENT_NAME"] == "push", "Dispatch needs a committed reviewed request")
        pins, render = {}, False
    else:
        pins, render = validate(Path.cwd(), json.loads(path.read_bytes())), True
    if render:
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in pins.items()))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("capture=" + str(render).lower() + "\n")
    print(json.dumps({"capture": render, "pins": pins, "clinical_provider_calls_authorized": False,
                      "synthesis_authorized": False, "public_publication_authorized": False}))


if __name__ == "__main__":
    main()
