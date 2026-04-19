# app/sources/greenhouse.py
from __future__ import annotations
from typing import Dict, Any, List, Tuple
import asyncio
import httpx
from datetime import datetime, timezone

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
UA = {"User-Agent": "AutoApply/1.0 (+greenhouse-fast)"}

# -------------------------
# Public entrypoint (unchanged signature)
# -------------------------
def collect_greenhouse(src: Dict[str, Any], concurrency: int = 10) -> List[Dict[str, Any]]:
    """
    Collect jobs from Greenhouse boards defined in sources.yaml, concurrently.
    Returns list[dict] with posted_date populated (YYYY-MM-DD).
    """
    orgs: List[str] = (src.get("orgs") or [])
    if not orgs or (len(orgs) == 1 and orgs[0] == "*"):
        print("[greenhouse] orgs=['*'] or empty — provide explicit board tokens.")
        return []

    filt = _prepare_filters(src.get("filters") or {})
    jobs = asyncio.run(_collect_concurrent(orgs, filt, concurrency))

    # Dedupe by job_id across tokens
    seen, out = set(), []
    for j in jobs:
        jid = j.get("job_id")
        if jid and jid not in seen:
            seen.add(jid)
            out.append(j)
    return out

# -------------------------
# Async core
# -------------------------
async def _collect_concurrent(orgs: List[str], filt: Dict[str, Any], concurrency: int) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    out: List[Dict[str, Any]] = []

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=concurrency)
    timeout = httpx.Timeout(20.0, connect=10.0, read=20.0)
    async with httpx.AsyncClient(
        headers=UA, http2=True, timeout=timeout, limits=limits, follow_redirects=True
    ) as client:

        async def one(token: str) -> Tuple[str, List[Dict[str, Any]]]:
            token = (token or "").strip().lower()
            if not token:
                return token, []
            url = API.format(token=token)
            try:
                async with sem:
                    r = await client.get(url)
                if r.status_code == 404:
                    print(f"[greenhouse] 404 for token '{token}'. Skipping.")
                    return token, []
                r.raise_for_status()
                jobs = (r.json() or {}).get("jobs") or []
                if not isinstance(jobs, list):
                    return token, []
                mapped = [_map_job(j, token) for j in jobs]
                mapped = _apply_filters_fast(mapped, filt)
                return token, mapped
            except httpx.RequestError as e:
                print(f"[greenhouse] error for '{token}': {e}")
                return token, []
            except ValueError:
                return token, []

        tasks = [one(s) for s in orgs if (s or "").strip()]
        results = await asyncio.gather(*tasks)

    for _, mapped in results:
        out.extend(mapped)
    return out

# -------------------------
# Mapping (now fills posted_iso & posted_date)
# -------------------------
def _map_job(j: Dict[str, Any], token: str) -> Dict[str, Any]:
    iso_src = j.get("created_at") or j.get("updated_at")  # prefer created_at as "posted"
    posted_dt = _safe_iso_to_utc(iso_src)
    posted_date = posted_dt.strftime("%Y-%m-%d") if posted_dt else None

    return {
        "source": "greenhouse",
        "board_token": token,
        "company": token.title() or "Unknown",
        "job_id": j.get("id"),
        "title": j.get("title"),
        "location": (j.get("location") or {}).get("name"),
        "absolute_url": j.get("absolute_url"),
        "apply_url": j.get("absolute_url"),
        "posted_iso": posted_dt.isoformat() if posted_dt else None,
        "posted_date": posted_date,
        "updated_at": j.get("updated_at"),
        "created_at": j.get("created_at"),
        "internal_job_id": j.get("internal_job_id"),
        "raw": j,
    }

def _safe_iso_to_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        from dateutil.parser import isoparse
        return isoparse(iso).astimezone(timezone.utc)
    except Exception:
        return None

# -------------------------
# Filters (optimized)
# -------------------------
def _prepare_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    norm = lambda it: [t.strip().lower() for t in (it or []) if t]
    return {
        "query_terms": norm(filters.get("query")),
        "loc_includes": norm(filters.get("locations_include")),
    }

def _apply_filters_fast(jobs: List[Dict[str, Any]], f: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not jobs:
        return jobs
    q_terms = f.get("query_terms") or []
    loc_includes = f.get("loc_includes") or []
    if not q_terms and not loc_includes:
        return jobs

    out: List[Dict[str, Any]] = []
    for j in jobs:
        title_l = (j.get("title") or "").lower()
        loc_l = (j.get("location") or "").lower()
        if q_terms and not any(term in title_l for term in q_terms):
            continue
        if loc_includes and not any(inc in loc_l for inc in loc_includes):
            continue
        out.append(j)
    return out

# -------------------------
# Optional: legacy util
# -------------------------
# -------------------------
# Optional: legacy util
# -------------------------
def gh_posted(j: Dict[str, Any]) -> datetime:
    iso = j.get("created_at") or j.get("updated_at")
    return _safe_iso_to_utc(iso) or datetime.now(timezone.utc)

# -------------------------
# Date cleaning util
# -------------------------
def clean_posted_date(j: Dict[str, Any]) -> str | None:
    """
    Returns a clean YYYY-MM-DD posted date string for a job dict.
    Falls back gracefully if missing.
    """
    if j.get("posted_date"):
        return j["posted_date"]
    iso = j.get("created_at") or j.get("updated_at")
    dt = _safe_iso_to_utc(iso)
    return dt.strftime("%Y-%m-%d") if dt else None

# -------------------------
# Standalone runner (no argparse)
# -------------------------
def main():
    src = {
        "orgs": ["databricks", "scaleai", "roblox"],  # board tokens
        "filters": {
            "query": ["ai", "machine learning"],
            "locations_include": ["remote", "united states", "new york"],
        },
    }
    print(f"[greenhouse] collecting {len(src['orgs'])} org(s)...")
    jobs = collect_greenhouse(src, concurrency=10)
    print(f"[greenhouse] collected {len(jobs)} jobs")
    for j in jobs[:5]:
        print("-" * 80)
        print(f"{j.get('company')}: {j.get('title')} @ {j.get('location')}")
        print(f"URL: {j.get('apply_url')}")
        print(f"Posted: {clean_posted_date(j)}")

if __name__ == "__main__":
    main()

