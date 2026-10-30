"""Versioned local retrieval of engineering-reviewed educational workflow excerpts.

No network, arbitrary URL, medical corpus, patient fact insertion or live search.
"""
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CORPUS_PATH = Path(__file__).parent / "data/careflow_evidence_v1.json"
CORPUS_VERSION = "careflow-workflow-evidence-v1"
CORPUS_SHA256 = "d52610536bd520e2a405b8af49a2cd261d60661ee8edb71069084f5333d550fe"
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "from", "with", "by", "as", "if",
    "is", "are", "was", "were", "be", "been", "it", "this", "that", "these", "those", "what", "which", "who", "when",
    "where", "why", "how", "can", "could", "should", "would", "will", "may", "must", "do", "does", "did", "i", "me",
    "my", "you", "your", "he", "she", "his", "her", "their", "they", "them", "we", "our", "please", "explain", "explains",
    "ο", "η", "το", "οι", "τα", "του", "της", "των", "και", "ή", "να", "σε", "με", "για", "από", "στο", "στη", "στην",
    "είναι", "τι", "ποιο", "ποιος", "πότε", "πού", "πώς", "εγώ", "μου", "εσείς", "σας", "θα", "μπορώ", "πρέπει",
})


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=240)
    language: Literal["en", "el"] = "en"
    population: Literal["adult", "all"] = "adult"
    jurisdiction: Literal["US", "EU", "GR", "any"] = "US"
    as_of: date | None = None
    limit: int = Field(default=3, ge=1, le=5, strict=True)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _load_corpus():
    body = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    if (body.get("version") != CORPUS_VERSION or body.get("sha256") != CORPUS_SHA256
            or body.get("sha256") != _hash({key: value for key, value in body.items() if key != "sha256"})):
        raise ValueError("Educational corpus fingerprint is invalid")
    seen = set()
    for doc in body["documents"]:
        text = doc["captured_excerpt_text"].encode("utf-8")
        if hashlib.sha256(text).hexdigest() != doc["document_sha256"] or len(doc["captured_excerpt_text"].split()) > 25:
            raise ValueError("Educational excerpt snapshot changed")
        if not doc["uri"].startswith("https://www.ahrq.gov/"):
            raise ValueError("Unapproved educational source")
        for span in doc["spans"]:
            if (span["evidence_id"] in seen or type(span["start"]) is not int or type(span["end"]) is not int
                    or not 0 <= span["start"] < span["end"] <= len(text)
                    or text[span["start"]:span["end"]] != span["quote"].encode("utf-8")
                    or hashlib.sha256(span["quote"].encode()).hexdigest() != span["sha256"]):
                raise ValueError("Educational source citation is invalid")
            seen.add(span["evidence_id"])
    return body


def _tokens(text):
    return [word for word in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE) if word not in STOPWORDS]


def _eligible(doc, query):
    return (doc["language"] == query["language"] and doc["population"] in {"all", query["population"]}
            and (query["jurisdiction"] == "any" or doc["jurisdiction"] == query["jurisdiction"])
            and doc["valid_from"] <= query["as_of"] < doc["valid_until"])


def search_workflow_evidence(query: dict) -> dict:
    request = EvidenceQuery.model_validate(query).model_dump(mode="json")
    request["as_of"] = request["as_of"] or datetime.now(timezone.utc).date().isoformat()
    corpus = _load_corpus()
    candidates = [(doc, span) for doc in corpus["documents"] if _eligible(doc, request) for span in doc["spans"]]
    texts = [_tokens(" ".join([doc["title"], span["section"], *span["topics"], span["quote"]])) for doc, span in candidates]
    frequencies = [Counter(text) for text in texts]
    average = sum(map(len, texts)) / len(texts) if texts else 1
    terms = set(_tokens(request["query"]))
    entries = []
    for index, (doc, span) in enumerate(candidates):
        score = 0.0
        for term in terms:
            count = frequencies[index][term]
            if count:
                present = sum(term in row for row in frequencies)
                inverse = math.log(1 + (len(texts) - present + 0.5) / (present + 0.5))
                score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * len(texts[index]) / average))
        if score <= 0:
            continue
        score = round(score, 8)
        entry = {key: doc[key] for key in ("source_id", "title", "uri", "publisher", "source_version", "document_sha256",
                                          "population", "jurisdiction", "language", "valid_from", "valid_until")}
        entry.update(evidence_id=span["evidence_id"], section=span["section"],
                     span={key: span[key] for key in ("start", "end", "quote", "sha256")}, score=score,
                     relevance={"method": "lexical_bm25", "status": "lexical_match_not_entailment", "score": score})
        entries.append(entry)
    entries.sort(key=lambda row: (-row["score"], row["evidence_id"]))
    entries = entries[:request["limit"]]
    return {"status": "supported" if entries else "no_support",
            "corpus": {key: corpus[key] for key in ("version", "sha256", "snapshot_date", "scope", "approval_kind",
                                                    "document_hash_scope", "offset_scope", "validity_scope")},
            "query": request, "entries": entries}


def validate_workflow_evidence(result: dict) -> None:
    """Recompute deterministic local retrieval, never trust supplied citation metadata."""
    if not isinstance(result, dict) or set(result) != {"status", "corpus", "query", "entries"}:
        raise ValueError("Educational evidence result shape is invalid")
    if result != search_workflow_evidence(result["query"]):
        raise ValueError("Educational evidence is not the exact approved snapshot selection")
