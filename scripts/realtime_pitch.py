"""CI-only composition of reviewed native UI recordings at their real monotonic times."""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import demo_video as media
import final_pitch as final
import final_pitch_graphics as graphics
import motion_graphics as design
import pitch_video as audio

FPS = 30


def require(value, reason):
    media.require(value, reason)


def recording_identity(proof, review, expected_source, expected_recorder_sha):
    require(re.fullmatch(r"[0-9a-f]{40}", expected_source or "") and media.HASH.fullmatch(expected_recorder_sha or ""),
            "Requested source and reviewed recorder hash are required")
    require(proof["application_source"] == review["application_source"] == expected_source,
            "Recording, independent review and requested application source must agree")
    require(proof["recorder_sha256"] == review["recorder_sha256"] == expected_recorder_sha,
            "Recording must use the exact independently reviewed recorder source")


def chronology(proof):
    require(proof["status"] == "completed_actual_ui_fixture_journey_pending_video_privacy_and_pixel_review", "Actual complete recording required")
    require(proof["journey"]["all_mutations_via_actual_ui"] and proof["journey"]["provider_calls"] == 0,
            "Only actual UI-driven offline fixture recording is eligible")
    events = proof["events"]
    require(events and all(audio.finite_number(row["seconds"]) for row in events), "Invalid real recording clock")
    require(all(left["seconds"] <= right["seconds"] for left, right in zip(events, events[1:])), "Recording time moves backwards")
    active = [row for row in events if row["kind"] == "active_page"]
    require(active and active[0]["page"] == "console", "Actual console start required")
    start, end = active[0]["seconds"], events[-1]["seconds"]
    require(0 <= start < end <= proof["wall_seconds"] < 120, "UI journey exceeded its actual time budget")
    windows, current, before = [], active[0]["page"], start
    for event in active[1:]:
        if event["page"] != current:
            require(event["seconds"] > before, "Unordered actual page switch")
            windows.append({"page": current, "start": before, "end": event["seconds"]})
            current, before = event["page"], event["seconds"]
    windows.append({"page": current, "start": before, "end": end})
    require(abs(sum(row["end"] - row["start"] for row in windows) - (end - start)) < 1e-6,
            "The entire actual UI clock must remain covered")
    return start, end, windows


def native_origin(row, seconds, frame_seconds):
    """Bound the origin with observed creation/close brackets and measured native duration."""
    require(audio.finite_number(seconds) and seconds > 0, "Empty native video")
    require(audio.finite_number(frame_seconds) and 0 < frame_seconds <= 0.1, "Measured native frame duration required")
    low = max(row["created_before_seconds"] - frame_seconds,
              row["closed_before_seconds"] - seconds - frame_seconds)
    high = min(row["created_after_seconds"] + frame_seconds,
               row["closed_after_seconds"] - seconds + frame_seconds)
    require(low <= high and high - low <= 0.5, "Native origin is not bounded by actual creation/closure; review synchronization")
    return {"selected_origin_seconds": (low + high) / 2, "origin_lower_seconds": low, "origin_upper_seconds": high,
            "uncertainty_seconds": high - low, "native_duration_seconds": seconds,
            "measured_native_frame_seconds": frame_seconds,
            "claim": "Origin bounded by actual metadata and measured video duration; no subframe synchronization asserted."}


class NativeDecoder:
    def __init__(self, path, output, alias, duration):
        self.log = (output / (alias + "-decode.log")).open("xb")
        self.process = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                                         "fps=30,scale=1280:720:flags=lanczos", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                        stdout=subprocess.PIPE, stderr=self.log)
        self.number, self.pixels = -1, None
        self.duration, self.tail_frame_holds = duration, []

    def frame(self, target, native_seconds):
        require(target >= self.number, "A source page was replayed or time reversed")
        require(math.floor(native_seconds * FPS) == target, "Native frame target must use its actual clock")
        while self.number < target:
            required, chunks = 1280 * 720 * 3, []
            while required:
                chunk = self.process.stdout.read(required)
                if not chunk:
                    require(not chunks and self.pixels is not None and target == self.number + 1
                            and self.duration - 1 / FPS <= native_seconds <= self.duration + 1 / FPS,
                            "Native source does not cover a real UI interval; preserve partial render")
                    require(self.process.wait(timeout=20) == 0, "Native decoder failed; no final-frame substitution permitted")
                    self.tail_frame_holds.append({"requested_index": target, "last_decoded_index": self.number,
                                                  "native_seconds": native_seconds, "measured_native_duration": self.duration,
                                                  "maximum_boundary_seconds": 1 / FPS,
                                                  "reason": "Successful decoder EOF within one final output frame; retain actual last pixels and full wall clock."})
                    return self.pixels
                chunks.append(chunk)
                required -= len(chunk)
            self.pixels = b"".join(chunks)
            self.number += 1
        return self.pixels

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=20)
        self.process.stdout.close()
        self.log.close()


