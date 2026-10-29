"""Pinned references expose coverage and availability without clinical clearance."""
from medplain.clinical_references import reference_material
from medplain.gate import canonical_hash


def test_reference_snapshot_binds_exact_shared_material_and_explicit_limits():
    material = reference_material()
    assert material["snapshot_sha256"] == canonical_hash({k: v for k, v in material.items()
                                                         if k != "snapshot_sha256"})
    assert material["references"]["status"] == "pinned_reference_snapshot"
    assert "unavailable" in material["interaction_service"] and "2024-01-02" in material["interaction_service"]
    assert "No medication substitution" in material["scope"]
    cards = material["references"]["cards"]
    assert len({c["id"] for c in cards}) == len(cards)
    assert all(c["url"].startswith("https://") and c["use"] and c["section"] for c in cards)


def test_identity_snapshot_retains_actual_lookup_candidates_and_no_equivalence_claim():
    snapshot = reference_material()["identity_snapshot"]
    assert snapshot["status"] == "pinned_reference_snapshot"
    assert "not branded-product equivalence" in snapshot["scope"]
    records = snapshot["records"]
    assert records and len({row["lookup_name"] for row in records}) == len(records)
    for row in records:
        assert row["status"] in {"available", "unavailable", "no_result"}
        assert row["url"].startswith("https://rxnav.nlm.nih.gov/REST/rxcui.json?")
        assert row["retrieved_at"]
        if row["status"] == "available":
            assert row["rxnorm_ids"] == row["raw_response"]["idGroup"]["rxnormId"]
            assert all(identifier.isdecimal() for identifier in row["rxnorm_ids"])
            assert len(row["response_sha256"]) == 64
        else:
            assert row["rxnorm_ids"] == []


def test_selected_label_pairs_have_their_own_citations_and_bounded_scope():
    cards = {card["id"]: card for card in reference_material()["references"]["cards"]}
    assert ["warfarin", "ibuprofen"] in cards["dailymed-warfarin"]["selected_pairs"]
    assert ["apixaban", "naproxen"] in cards["dailymed-apixaban"]["selected_pairs"]
    assert "do not independently stop" in cards["dailymed-warfarin"]["use"].lower()
    assert "does not establish a contraindication" in cards["dailymed-apixaban"]["use"].lower()
