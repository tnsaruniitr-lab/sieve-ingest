"""Tests for the verification harness core (Tier-1 classify + honest transition).
Pure logic — no DB, network, or LLM. Guards the freshness-honesty invariant."""

from sieve_ingest.verify import classify as C
from sieve_ingest.verify import writer as W


def test_classify_link_states():
    assert C.classify(200, "u", "u")["link_status"] == C.LIVE
    assert C.classify(402, "u", "u")["link_status"] == C.PAUSED
    assert C.classify(403, "u", "u")["link_status"] == C.BLOCKED
    assert C.classify(404, "u", "u")["link_status"] == C.DEAD
    assert C.classify(500, "u", "u")["link_status"] == C.ERROR
    r = C.classify(301, "http://a", "https://b")
    assert r["link_status"] == C.REDIRECT and r["redirected"] and r["final_url"] == "https://b"
    assert C.classify(None, "u", None, "Name or service not known")["link_status"] == C.DNS_FAIL


def test_content_hash_and_dates():
    assert C.content_hash("Hello   World") == C.content_hash("hello world")
    assert C.content_hash(None) is None
    assert C.last_modified_seen({"last-modified": "x"}, None) == "x"
    assert C.last_modified_seen(None, 'article:modified_time":"2026-05-03"') == "2026-05-03"


def test_only_supported_entailment_verifies():
    # THE honesty invariant, enumerated.
    for ls in (C.LIVE, C.DEAD, C.PAUSED, C.BLOCKED, C.REDIRECT, C.ERROR, C.DNS_FAIL):
        for v in (W.SUPPORTED, W.WEAKENED, W.CONTRADICTED, W.ABSENT, None):
            got = W.decide(ls, v)["set_last_verified"]
            assert got is (ls == C.LIVE and v == W.SUPPORTED), (ls, v)


def test_transition_rules():
    assert W.decide(C.DEAD, None, strikes=0)["status"] is None      # strike 1 holds
    assert W.decide(C.DEAD, None, strikes=1)["status"] == "retired"  # strike 2 retires
    assert W.decide(C.LIVE, W.CONTRADICTED)["status"] == "contested"
    assert W.decide(C.LIVE, W.SUPPORTED, exact_page=True)["url_provenance"] == "extracted"
    assert W.decide(C.LIVE, W.SUPPORTED, exact_page=False)["url_provenance"] is None
    # a live 200 alone never verifies
    assert W.decide(C.LIVE, None)["set_last_verified"] is False
