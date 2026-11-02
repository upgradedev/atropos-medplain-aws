"""Dependency-free fail-closed controls for the media gate, never app tests."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

path = Path(__file__).with_name("demo_video.py")
spec = importlib.util.spec_from_file_location("demo_video", path)
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)

info = {"streams": [
    {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920,
     "height": 1080, "avg_frame_rate": "30/1", "nb_frames": "4200", "duration": "140.000000"},
    {"codec_type": "audio", "codec_name": "aac", "duration": "140.000000"}],
    "format": {"duration": "140.000000"}}
assert video.validate_media(info, [(0, 2), (2, 139.9)], 4200)["frames"] == 4200
controls = []
for name, mutate in [
    ("exactly_three_minutes", lambda x: x["format"].update(duration="180.000000")),
    ("audio_video_drift", lambda x: x["streams"][1].update(duration="140.034000")),
    ("missing_audio", lambda x: x["streams"].pop()),
    ("wrong_frame_count", lambda x: x["streams"][0].update(nb_frames="4199")),
    ("wrong_dimensions", lambda x: x["streams"][0].update(width=1280)),
    ("unsupported_audio_codec", lambda x: x["streams"][1].update(codec_name="opus")),
]:
    broken = deepcopy(info)
    mutate(broken)
    try:
        video.validate_media(broken, [(0, 2)], 4200)
    except ValueError:
        controls.append(name)
    else:
        raise AssertionError("Control failed: " + name)
try:
    video.validate_media(info, [(0, 140.001)], 4200)
except ValueError:
    controls.append("caption_out_of_bounds")
else:
    raise AssertionError("Out-of-bounds caption accepted")
with tempfile.TemporaryDirectory() as root:
    srt = Path(root) / "captions.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nOriginal source quotations remain visible.\n", encoding="utf-8")
    assert video.parse_srt(srt) == [(1, 3)]
    srt.write_text("1\n00:00:03,000 --> 00:00:01,000\nWrong order.\n", encoding="utf-8")
    try:
        video.parse_srt(srt)
    except ValueError:
        controls.append("reversed_caption")
    else:
        raise AssertionError("Reversed caption accepted")
print({"status": "passed_dependency_free_media_gate_controls", "negative_controls": controls,
       "real_render_or_publication_claimed": False, "model_calls": 0})

source_root = path.resolve().parent.parent
validator = source_root / "scripts/prepare_demo_video_request.py"
code_pins = {field: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for field, name in {
    "renderer_sha256": "scripts/demo_video.py",
    "checker_sha256": "scripts/check_demo_video_source.py",
    "request_validator_sha256": "scripts/prepare_demo_video_request.py",
    "workflow_sha256": ".github/workflows/demo-video.yml",
}.items()}
request_controls = []
with tempfile.TemporaryDirectory() as temporary:
    for name, expected_success in [
        ("no_request_no_render", True), ("valid_pinned_push", True), ("wrong_repository", False),
        ("unreviewed_request", False), ("malformed_manifest_digest", False),
        ("changed_renderer", False), ("unsafe_release_tag", False),
        ("valid_dispatch", True), ("bad_dispatch_digest", False),
    ]:
        case = Path(temporary) / name
        case.mkdir()
        for relative in ("scripts/demo_video.py", "scripts/check_demo_video_source.py",
                         "scripts/prepare_demo_video_request.py", ".github/workflows/demo-video.yml"):
            destination = case / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((source_root / relative).read_bytes())
        value = {"status": "root_requested_after_independent_source_and_input_review",
                 "independent_review_sha256": "a" * 64, "source_tag": "medplain-video-inputs-CONSTRUCTED-CONTROL",
                 "source_archive_sha256": "b" * 64, "source_manifest_sha256": "c" * 64, **code_pins}
        if name == "unreviewed_request":
            value["status"] = "draft"
        if name == "malformed_manifest_digest":
            value["source_manifest_sha256"] = "short"
        if name == "changed_renderer":
            value["renderer_sha256"] = "d" * 64
        if name == "unsafe_release_tag":
            value["source_tag"] = "medplain-video-inputs-bad\nINJECTED=value"
        if name != "no_request_no_render":
            (case / "video").mkdir()
            (case / "video/render-request.json").write_text(json.dumps(value), encoding="utf-8")
        env = {**os.environ, "GITHUB_REPOSITORY": "wrong/repository" if name == "wrong_repository" else "upgradedev/atropos-medplain-aws",
               "GITHUB_EVENT_NAME": "workflow_dispatch" if "dispatch" in name else "push",
               "GITHUB_ENV": str(case / "environment-output"), "GITHUB_OUTPUT": str(case / "step-output"),
               "SOURCE_TAG": value["source_tag"], "SOURCE_ARCHIVE_SHA": "b" * 64,
               "SOURCE_MANIFEST_SHA": "bad" if name == "bad_dispatch_digest" else "c" * 64}
        result = subprocess.run([sys.executable, "-B", str(validator)], cwd=case, env=env,
                                capture_output=True, timeout=10, check=False)
        assert (result.returncode == 0) == expected_success, "Request control failed: " + name
        if expected_success:
            expected = "render=false" if name == "no_request_no_render" else "render=true"
            assert (case / "step-output").read_text(encoding="utf-8").strip() == expected
        else:
            assert not (case / "step-output").exists() and not (case / "environment-output").exists()
        request_controls.append(name)
print({"status": "passed_actual_request_validator_CLI_controls", "controls": request_controls,
       "constructed_pins_are_not_actual_approval": True, "network_calls": 0, "model_calls": 0})
