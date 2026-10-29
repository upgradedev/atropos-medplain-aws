"""Pinned primary references, not a live or comprehensive clinical decision API."""
import json
from pathlib import Path

from medplain.gate import canonical_hash


def reference_material() -> dict:
    """Return explicit snapshot provenance shared by agents and the full baseline."""
    root = Path(__file__).parent / "data"
    references = json.loads((root / "reconciliation_references.json").read_text(encoding="utf-8"))
    identities = json.loads((root / "rxnorm_identity_snapshot.json").read_text(encoding="utf-8"))
    body = {"references": references, "identity_snapshot": identities,
            "interaction_service": "unavailable: RxNav drug-drug interaction service discontinued 2024-01-02",
            "scope": "Pinned terminology and selected label references only. No medication substitution or clinical clearance."}
    return {**body, "snapshot_sha256": canonical_hash(body)}
