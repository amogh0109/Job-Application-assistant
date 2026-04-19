# app/pipeline.py
from __future__ import annotations
from typing import Iterable, Dict, Any, List
from datetime import datetime
import pandas as pd  # only for robust datetime parsing
from app.models import Job
from app.normalize import standardize_fields, canonicalize_url, stable_job_id, is_valid_url


def _clean_str(x: Any) -> str:
    """Normalize common text fields to reduce accidental dupes."""
    if x is None:
        return ""
    s = str(x).strip()
    # collapse internal whitespace
    s = " ".join(s.split())
    return s


def _to_dt(x: Any) -> datetime | None:
    """Best-effort datetime coercion; returns tz-aware UTC when possible."""
    if isinstance(x, datetime):
        return x  # assume upstream already set tz
    if not x:
        return None
    try:
        # pandas handles many formats; utc=True gives tz-aware UTC
        return pd.to_datetime(x, errors="coerce", utc=True).to_pydatetime()
    except Exception:
        return None


def normalize_jobs(raw_jobs: Iterable[Dict[str, Any]]) -> List[Job]:
    # --- lightweight helpers for location/remote normalization ---
    US_ALIASES = {"us", "usa", "u.s.", "u.s.a", "united states", "united states of america"}
    UK_ALIASES = {"uk", "gbr", "great britain", "united kingdom"}
    STATE_NAME_TO_CODE = {
        "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO","connecticut":"CT","delaware":"DE","district of columbia":"DC","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY"
    }

    def _normalize_location_and_remote(loc: str) -> Dict[str, str]:
        s = (loc or "").strip()
        return {"location": s, "remote_type": "onsite", "city": "", "state": "", "country": ""}

        # Drop parentheses like "(Bill Leathem)"
        s = re.sub(r"\s*\([^)]*\)", "", s)

        # Strip/normalize explicit "Remote - Country" or "; Remote, US" forms
        if re.match(r"^\s*Remote\b", s, re.I):
            m = re.search(r"Remote\s*[-:]\s*([^,;]+)", s, re.I)
            if m:
                s = m.group(1).strip()
            else:
                s = s  # leave as-is; further cleanup below may trim duplicates
        s = re.sub(r"[;,]\s*Remote[^,;]*", "", s, flags=re.I).strip(" ,;")
        if (not s) and remote:
            s = "Remote"

        # Rewrite patterns like "US - CA - Santa Clara - ..." -> "Santa Clara, CA, United States"
        m = re.match(r"^(US|USA|U\.S\.?A?)[\s-]+([A-Z]{2})[\s-]+([^,\-]+)", s, re.I)
        if m:
            city, st = m.group(3).strip(), m.group(2).upper()
            s = f"{city}, {st}, United States"

        # Special: "Canada - Ottawa - ..." -> "Ottawa, Canada"
        m = re.match(r"^(Canada)[\s\-:]+([^,]+)", s, re.I)
        if m:
            s = f"{m.group(2).strip()}, Canada"

        # Region token chains like "APAC - Korea - Seoul - ASEM Tower, Korea, Republic of"
        # Keep the token before the country, dropping facility words
        if "," in s and " - " in s:
            left, right = s.rsplit(",", 1)
            tokens = [t.strip() for t in left.split(" - ") if t.strip()]
            if tokens:
                city_token = tokens[-1]
                if re.search(r"\\b(Building|Tower|Center|Centre|Headquarters|Campus)\\b", city_token, re.I) and len(tokens) >= 2:
                    city_token = tokens[-2]
                s = f"{city_token}, {right.strip()}"

        # Collapse duplicated site name: "X - X, Country" -> "X, Country"
        s = re.sub(r"^\s*([^,\-]+?)\s*-\s*\1\s*,\s*([^,]+)\s*$", r"\1, \2", s, flags=re.I)

        # Country/style corrections
        s = re.sub(r"(?i)korea,\s*republic\s*of\b", "South Korea", s)

        # Remove three-letter region codes like " - GBR" or middle token "City, SGP, Country"
        s = re.sub(r"\s*-\s*[A-Z]{3}(?=\s*,|$)", "", s)
        s = re.sub(r"^(.*?),\s*([A-Z]{3}),\s*(.*)$", r"\1, \3", s)

        # Normalize weird separators
        s = s.replace("  ", " - ")  # rare control sep
        s = s.replace("  ", " - ")
        s = s.replace("  ", " - ")

        # Remove campus tails right after a state code: ", ST - Something" -> ", ST" (use capture, no lookbehind)
        s = re.sub(r"(,\s*[A-Z]{2})\s*-\s*[^,]+", r"\1", s)

        # Collapse duplicate country tokens: "Argentina, Argentina"
        s = re.sub(r"(,\s*)([A-Za-z .]+)(,\s*\2)(\b)", r"\1\2", s)

        # Unify country names
        s = re.sub(r"(?i)united states of america\b", "United States", s).strip()
        tail = (s.rsplit(",", 1)[-1] if "," in s else s).strip().lower()
        if tail in US_ALIASES:
            s = re.sub(r"(?i)(united states of america|usa|u\.s\.?a?|us)$", "United States", s.strip())
        if tail in UK_ALIASES:
            s = re.sub(r"(?i)(united kingdom|uk|gbr|great britain)$", "United Kingdom", s.strip())

        # Strip obvious prefixes
        s = re.sub(r"^Default Location,\s*", "", s, flags=re.I)
        s = re.sub(r"^Field-([A-Z]{2}),\s*United States$", r"\1, United States", s)
        s = re.sub(r"^[A-Z]{2}-Headquarters,.*?,\s*United States$", lambda m: f"{m.group(0)[:2]}, United States", s)

        # USA:ST:City / ... -> City, ST, United States
        m = re.match(r"^(USA)[\s:]+([A-Z]{2})[\s:]+([^/]+)", s, re.I)
        if m:
            s = f"{m.group(3).strip()}, {m.group(2).upper()}, United States"
        # IND:STATE:City / ... -> City, India (keep state code if 2-3 letters)
        m = re.match(r"^(IND)[\s:]+([A-Z]{2,3})[\s:]+([^/]+)", s, re.I)
        if m:
            st = m.group(2).upper()
            s = f"{m.group(3).strip()}, {('' if len(st)>3 else st + ', ') }India".replace(" , ", ", ").strip(', ')

        # Parse city/state/country (best-effort)
        parts = [p.strip() for p in s.split(",") if p.strip()]
        city = parts[0] if parts else ""
        # Normalize US state names to codes when followed by United States
        if len(parts) >= 3 and parts[-1].lower() in {"united states", "usa", "us", "u.s.", "u.s.a"}:
            if len(parts) >= 2:
                mid = parts[1]
                code = STATE_NAME_TO_CODE.get(mid.lower())
                if code:
                    parts[1] = code
            # ensure canonical country spelling
            parts[-1] = "United States"
        state = parts[1] if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2,3}", parts[1]) else ""
        country = parts[-1] if parts else ""

        # If location starts with Remote and we extracted a country, keep only country in location
        if remote and (re.match(r"^\s*Remote\b", text_orig, re.I) or "Work From Home" in text_orig):
            if len(parts) >= 1:
                s = country or s

        remote_type = "remote" if remote else "onsite"
        # Recompute parts after changes
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if parts:
            city = parts[0]
            if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2,3}", parts[1]):
                state = parts[1]
            country = parts[-1]

        return {"location": s, "remote_type": remote_type, "city": city, "state": state, "country": country}

    out: List[Job] = []
    skipped = 0

    for r in raw_jobs:
        f = standardize_fields(r)

        # Clean core strings early (avoids different IDs for trivial whitespace/case)
        f["title"] = _clean_str(f.get("title"))
        f["company"] = _clean_str(f.get("company"))
        f["location"] = _clean_str(f.get("location"))
        f["remote_type"] = _clean_str(f.get("remote_type"))
        f["ats_type"] = _clean_str(f.get("ats_type"))

        # Hard-require a valid apply_url
        apply_url = f.get("apply_url")
        if not is_valid_url(apply_url):
            skipped += 1
            print(f"[normalize] skipped: invalid apply_url -> {apply_url!r} title={f.get('title')!r}")
            continue

        canon = canonicalize_url(apply_url)
        if not is_valid_url(canon):
            skipped += 1
            print(f"[normalize] skipped: invalid canonical_apply_url -> {canon!r} title={f.get('title')!r}")
            continue

        # Parse/normalize posted_date
        posted_dt = _to_dt(f.get("posted_date"))

        # Stable ID (company is cleaned, URL is canonicalized)
        jid = stable_job_id(f["company"], canon)

        meta = f.get("meta") or {}
        out.append(Job(
            job_id=jid,
            title=f["title"],
            company=f["company"],
            location=f["location"],
            remote_type=f["remote_type"],
            posted_date=posted_dt or datetime.utcnow(),  # fallback to "now" to keep sort stable
            apply_url=apply_url,
            ats_type=f["ats_type"],
            canonical_apply_url=canon,
            meta=meta,
        ))

    if skipped:
        print(f"[normalize] total skipped due to missing/invalid URLs: {skipped}")

    # ---- Exact de-dupe across sources (keep NEWEST by canonical URL) ----
    # This will NOT collapse different roles from the same company; it only
    # removes true duplicates pointing to the same canonical_apply_url.
    by_url: dict[str, Job] = {}
    for j in out:
        key = j.canonical_apply_url
        keep = j
        if key in by_url:
            existing = by_url[key]
            if (existing.posted_date or datetime.min) >= (j.posted_date or datetime.min):
                keep = existing
        by_url[key] = keep

    deduped = list(by_url.values())
    # Optional: sort newest-first for downstream convenience
    deduped.sort(key=lambda j: j.posted_date or datetime.min, reverse=True)
    return deduped
