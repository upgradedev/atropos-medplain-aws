"""Admit only exact independently reviewed real-time capture source; absent request is source-only."""
import json
import os
from pathlib import Path
import re

import demo_video as media

FILES = {"recorder": "scripts/realtime_careflow.py", "checker": "scripts/check_realtime_source.py",
         "validator": "scripts/prepare_realtime_request.py", "workflow": ".github/workflows/realtime-pitch.yml",
         "media_helpers": "scripts/demo_video.py", "original_careflow_journey": "scripts/careflow_browser_smoke.py",
         "original_capture": "scripts/final_pitch_capture.py"}


def validate(root, request):
    media.require(request["status"] == "root_requested_after_independent_source_review", "Real independent source review required")
    media.require(media.HASH.fullmatch(request["independent_review_sha256"] or ""), "Actual independent review hash required")
    media.require(request["mode"] == "capture" and re.fullmatch(r"[0-9a-f]{40}", request["application_source"]), "Exact root-reviewed application source required")
    media.require(type(request["reading_seconds"]) in {int, float} and 0 <= request["reading_seconds"] <= 4, "Bounded real reading time required")
    for name, relative in FILES.items():
        media.require(media.digest(root / relative) == request["code_sha256"][name], "Reviewed recording source changed: " + relative)
    return {"APPLICATION_SOURCE": request["application_source"], "READING_SECONDS": str(request["reading_seconds"])}


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
