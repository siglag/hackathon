import pytest

from app.evidence import phi, policy
from app.evidence.policy import EvidenceRequest, evaluate, request_hash


def ev(concepts, **kw):
    return evaluate(EvidenceRequest(concepts=concepts, **kw))


def test_vocabulary_resolution_and_synonyms():
    r = ev(["T2DM", "CKD", "SGLT2 inhibitors"], population="adults", study_types=["systematic review"])
    assert r.ok
    assert r.request["concepts"] == ["chronic kidney disease", "sglt2 inhibitors", "type 2 diabetes"]
    assert r.held_terms == []


def test_unrecognized_terms_stay_local():
    r = ev(["type 2 diabetes", "John Smith"])
    assert r.ok  # the recognized concept may be searched...
    assert "John Smith" in r.held_terms  # ...the unrecognized one is held, never sent
    assert "John Smith" not in str(policy.build_search(r.request))


def test_nothing_recognized_is_rejected():
    r = ev(["Mr Ahmed Al-Rashid", "MRN 00123456"])
    assert not r.ok and r.request is None


@pytest.mark.parametrize("text,kind", [
    ("john@example.com", "email"),
    ("+966 55 123 4567", "phone"),
    ("2026-10-01", "date"),
    ("12 March 1961", "date"),
    ("MRN: A12345", "record_number"),
    ("00123456", "number_sequence"),
    ("47-year-old", "exact_age"),
    ("12 King Fahd Road", "address"),
    ("lab_scan.pdf", "filename"),
    ("Mrs Fatima", "honorific_name"),
    ("diab​etes", "obfuscation"),
])
def test_identifier_detection(text, kind):
    assert kind in {f["type"] for f in phi.detect(text)}


def test_vocabulary_terms_are_not_flagged():
    index, _, _ = policy.vocabulary()
    flagged = [t for t in set(index.values()) if phi.detect(t)]
    assert flagged == []


def test_case_identifier_blocks_matching_vocab_word():
    # A patient whose surname equals a vocabulary word must still be blocked.
    ids = phi.identifier_tokens(["Gout Hassan"])
    r = evaluate(EvidenceRequest(concepts=["gout"]), ids)
    assert not r.ok and r.blocked and r.blocked[0]["type"] == "case_identifier"


def test_too_many_concepts_rejected():
    r = ev(["type 2 diabetes", "hypertension", "heart failure", "obesity", "gout"])
    assert not r.ok


def test_hash_binds_to_exact_request():
    a = ev(["type 2 diabetes", "metformin"]).request
    b = ev(["type 2 diabetes", "metformin"], population="adults").request
    assert request_hash(a) != request_hash(b)
    assert request_hash(a) == request_hash(ev(["metformin", "T2DM"]).request)


def test_gateway_generates_search_syntax():
    r = ev(["type 2 diabetes"], population="older adults", study_types=["meta-analysis"], year_from=2015, year_to=2026)
    args = policy.build_search(r.request)
    assert args["query"] == '("type 2 diabetes") AND "aged"[MeSH Terms] AND "humans"[MeSH Terms]'
    assert args["publicationTypes"] == ["Meta-Analysis"]
    assert args["dateRange"]["minDate"] == "2015"


def test_match_in_text_prefers_specific_terms():
    hits = policy.match_in_text("Patient Ali, 63, with type 2 diabetes and CKD stage 3 on metformin: dose?")
    assert "type 2 diabetes" in hits and "diabetes mellitus" not in hits
    assert "chronic kidney disease" in hits and "metformin" in hits
