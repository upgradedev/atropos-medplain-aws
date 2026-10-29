"""Build labelled examples through the actual offline Strands Graph, only in CI."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from medplain.clinical import parse_packet
from medplain.engine import run_packet
from medplain.fixture_model import FixtureModel
from medplain.fixtures import get_packet
from medplain.gate import canonical_hash
from medplain.ops_seed import EXEMPLARS, PACKET_IDS, SEED_VERSION, validate_bundle
from medplain.ops_validation import validate_result


class InterruptedFixtureModel(FixtureModel):
    async def stream(self, *args, **kwargs):
        raise RuntimeError("Deliberately interrupted offline preparation fixture")
        yield  # Make this an async generator with the same SDK contract.


def build(source_commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Supply the exact source commit SHA")
    exemplars = {}
    for name in EXEMPLARS:
        packet = parse_packet(get_packet(PACKET_IDS[0] if name == "interrupted-fixture" else name))
        execution_id = f"prepared:{name}:{uuid4().hex}"
        if name == "interrupted-fixture":
            with patch("medplain.engine.FixtureModel", InterruptedFixtureModel):
                result = run_packet(packet, execution_id, model_mode="fixture")
        else:
            result = run_packet(packet, execution_id, model_mode="fixture", stress=name == PACKET_IDS[2])
        result["commit"] = source_commit
        exemplars[name] = {"source": packet.model_dump(), "result": result,
                           "execution_id": execution_id,
                           "artifact_hash": validate_result(packet, result, execution_id)}
    bundle = {"version": SEED_VERSION, "prepared_at": datetime.now(timezone.utc).isoformat(),
              "source_commit": source_commit, "exemplars": exemplars}
    bundle["hash"] = canonical_hash(bundle)
    return validate_bundle(bundle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, default=Path("src/medplain/data/prepared_demo.json"))
    args = parser.parse_args()
    bundle = build(args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "version": SEED_VERSION, "exemplars": len(EXEMPLARS),
                      "source_commit": args.source_commit, "sha256": bundle["hash"]}))