class RealtimeCanvas(graphics.FinalCanvas):
    def __init__(self, inputs, plan, proof, start, end, windows, origins, decoders, ui_offset):
        super().__init__(inputs)
        self.plan, self.proof = plan, proof
        self.start, self.end, self.windows = start, end, windows
        self.origins, self.decoders, self.ui_offset = origins, decoders, ui_offset

    def frame(self, seconds, cues):
        local = seconds - self.ui_offset
        if not 0 <= local < self.end - self.start:
            # Keep contextual CGI/historical scenes. Old UI screenshots are never substituted for real-time footage.
            shot = next((row for row in self.plan["shots"] if seconds < row["end_seconds"]), self.plan["shots"][-1])
            if shot["kind"] != "ui":
                image, label = self.scene(shot, seconds), shot["honesty_label"]
            else:
                image = self.background.copy()
                illustration = {**shot, "kind": "process", "nodes": ["Source evidence", "Accepted question", "Human review"],
                                "title": "A source-bound next step", "subtitle": "The full UI journey retains its real actions and waits.",
                                "process_note": "Illustration. No extra patient fact or successful clinical outcome is supplied."}
                self.graph(image, illustration, design.motion_state("process", seconds - shot["start_seconds"], shot["end_seconds"] - shot["start_seconds"]), seconds - shot["start_seconds"])
                label = "CGI process illustration | synthetic prototype"
        else:
            wall = self.start + local
            window = next((row for row in self.windows if row["start"] <= wall < row["end"]), self.windows[-1])
            alias = window["page"]
            native_seconds = wall - self.origins[alias]["selected_origin_seconds"]
            require(native_seconds >= 0, "Actual page video has not started")
            pixels = self.decoders[alias].frame(math.floor(native_seconds * FPS), native_seconds)
            native = self.Image.frombytes("RGB", (1280, 720), pixels)
            image = self.background.copy()
            rendered = native.resize((1460, 821), self.Image.Resampling.LANCZOS)
            image.paste(rendered, (230, 100))
            self.Draw.Draw(image).rectangle((229, 99, 1691, 922), outline=(*design.MINT, 100), width=2)
            for event in self.proof["events"]:
                age = wall - event["seconds"]
                if event["kind"] == "click_target" and event["page"] == alias and 0 <= age < 0.7:
                    target = event["target"]
                    require(0 <= target["x"] <= 1280 and 0 <= target["y"] <= 720, "Recorded click target is outside its actual native viewport")
                    x, y = 230 + target["x"] * 1460 / 1280, 100 + target["y"] * 821 / 720
                    pulse = self.Image.new("RGBA", image.size)
                    radius = 12 + 22 * age / 0.7
                    self.Draw.Draw(pulse).ellipse((x - radius, y - radius, x + radius, y + radius),
                                                  outline=(*design.MINT, round(255 * (1 - age / 0.7))), width=4)
                    self.Draw.Draw(pulse).ellipse((x - 5, y - 5, x + 5, y + 5), fill=(*design.WHITE, 240))
                    image.alpha_composite(pulse)
            label = ("ACTUAL OFFLINE FIXTURE + STRANDS SDK | SOURCE " + self.proof["application_source"][:7]
                     + " | REAL TIME 1x | " + alias.upper() + " | " + f"{local:05.1f}s")
            self.text(image, "Circle = actual automation click target", (1295, 30), 16, design.MUTED, width=550)
            latest = next((event for event in reversed(self.proof["events"]) if event["seconds"] <= wall
                           and event["kind"] in {"milestone", "operator_reading_started", "operator_reading_finished", "click_target", "fill", "select", "check"}), None)
            if latest:
                action = ("Operator reading, actual elapsed time" if latest["kind"] == "operator_reading_started"
                          else latest.get("label", latest.get("name", "Actual UI interaction")).replace("_", " "))
                self.text(image, action, (235, 925), 22, design.MINT, width=1450)
        self.text(image, "ATROPOS MEDPLAIN", (90, 27), 23, design.MINT, bold=True)
        self.text(image, label, (90, 62), 18, design.WHITE, width=1740)
        for before, after, text in cues:
            if before <= seconds < after:
                lines = text.splitlines()
                draw = self.Draw.Draw(image)
                width = max(draw.textlength(line, font=self.font(36)) for line in lines)
                height = len(lines) * 48 + 24
                self.panel(image, ((1920 - width) / 2 - 32, 1070 - height, (1920 + width) / 2 + 32, 1070),
                           16, accent=(44, 77, 90), fill=(3, 10, 16, 245))
                for index, line in enumerate(lines):
                    line_width = draw.textlength(line, font=self.font(36))
                    self.text(image, line, ((1920 - line_width) / 2, 1075 - height + index * 48), 36)
                break
        return image.convert("RGB")


