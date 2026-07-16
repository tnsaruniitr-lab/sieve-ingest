"""
verify/writer.py — the HONEST transition decision.

Pure function: given a Tier-1 link state + a Tier-2 entailment verdict, decide
what (if anything) to write to a rule. Separated from any DB call so the honesty
invariant is unit-testable with zero infrastructure.

THE INVARIANT (enforced + tested): `set_last_verified` is True in EXACTLY ONE
case — a Tier-2 entailment verdict of 'supported'. No fetch, no 200, no header
date, no strike count can ever earn a verified date. This is what stops the
product fabricating freshness.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from . import classify as C
except ImportError:  # allow running as a standalone script
    import classify as C

# Tier-2 entailment verdicts.
SUPPORTED = "supported"      # page still supports the rule → earns last_verified
WEAKENED = "weakened"        # partial support → citeable, but NOT re-verified
CONTRADICTED = "contradicted"  # page now says otherwise → contest, never verify
ABSENT = "absent"            # rule's claim no longer on the page → citeable, stale
_VALID_VERDICTS = {SUPPORTED, WEAKENED, CONTRADICTED, ABSENT, None}

# Status a dead link is retired to (the auditor's trust filter excludes it).
_RETIRED = "retired"
_CONTESTED = "contested"

DEAD_STRIKE_THRESHOLD = 2   # transition only after N consecutive failures


def decide(link_status: str,
           entailment: Optional[str] = None,
           strikes: int = 0,
           exact_page: bool = False) -> Dict[str, Any]:
    """Return the honest write intent for one rule.

    Keys: set_last_verified(bool), status(Optional[str] — new status or None to
    leave), url_provenance(Optional[str] — 'extracted' only on an entailed exact
    page; None to leave), reason(str). NEVER raises.
    """
    if entailment not in _VALID_VERDICTS:
        entailment = None
    out: Dict[str, Any] = {"set_last_verified": False, "status": None,
                           "url_provenance": None, "reason": ""}

    # 1) Dead / transient link states are decided by Tier-1 alone; they never verify.
    if link_status == C.DEAD:
        if strikes + 1 >= DEAD_STRIKE_THRESHOLD:
            out["status"] = _RETIRED
            out["reason"] = f"dead link, {strikes + 1} consecutive strikes → retired"
        else:
            out["reason"] = f"dead link, strike {strikes + 1} (< {DEAD_STRIKE_THRESHOLD}) → hold"
        return out
    if link_status in (C.PAUSED, C.BLOCKED, C.ERROR, C.DNS_FAIL, C.REDIRECT):
        # Transient / unconfirmable: change nothing, and DO NOT refresh verified.
        out["reason"] = f"{link_status}: transient/unconfirmable → no change, not verified"
        return out

    # 2) Link is LIVE. Only Tier-2 entailment decides verification.
    if entailment == SUPPORTED:
        out["set_last_verified"] = True
        out["reason"] = "live + entailment supported → verified"
        if exact_page:
            out["url_provenance"] = "extracted"
            out["reason"] += "; exact source page → provenance extracted"
        return out
    if entailment == CONTRADICTED:
        out["status"] = _CONTESTED
        out["reason"] = "live but page contradicts the rule → contested, NOT verified"
        return out
    if entailment in (WEAKENED, ABSENT):
        out["reason"] = f"live but entailment {entailment} → citeable, honestly stale (not verified)"
        return out
    # LIVE but no entailment ran (Tier-1 only): explicitly NOT verified.
    out["reason"] = "live, no entailment run → link ok, NOT verified"
    return out


def _selftest() -> None:
    # THE INVARIANT: set_last_verified is True iff entailment == 'supported'.
    for ls in (C.LIVE, C.DEAD, C.PAUSED, C.BLOCKED, C.REDIRECT, C.ERROR, C.DNS_FAIL):
        for v in (SUPPORTED, WEAKENED, CONTRADICTED, ABSENT, None, "garbage"):
            d = decide(ls, v, strikes=0)
            expect = (ls == C.LIVE and v == SUPPORTED)
            assert d["set_last_verified"] is expect, (ls, v, d)

    # A mere live 200 with no entailment NEVER verifies.
    assert decide(C.LIVE, None)["set_last_verified"] is False

    # Dead link retires only at the strike threshold.
    assert decide(C.DEAD, None, strikes=0)["status"] is None            # strike 1 → hold
    assert decide(C.DEAD, None, strikes=1)["status"] == _RETIRED        # strike 2 → retired
    # ...and a dead link never earns a verified date even if a stale verdict is passed.
    assert decide(C.DEAD, SUPPORTED, strikes=1)["set_last_verified"] is False

    # Contradicted → contested, never verified.
    c = decide(C.LIVE, CONTRADICTED)
    assert c["status"] == _CONTESTED and c["set_last_verified"] is False

    # Weakened/absent stay citeable (no status change) but honestly stale.
    for v in (WEAKENED, ABSENT):
        d = decide(C.LIVE, v)
        assert d["status"] is None and d["set_last_verified"] is False

    # Exact-page provenance upgrade ONLY on a supported entailment.
    assert decide(C.LIVE, SUPPORTED, exact_page=True)["url_provenance"] == "extracted"
    assert decide(C.LIVE, SUPPORTED, exact_page=False)["url_provenance"] is None
    assert decide(C.LIVE, WEAKENED, exact_page=True)["url_provenance"] is None

    # Transient states change nothing.
    for ls in (C.PAUSED, C.BLOCKED, C.ERROR, C.DNS_FAIL, C.REDIRECT):
        d = decide(ls, SUPPORTED)
        assert d["status"] is None and d["set_last_verified"] is False and d["url_provenance"] is None

    print("VERIFY_WRITER_OK")


if __name__ == "__main__":
    _selftest()
