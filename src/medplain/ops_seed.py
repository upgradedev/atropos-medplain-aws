"""Packaged, explicitly offline examples. Runtime never invokes a model to seed them."""

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from medplain.clinical import check_interactions, parse_packet
from medplain.fixtures import get_packet
from medplain.gate import canonical_hash
from medplain.ops_store import StoreUnavailable
from medplain.ops_validation import validate_result

SEED_VERSION = "prepared-demo-v1"
PACKET_IDS = ("home-after-pneumonia", "heart-medicine-review", "dosage-mutation-stress")
EXEMPLARS = (*PACKET_IDS, "interrupted-fixture")
# These are labelled scenarios, not invented jobs or historical clinical decisions.
SCENARIOS = (
    ("demo-athens", "released", PACKET_IDS[0]),
    ("demo-athens", "released", PACKET_IDS[0]),
    ("demo-athens", "review_required", PACKET_IDS[2]),
    ("demo-athens", "rejected", PACKET_IDS[1]),
    ("demo-thessaloniki", "released", PACKET_IDS[0]),
    ("demo-thessaloniki", "review_required", PACKET_IDS[2]),
    ("demo-thessaloniki", "review_required", PACKET_IDS[1]),
    ("demo-thessaloniki", "failed", "interrupted-fixture"),
    ("demo-patras", "released", PACKET_IDS[0]),
    ("demo-patras", "released", PACKET_IDS[0]),
    ("demo-patras", "review_required", PACKET_IDS[2]),
    ("demo-patras", "rejected", PACKET_IDS[1]),
)


def validate_bundle(bundle: dict) -> dict:
    """Recheck source, typed artifact, mode, original binding and complete receipt chains."""
    if (not isinstance(bundle, dict)
            or set(bundle) != {"version", "prepared_at", "source_commit", "exemplars", "hash"}
            or bundle["version"] != SEED_VERSION
            or not isinstance(bundle["source_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", bundle["source_commit"])
            or canonical_hash({k: v for k, v in bundle.items() if k != "hash"}) != bundle["hash"]):
        raise ValueError("Invalid prepared collection manifest")
    if datetime.fromisoformat(bundle["prepared_at"]).tzinfo is None:
        raise ValueError("Preparation time must include its timezone")
    if set(bundle["exemplars"]) != set(EXEMPLARS):
        raise ValueError("Incomplete prepared collection")
    for name in EXEMPLARS:
        exemplar = bundle["exemplars"][name]
        if set(exemplar) != {"source", "result", "execution_id", "artifact_hash"}:
            raise ValueError("Invalid preparation evidence")
        packet_id = PACKET_IDS[0] if name == "interrupted-fixture" else name
        packet = parse_packet(get_packet(packet_id))
        if exemplar["source"] != packet.model_dump():
            raise ValueError("Prepared source differs from the included synthetic packet")
        result = exemplar["result"]
        if (result.get("model_mode") != "fixture" or result.get("commit") != bundle["source_commit"]
                or not isinstance(exemplar["execution_id"], str)
                or not re.fullmatch(r"prepared:[a-z-]+:[0-9a-f]{32}", exemplar["execution_id"])):
            raise ValueError("Preparation must retain its explicit offline provenance")
        artifact = validate_result(packet, result, exemplar["execution_id"])
        if artifact != exemplar["artifact_hash"]:
            raise ValueError("Prepared artifact hash changed")
        if name == "interrupted-fixture":
            if result["status"] != "refused" or artifact is not None:
                raise ValueError("Failure illustration cannot publish an artifact")
        elif (artifact is None or result.get("interactions") != check_interactions(packet)
              or result.get("correction_count") != (1 if name == PACKET_IDS[2] else 0)):
            raise ValueError("Preparation did not complete the required safety path")
    return bundle


def load_bundle(path: Path | None = None) -> dict:
    """Missing or altered build data fails visibly, without runtime fixture generation."""
    try:
        path = path or Path(__file__).parent / "data" / "prepared_demo.json"
        if path.stat().st_size > 512 * 1024:
            raise ValueError("Prepared collection exceeded its bounded size")
        return copy.deepcopy(validate_bundle(json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise StoreUnavailable() from None


def check_seed_size(cases: list[dict], shares: list[dict]):
    """Leave headroom beneath DynamoDB's 400 KiB item and 4 MiB transaction limits."""
    sizes = [len(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode())
             for row in [*cases, *shares]]
    # DynamoStore embeds JSON in an attribute string; include escaping overhead.
    wire_sizes = [len(json.dumps(json.dumps(row, separators=(",", ":"))).encode())
                  for row in [*cases, *shares]]
    if (len(cases) != 12 or len(shares) != 5 or max(sizes, default=0) > 300 * 1024
            or max(wire_sizes, default=0) > 350 * 1024 or sum(wire_sizes) > 3 * 1024 * 1024):
        raise StoreUnavailable()