def check_media(video, captions, proof, output):
    """Retain the exact existing media/audio algorithms with the actual recorded source identity."""
    require(not output.exists() and re.fullmatch(r"[0-9a-f]{40}", proof["runtime_commit"]), "New source-bound video verdict required")
    require(proof["runtime_commit"] == proof["recorded_application_source"] and proof["audio_policy"] == audio.POLICY
            and 0 <= proof["visual_rounding_seconds"] < 1 / FPS, "Actual recorded source or strict audio policy changed")
    require(media.digest(video) == proof["video_sha256"] and media.digest(captions) == proof["captions_sha256"], "Media bytes changed")
    info, intervals = media.probe(video), media.parse_srt(captions)
    verdict = media.validate_media(info, intervals, proof["expected_frames"])
    silence = audio.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af", "silencedetect=noise=-45dB:d=0.8", "-f", "null", "-"], report=output.with_name("audio-silence.log"))
    loudness = audio.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], report=output.with_name("audio-loudness.log"))
    qa = audio.parse_audio_reports(silence.stderr.decode(errors="replace"), loudness.stderr.decode(errors="replace"), verdict["audio_seconds"])
    pcm = output.with_name("final-audio-decoded.wav")
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(audio.RATE), "-c:a", "pcm_s16le", str(pcm)], report=output.with_name("final-audio-decode.log"))
    qa.update({key: proof[key] for key in ("source_pcm_signal", "normalized_pcm_signal", "normalization")})
    qa["final_pcm_signal"] = audio.pcm_signal_metrics(pcm)
    verdict.update(audio.quality_verdict(qa, intervals))
    verdict.update(runtime_commit=proof["runtime_commit"], context_inputs_runtime_commit=proof["context_inputs_runtime_commit"],
                   audio_qa=qa, ffprobe=info, video_sha256=media.digest(video), captions_sha256=media.digest(captions),
                   owner_review="Actual listening, native-recording privacy, pixels and claims remain human review items.")
    output.write_bytes(media.encoded(verdict))
    return verdict


