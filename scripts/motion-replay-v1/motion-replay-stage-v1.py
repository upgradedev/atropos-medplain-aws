"""Stage a temporary transport-disabled replay. This script does not build or use the network."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

PINNED_DISPLAY_FILES = {
    "src/ReconciliationView.tsx": "0c635fec9bf1d257be5f897fd3cdac6d4a87a68462c47ef81f03bb1e46d2b5ef",
    "src/ReadingControls.tsx": "22f0e58b6df5fc230d8fcae8adda82e53cdd17e68c7b30a61e0f26c8b5da5075",
    "src/ReferencePanel.tsx": "c2912e5435c51b1e174a712fe69e59a11282d3950b8bd431f8cc86936e793ae9",
    "src/ReconciliationForm.tsx": "c6d6d9f3ea4ce7b86df32dcd851d384ae1811937d68f41591fa18d9206b34f56",
    "src/ui.tsx": "7fb7cc3af7eacd9fd5c7c1822af8481281c77494970035b796dc9a78a959dbe0",
    "src/styles.css": "7d7dc2f4eb3ccb25c50bce765f422387206dda322330a29a0522aeeaf787c2e7",
    "src/types.ts": "b1195f6358c62761a74872ae2b768ff7424ada8cac7c6fb141f5cc706657f665",
    "package.json": "852e2237aa626b6122bb82b4ce13ffcf578910e77e335942cb3384547d9dd6fa",
    "package-lock.json": "4d9d7ce4f7251a3ebb08e244d9574fa867dfc0b61f7fe278a7be110abcd58f23",
}
HISTORICAL_COMMIT = "38313f8c804e2235568c65ee04f8efa4f6c301f9"
RUNTIME = "efe6c3e7c596697b308fffa758ae377b6422e03f"
RAW_SHA = "12c19717314d3bc75b3904c3e90571d73e3046c2f25fa29bfea7c615fa942fea"
SOURCE_SHA = "6f8fa08800d139c5a1f88e58dab810162a57de918c2e392974cfe89637537c68"
ARTIFACT_SHA = "e15bc414ed8ba0345762d6f5b92dba4e24c5ae5a08aa0cb48d6b9b55bf12f633"
VITE = '''import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({plugins:[react()],base:"/console/",resolve:{alias:[{find:/^\\.\\/api$/,replacement:new URL("./src/motion-replay-api.ts",import.meta.url).pathname}]},build:{outDir:"dist",emptyOutDir:true}});
'''


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def verify_projection(path, expected):
    assert re.fullmatch(r"[a-f0-9]{64}", expected) and digest(path) == expected, "Reviewed projection digest mismatch"
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    assert set(value) == {"schema", "provenance", "source", "artifact", "receipts"}
    assert value["schema"] == "medplain-historical-english-replay-v1"
    p, source, artifact, receipts = (value[k] for k in ("provenance", "source", "artifact", "receipts"))
    assert p["kind"] == "historical_actual_bedrock_read_only_replay"
    assert p["original_raw_sha256"] == RAW_SHA and p["recorded_source_commit"] == HISTORICAL_COMMIT
    assert p["recorded_run"] == "41029208154" and p["recorded_status"] == "review_required"
    assert p["current_component_runtime"] == RUNTIME and p["new_model_calls"] == p["new_release_events"] == 0
    assert p["recorded_source_hash"] == SOURCE_SHA and p["recorded_artifact_hash"] == ARTIFACT_SHA
    assert hashlib.sha256(canonical(source)).hexdigest() == SOURCE_SHA == artifact["source_hash"]
    assert source["language"] == artifact["language"] == "en"
    assert artifact["kind"] == "clarification_plan" and artifact["patient_regimen"] is None
    assert artifact["teachback"], "A real recorded English teach-back prompt is required"
    assert not re.search(r"[\u0370-\u03ff\u1f00-\u1fff]", json.dumps([source, artifact], ensure_ascii=False))
    documents = {d["document_id"]: d["text"] for d in source["documents"]}
    documents["caregiver-concerns"] = source["caregiver_concerns"]
    for statement in artifact["source_statements"]:
        c = statement["citation"]
        text = documents[c["document_id"]]
        encoded = text.encode()
        assert c["document_sha256"] == hashlib.sha256(encoded).hexdigest()
        assert encoded[c["byte_start"]:c["byte_end"]].decode() == c["quote"]
        assert text.splitlines()[c["line"] - 1] == c["quote"]
    previous = "0" * 64
    assert len(receipts) == 2 and [r["verdict"] for r in receipts] == ["refused", "verified"]
    for i, row in enumerate(receipts, 1):
        assert set(row) == {"sequence", "session_id", "source_hash", "artifact_hash", "previous_hash", "verdict", "discrepancies", "timestamp", "hash"}
        assert row["sequence"] == i and row["previous_hash"] == previous and row["source_hash"] == SOURCE_SHA
        assert row["hash"] == hashlib.sha256(canonical({k: v for k, v in row.items() if k != "hash"})).hexdigest()
        previous = row["hash"]
    assert receipts[-1]["artifact_hash"] == ARTIFACT_SHA
    return value


def stage(repository, projection, expected, output, templates):
    verify_projection(projection, expected)
    web = repository / "web"
    for rel, expected_hash in PINNED_DISPLAY_FILES.items():
        assert digest(web / rel) == expected_hash, "Frozen display source changed: " + rel
    assert not output.resolve().is_relative_to(repository.resolve()) and not output.exists(), "Output must be a new temporary directory outside the repository"
    output.mkdir(parents=True)
    shutil.copytree(web, output / "web", ignore=shutil.ignore_patterns("node_modules", "dist", ".vite"))
    main = templates / "motion-replay-harness-v1.tsx"
    shim = templates / "motion-replay-api-v1.ts"
    shutil.copyfile(main, output / "web/src/main.tsx")
    shutil.copyfile(shim, output / "web/src/motion-replay-api.ts")
    shutil.copyfile(projection, output / "web/src/motion-replay-data.json")
    (output / "web/vite.config.ts").write_text(VITE, encoding="utf-8")
    for rel, expected_hash in PINNED_DISPLAY_FILES.items():
        assert digest(output / "web" / rel) == expected_hash
    proof = {
        "schema": "medplain-historical-replay-stage-v1", "status": "staged_source_only_not_built",
        "projection_sha256": expected, "original_raw_sha256": RAW_SHA,
        "recorded_commit": HISTORICAL_COMMIT, "component_runtime": RUNTIME,
        "recorded_status": "review_required", "artifact_hash": ARTIFACT_SHA, "source_hash": SOURCE_SHA,
        "original_receipt_chain_validated": True, "source_citations_validated": True,
        "artifact_hash_scope": "Original stored artifact hash and exact reviewed projection are bound; explanation_text is absent so it is not recomputed from an invented rendering.",
        "unchanged_display_files": PINNED_DISPLAY_FILES,
        "stager_sha256": digest(__file__), "harness_sha256": digest(main), "transport_adapter_sha256": digest(shim),
        "staged_main_sha256": digest(output / "web/src/main.tsx"), "staged_vite_config_sha256": digest(output / "web/vite.config.ts"),
        "entrypoint_scope": "TEMP-only replay shell replaces entrypoint. Original ClarificationView/ReconciliationSources/ReadingPreferences/ReferencePanel and styles are unchanged.",
        "transport_scope": "TEMP-only Vite alias ./api rejects in memory. ReferencePanel can make a local denied metadata call; no metadata is substituted. Browser policy independently rejects API, remote and mutation requests.",
        "new_model_calls": 0, "new_review_or_release_events": 0,
    }
    (output / "stage-proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--projection-sha256", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--templates", type=Path, default=Path(__file__).parent)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        verify_projection(args.projection, args.projection_sha256)
        for rel, expected in PINNED_DISPLAY_FILES.items():
            assert digest(args.repository / "web" / rel) == expected
        print(json.dumps({"status": "passed_source_only", "projection_sha256": args.projection_sha256, "display_files": len(PINNED_DISPLAY_FILES)}))
        return
    assert args.output is not None
    print(json.dumps(stage(args.repository, args.projection, args.projection_sha256, args.output, args.templates)))


if __name__ == "__main__":
    main()
