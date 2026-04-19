from __future__ import annotations
from typing import Dict, Any
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import hashlib

def standardize_fields(r: Dict[str, Any]) -> Dict[str, Any]:
    """Map raw record to standard keys with safe defaults."""
    return {
        "title": r.get("title", "").strip(),
        "company": r.get("company", "").strip(),
        "location": r.get("location", "").strip() or "Unknown",
        "remote_type": r.get("remote_type", "").strip() or infer_remote(r),
        "posted_date": parse_dt(r.get("posted_date")),
        "apply_url": r.get("apply_url", "").strip(),
        "ats_type": r.get("ats_type", "").strip(),
        "meta": r.get("meta", {}) or {},
    }

def infer_remote(r: Dict[str, Any]) -> str:
    txt = " ".join(str(v).lower() for v in r.values() if isinstance(v, str))
    if "remote" in txt or "distributed" in txt or "work from home" in txt:
        return "remote"
    if "hybrid" in txt:
        return "hybrid"
    return "onsite"

def parse_dt(value) -> datetime:
    """Parse various date/datetime inputs to UTC.

    - If a tz-aware datetime is given, convert to UTC.
    - If a naive datetime (including date-only parsed as midnight) is given,
      assume UTC and attach tzinfo.
    - If a string like "YYYY-MM-DD" is provided, treat as midnight UTC of that date.
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        # Try multiple formats; treat naive as UTC
        from dateutil.parser import isoparse
        dt = isoparse(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def canonicalize_url(url: str) -> str:
    """Drop tracking params; normalize scheme/host/path/query order."""
    try:
        u = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}]
        return urlunparse((u.scheme or "https", u.netloc.lower(), u.path.rstrip("/"), "", urlencode(q, doseq=True), ""))
    except Exception:
        return url

def stable_job_id(company: str, canonical_apply_url: str) -> str:
    raw = f"{company.strip().lower()}|{canonical_apply_url.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def is_valid_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        u = urlparse(url.strip())
        return bool(u.scheme in {"http", "https"} and u.netloc)
    except Exception:
        return False
