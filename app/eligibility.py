from __future__ import annotations
from typing import Dict, Any, Tuple, List
from app.models import Job

def apply_rules(j: Job, rules: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (is_eligible, reason_if_not). V0: title/location/keywords only."""
    # --- Visa (placeholder) ---
    visa_rules = rules.get("visa", {})
    # In V0 we don't know the user's visa; treat as pass (extend later)
    visa_ok = True

    # --- Location ---
    allowed_regions = set(x.lower() for x in rules.get("location", {}).get("allowed_regions", []))
    loc = (j.location or "").lower()
    remote_type = (j.remote_type or "").lower()
    location_ok = (not allowed_regions) or any(tok in loc for tok in allowed_regions) or any(tok.lower() in remote_type for tok in allowed_regions)
    if not location_ok:
        return False, "location"

    # --- Title target list ---
    targets = [t.lower() for t in rules.get("titles", {}).get("target_list", [])]
    title_ok = (not targets) or any(t in j.title.lower() for t in targets)
    if not title_ok:
        return False, "title"

    # --- Keywords threshold (on title + company; add description later) ---
    kw = [k.lower() for k in rules.get("keywords", {}).get("must_have_any", [])]
    threshold = int(rules.get("keywords", {}).get("threshold", 0))
    hay = f"{j.title} {j.company}".lower()
    hits = sum(1 for k in kw if k in hay)
    if threshold and hits < threshold:
        return False, f"keywords({hits}/{threshold})"

    # Visa gate last (placeholder)
    if not visa_ok:
        return False, "visa"

    return True, ""

def mark_eligibility(jobs: List[Job], rules: Dict[str, Any]) -> List[Job]:
    """Mutates status/eligible in-place based on apply_rules."""
    for j in jobs:
        ok, reason = apply_rules(j, rules)
        if ok:
            j.eligible = True
            j.status = "queued"
        else:
            j.eligible = False
            j.status = "parked"
            meta = j.meta or {}
            meta["parked_reason"] = reason
            j.meta = meta
    return jobs
