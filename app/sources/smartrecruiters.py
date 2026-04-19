# app/sources/smartrecruiters.py
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import asyncio
import httpx

UA = {"User-Agent": "AutoApply/1.0 (+smartrecruiters-fast)"}
API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"


# -------------------------
# Public entrypoint (name unchanged)
# -------------------------
def collect_smartrecruiters(src: Dict[str, Any], concurrency: int = 5) -> List[Dict[str, Any]]:
    """
    Concurrent SmartRecruiters collector.
    Usage: raw_jobs = collect_smartrecruiters(src, concurrency=5)
    """
    orgs: List[str] = (src.get("orgs") or [])
    if not orgs or (len(orgs) == 1 and orgs[0] == "*"):
        print("[smartrecruiters] orgs=['*'] or empty — provide explicit company slugs in sources.yaml.")
        return []

    filters: Dict[str, Any] = (src.get("filters") or {})
    filt = _prepare_filters(filters)

    pagination: Dict[str, Any] = (src.get("pagination") or {})
    limit = int(pagination.get("page_size", 100))
    max_pages = int(pagination.get("max_pages", 5))

    return asyncio.run(_collect_concurrent(orgs, filt, limit, max_pages, concurrency))


# -------------------------
# Async core
# -------------------------
async def _collect_concurrent(
    orgs: List[str],
    filt: Dict[str, Any],
    limit: int,
    max_pages: int,
    concurrency: int,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    out: List[Dict[str, Any]] = []

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=concurrency)
    timeout = httpx.Timeout(30.0, connect=10.0, read=30.0)
    async with httpx.AsyncClient(
        headers=UA, http2=True, timeout=timeout, limits=limits, follow_redirects=True
    ) as client:

        async def one(company: str) -> Tuple[str, List[Dict[str, Any]]]:
            if not company:
                return company, []
            try:
                postings = await _fetch_company_postings(client, company, limit, max_pages, sem)
                postings = _apply_filters_fast(postings, filt)
                mapped = [_map_posting(p, company) for p in postings]
                return company, mapped
            except Exception as e:
                print(f"[smartrecruiters] error for '{company}': {e}")
                return company, []

        tasks = [one((c or "").strip()) for c in orgs if (c or "").strip()]
        results = await asyncio.gather(*tasks)

    for _, mapped in results:
        out.extend(mapped)
    return out


# -------------------------
# Fetch & pagination (async)
# -------------------------
async def _fetch_company_postings(
    client: httpx.AsyncClient, company: str, limit: int, max_pages: int, sem: asyncio.Semaphore
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    offset = 0
    pages = 0

    while True:
        url = API.format(company=company)
        params = {"limit": limit, "offset": offset}
        try:
            async with sem:
                r = await client.get(url, params=params)
            if r.status_code == 404:
                print(f"[smartrecruiters] 404 for company '{company}'. Skipping.")
                break
            r.raise_for_status()
            data = r.json() or {}
        except Exception as e:
            print(f"[smartrecruiters] fetch error for '{company}': {e}")
            break

        content = data.get("content") or []
        if not isinstance(content, list):
            break

        results.extend(content)
        pages += 1

        total = data.get("totalFound")
        if not content:
            break
        offset += limit
        if pages >= max_pages:
            break
        if isinstance(total, int) and offset >= total:
            break

    return results


# -------------------------
# Mapping
# -------------------------
def _map_posting(p: Dict[str, Any], company: str) -> Dict[str, Any]:
    pid = p.get("id")
    name = p.get("name") or p.get("title")
    released = p.get("releasedDate") or p.get("createdOn") or p.get("updatedOn")

    loc_obj = p.get("location") or {}
    location = (
        loc_obj.get("city")
        or loc_obj.get("region")
        or loc_obj.get("country")
        or loc_obj.get("address")
        or _join([loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country")])
    )

    apply_url = p.get("applyUrl") or p.get("ref") or p.get("jobAdUrl") or ""
    absolute_url = apply_url

    return {
        "source": "smartrecruiters",
        "company": company,
        "title": name,
        "location": location,
        "posted_date": released,
        "apply_url": absolute_url,
        "absolute_url": absolute_url,
        "ats_type": "smartrecruiters",
        "meta": {"raw": p, "id": pid},
    }


# -------------------------
# Filters (optimized)
# -------------------------
def _prepare_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    q_terms = [t.strip().lower() for t in (filters.get("query") or []) if t]
    loc_includes = [t.strip().lower() for t in (filters.get("locations_include") or []) if t]
    return {"q_terms": q_terms, "loc_includes": loc_includes}

def _apply_filters_fast(posts: List[Dict[str, Any]], filt: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not posts:
        return posts
    q_terms = filt.get("q_terms") or []
    loc_includes = filt.get("loc_includes") or []
    if not q_terms and not loc_includes:
        return posts

    out: List[Dict[str, Any]] = []
    for p in posts:
        title = (p.get("name") or p.get("title") or "").lower()
        loc = _extract_location_str(p).lower()

        if q_terms and not any(term in title for term in q_terms):
            continue
        if loc_includes and not any(inc in loc for inc in loc_includes):
            continue

        out.append(p)
    return out


def _extract_location_str(p: Dict[str, Any]) -> str:
    loc = p.get("location") or {}
    if isinstance(loc, dict):
        return " ".join([
            str(loc.get("address") or ""),
            str(loc.get("city") or ""),
            str(loc.get("region") or ""),
            str(loc.get("country") or ""),
        ]).strip()
    return str(loc or "")


# -------------------------
# Small utils
# -------------------------
def _join(parts: List[Optional[str]], sep: str = ", ") -> str:
    return sep.join([p for p in parts if p])
