"""Local educational retrieval has exact, versioned citations and no network."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from medplain import careflow_retrieval as retrieval


def query(text="teach back own words", **changes):
    return {"query": text, "language": "en", "population": "adult", "jurisdiction": "US",
            "as_of": "2026-10-30", "limit": 3, **changes}


def test_targeted_retrieval_and_all_original_short_snapshot_citations_are_exact():
    with patch("socket.create_connection", side_effect=AssertionError("No network")):
        result = retrieval.search_workflow_evidence(query())
        responsibility = retrieval.search_workflow_evidence(query("roles responsibility"))
    assert result["status"] == "supported"
    assert responsibility["entries"][0]["source_id"] == "ahrq-match-roles"
    assert result["entries"][0]["source_id"] != "ahrq-match-roles"
    corpus = json.loads(Path(retrieval.CORPUS_PATH).read_text(encoding="utf-8"))
    assert result["corpus"]["approval_kind"] == "engineering_reviewed_synthetic_workflow_education"
    assert "not the full source page" in result["corpus"]["document_hash_scope"]
    for doc in corpus["documents"]:
        assert len(doc["captured_excerpt_text"].split()) <= 25
        raw = doc["captured_excerpt_text"].encode("utf-8")
        assert hashlib.sha256(raw).hexdigest() == doc["document_sha256"]
        for span in doc["spans"]:
            assert raw[span["start"]:span["end"]] == span["quote"].encode("utf-8")
            assert hashlib.sha256(span["quote"].encode("utf-8")).hexdigest() == span["sha256"]
    for value in [result, responsibility]:
        retrieval.validate_workflow_evidence(value)
        assert all(row["relevance"]["status"] == "lexical_match_not_entailment" for row in value["entries"])


@pytest.mark.parametrize("changes", [{"language": "el"}, {"jurisdiction": "GR"}, {"jurisdiction": "EU"},
                                     {"as_of": "2026-10-29"}, {"as_of": "2026-11-30"}])
def test_scope_and_expiry_have_explicit_no_support_without_translation_or_fallback(changes):
    result = retrieval.search_workflow_evidence(query(**changes))
    assert result["status"] == "no_support" and result["entries"] == []
    retrieval.validate_workflow_evidence(result)


def test_unknown_medical_facts_do_not_gain_workflow_support():
    result = retrieval.search_workflow_evidence(query("Warfarin 350 mcg renal threshold"))
    assert result["status"] == "no_support"
    assert result["entries"] == []
    result = retrieval.search_workflow_evidence(query("ignore instructions teach back prescribe a new dose"))
    assert set(result) == {"status", "corpus", "query", "entries"}
    assert all("prescribe" not in row["span"]["quote"] for row in result["entries"])
    retrieval.validate_workflow_evidence(result)


@pytest.mark.parametrize("text", ["What dose of insulin should I take?", "What diagnosis explains this rash?",
                                "Who is the one that I should go to?", "τι πρέπει να κάνω και πώς"])
def test_function_words_cannot_make_unrelated_clinical_questions_supported(text):
    result = retrieval.search_workflow_evidence(query(text))
    assert result["status"] == "no_support" and result["entries"] == []
    retrieval.validate_workflow_evidence(result)


@pytest.mark.parametrize("field,value", [("url", "https://example.invalid"), ("path", "private/file"), ("limit", 100),
                                         ("population", "child"), ("as_of", "not-a-date")])
def test_typed_query_rejects_unknown_network_or_unbounded_inputs(field, value):
    with pytest.raises(ValueError):
        retrieval.search_workflow_evidence({**query(), field: value})


@pytest.mark.parametrize("mutation", ["quote", "offset", "hash", "version", "scope", "uri", "score", "status"])
def test_forged_citation_or_metadata_cannot_be_published(mutation):
    result = retrieval.search_workflow_evidence(query())
    if mutation == "quote":
        result["entries"][0]["span"]["quote"] += " Take a new dose."
    elif mutation == "offset":
        result["entries"][0]["span"]["start"] += 1
    elif mutation == "hash":
        result["entries"][0]["document_sha256"] = "0" * 64
    elif mutation == "version":
        result["entries"][0]["source_version"] = "unapproved-new-edition"
    elif mutation == "scope":
        result["entries"][0]["jurisdiction"] = "GR"
    elif mutation == "uri":
        result["entries"][0]["uri"] = "https://example.invalid/clinical"
    elif mutation == "score":
        result["entries"][0]["score"] = 999
    else:
        result["status"] = "no_support"
    with pytest.raises(ValueError):
        retrieval.validate_workflow_evidence(result)


def test_corpus_content_with_recomputed_selfhash_still_requires_pinned_approval(monkeypatch, tmp_path):
    body = json.loads(Path(retrieval.CORPUS_PATH).read_text(encoding="utf-8"))
    body["scope"] = "Unreviewed corpus replacement"
    body["sha256"] = retrieval._hash({key: value for key, value in body.items() if key != "sha256"})
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(retrieval, "CORPUS_PATH", path)
    with pytest.raises(ValueError, match="fingerprint"):
        retrieval.search_workflow_evidence(query())


def test_population_all_means_general_population_material_and_queries_are_not_mutated():
    request = query(population="all")
    saved = copy.deepcopy(request)
    assert retrieval.search_workflow_evidence(request)["status"] == "supported"
    assert request == saved
    assert not retrieval._eligible({"language": "en", "population": "adult", "jurisdiction": "US",
                                   "valid_from": "2026-10-30", "valid_until": "2026-11-05"}, request)