def render(inputs, manifest_sha, recordings, proof_sha, review_path, review_sha, ui_offset, output, expected_source, expected_recorder_sha):
    require(not output.exists(), "Preserve previous continuous films and failures")
    plan, aligned = final.load_plan(inputs, manifest_sha, allow_capture_slots=True)
    proof_path = media.local_file(recordings, "recording-proof.json", {".json"})
    require(media.digest(proof_path) == proof_sha and media.digest(review_path) == review_sha, "Actual recording/review bytes changed")
    proof, review = json.loads(proof_path.read_bytes()), json.loads(review_path.read_bytes())
    require(review["status"] == "approved_for_realtime_composition" and review["recording_proof_sha256"] == proof_sha
            and review["privacy_reviewed"] is True and review["ui_pixels_reviewed"] is True and review["reviewer"].strip(),
            "Actual native-video privacy/pixel review required")
    recording_identity(proof, review, expected_source, expected_recorder_sha)
    start, end, windows = chronology(proof)
    require(audio.finite_number(ui_offset) and 0 <= ui_offset <= 45, "Reviewed finite UI start required")
    voice_end = max(aligned["character_end_times_seconds"])
    require(end - start >= voice_end / 2 and ui_offset + end - start <= voice_end - 6,
            "Actual complete UI must dominate the film and leave a real closing; do not compress or truncate it")
    output.mkdir(parents=True)
    output = output.resolve()
    raw = output / "continuous-narration.wav"
    original = inputs / plan["narration"]["audio"]
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original), "-vn", "-ac", "1", "-ar", str(audio.RATE), "-c:a", "pcm_s16le", str(raw)], report=output / "audio-decode.log")
    seconds = float(media.probe(raw)["format"]["duration"])
    require(abs(seconds - voice_end) <= 1 / FPS and math.ceil(seconds * FPS) / FPS < 180, "Original continuous voice duration changed")
    normalized, normalization = audio.normalize_audio(raw, output, seconds)
    cues = audio.aligned_captions(plan["narration"]["text"], aligned, seconds)
    origins, decoders, samples = {}, {}, []
    try:
        for row in proof["page_videos"]:
            alias = row["alias"]
            require(re.fullmatch(r"[a-z]{1,12}", alias), "Unsafe native page alias")
            path = media.local_file(recordings, row["native_video"], {".webm"})
            require(media.digest(path) == row["sha256"] == review["native_video_sha256"][alias], "Reviewed native page bytes changed")
            metadata = media.probe(path)
            duration = float(metadata["format"]["duration"])
            stream = next(item for item in metadata["streams"] if item["codec_type"] == "video")
            rate = Fraction(stream["avg_frame_rate"])
            require(rate > 0, "Actual native frame rate is unavailable")
            origins[alias] = native_origin(row, duration, 1 / float(rate))
            decoders[alias] = NativeDecoder(path, output, alias, duration)
        require({row["page"] for row in windows} <= set(decoders), "An active page has no real source recording")
        canvas = RealtimeCanvas(inputs, plan, proof, start, end, windows, origins, decoders, ui_offset)
        frames = math.ceil(seconds * FPS)
        (output / "qa").mkdir()
        qa = {round((ui_offset + (end - start) * i / 11) * FPS) for i in range(12)}
        with (output / "visual-encode.log").open("xb") as log:
            process = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "1920x1080", "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "realtime-visuals.mp4"], cwd=output, stdin=subprocess.PIPE, stderr=log, stdout=subprocess.DEVNULL)
            try:
                for number in range(frames):
                    image = canvas.frame(number / FPS, cues)
                    pixels = image.tobytes()
                    process.stdin.write(pixels)
                    if number in qa:
                        image.resize((960, 540), canvas.Image.Resampling.LANCZOS).save(output / "qa" / f"{number:05}.png")
                        samples.append({"frame": number, "rgb_sha256": hashlib.sha256(pixels).hexdigest()})
                process.stdin.close()
                require(process.wait(timeout=180) == 0, "Continuous encoder failed; preserve partial media")
            except BaseException:
                if not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=20)
                raise
    finally:
        for decoder in decoders.values():
            decoder.close()
    video, captions = output / "atropos-medplain-realtime.mp4", output / "atropos-medplain-realtime.srt"
    audio.run(["ffmpeg", "-nostdin", "-v", "error", "-i", "realtime-visuals.mp4", "-i", normalized.name, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(audio.RATE), "-movflags", "+faststart", video.name], cwd=output, report=output / "final-mux.log")
    captions.write_text("\n\n".join(f"{index}\n{media.clock(a)} --> {media.clock(b)}\n{text}" for index, (a, b, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")
    render_proof = {"status": "rendered_continuous_ui_pending_quality_and_owner_review", "runtime_commit": proof["application_source"],
                    "context_inputs_runtime_commit": final.RUNTIME,
                    "recorded_application_source": proof["application_source"], "manifest_sha256": manifest_sha,
                    "requested_application_source": expected_source, "reviewed_recorder_sha256": expected_recorder_sha,
                    "recording_proof_sha256": proof_sha, "recording_review_sha256": review_sha, "native_origins": origins,
                    "native_final_frame_holds": {alias: decoder.tail_frame_holds for alias, decoder in decoders.items()},
                    "ui_clock_start": start, "ui_clock_end": end, "actual_page_windows": windows, "ui_film_offset": ui_offset,
                    "no_time_compression": True, "actual_complete_ui_seconds": end - start, "source_audio_sha256": media.digest(original),
                    "audio_policy": audio.POLICY, "source_pcm_signal": audio.pcm_signal_metrics(raw),
                    "normalized_pcm_signal": audio.pcm_signal_metrics(normalized), "normalization": normalization,
                    "continuous_audio_seconds": seconds, "expected_frames": frames, "visual_rounding_seconds": frames / FPS - seconds,
                    "video_sha256": media.digest(video), "captions_sha256": media.digest(captions), "qa_samples": samples,
                    "representation": "Actual chronological UI automation with genuine page switches and visible real waits, surrounded by labelled CGI/historical context. Native temporal resampling repeats frames without optical interpolation. No provider efficacy or clinical outcome claimed.",
                    "cursor_annotation": "Pulses use actual logged click-target coordinates, active page and monotonic time only; no invented or interpolated cursor movement.",
                    "publication": False}
    path = output / "render-proof.json"
    path.write_bytes(media.encoded(render_proof))
    verdict = check_media(video, captions, render_proof, output / "media-gate.json")
    require(verdict["quality_ready"], "Strict retained audio/AV/caption gate failed; preserve real-time MP4 and reports")
    print(json.dumps({"status": verdict["status"], "actual_ui_seconds": end - start, "video_sha256": media.digest(video)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--recording-proof-sha256", required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--review-sha256", required=True)
    parser.add_argument("--ui-start-seconds", type=float, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--expected-recorder-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.inputs.resolve(), args.manifest_sha256, args.recordings.resolve(), args.recording_proof_sha256,
           args.review.resolve(), args.review_sha256, args.ui_start_seconds, args.output.resolve(),
           args.expected_source, args.expected_recorder_sha256)
