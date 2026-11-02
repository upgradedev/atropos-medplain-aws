"""Validate root-supplied media pins. No request means no render on a source push."""
import hashlib
import json
import os
from pathlib import Path
import re

assert os.environ["GITHUB_REPOSITORY"] == "upgradedev/atropos-medplain-aws"
event = os.environ["GITHUB_EVENT_NAME"]
request = Path("video/render-request.json")
if event == "push" and not request.exists():
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("render=false\n")
    print("Source-only push: root has not supplied a reviewed video-input request. No render or inference.")
    raise SystemExit(0)
if event == "push":
    value = json.loads(request.read_bytes())
    assert value["status"] == "root_requested_after_independent_source_and_input_review"
    assert re.fullmatch(r"[0-9a-f]{64}", value["independent_review_sha256"])
    for field, path in {
        "renderer_sha256": "scripts/demo_video.py",
        "checker_sha256": "scripts/check_demo_video_source.py",
        "request_validator_sha256": "scripts/prepare_demo_video_request.py",
        "workflow_sha256": ".github/workflows/demo-video.yml",
    }.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == value[field], "Reviewed media code changed"
    pins = {"SOURCE_TAG": value["source_tag"], "SOURCE_ARCHIVE_SHA": value["source_archive_sha256"],
            "SOURCE_MANIFEST_SHA": value["source_manifest_sha256"]}
elif event == "workflow_dispatch":
    pins = {key: os.environ[key] for key in ("SOURCE_TAG", "SOURCE_ARCHIVE_SHA", "SOURCE_MANIFEST_SHA")}
else:
    raise ValueError("Unsupported video event")
assert re.fullmatch(r"medplain-video-inputs-[a-zA-Z0-9._-]{1,60}", pins["SOURCE_TAG"])
assert re.fullmatch(r"[0-9a-f]{64}", pins["SOURCE_ARCHIVE_SHA"])
assert re.fullmatch(r"[0-9a-f]{64}", pins["SOURCE_MANIFEST_SHA"])
with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
    stream.write("".join(f"{key}={value}\n" for key, value in pins.items()))
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
    stream.write("render=true\n")
print(json.dumps({"status": "validated_source_archive_pins_not_a_model_or_publication_permit", **pins}))
