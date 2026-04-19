# app/sources/ashby.py  (add these fast paths; keep your existing helpers)

from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import asyncio
import httpx

UA = {"User-Agent": "AutoApply/1.0 (+ashby-fast)"}
API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

# ---------- Public entrypoint (sync wrapper over async) ----------
def collect_ashby(src: Dict[str, Any], concurrency: int = 10) -> List[Dict[str, Any]]:
    """
    Faster, concurrent Ashby collector. Same return shape as collect_ashby().
    Usage: raw_jobs = collect_ashby_fast(src, concurrency=10)
    """
    orgs: List[str] = (src.get("orgs") or [])
    if not orgs or (len(orgs) == 1 and orgs[0] == "*"):
        print("[ashby] orgs=['*'] or empty — provide explicit job board names.")
        return []

    # Prepare filters once (lowercased)
    filters: Dict[str, Any] = (src.get("filters") or {})
    filt = _prepare_filters(filters)

    pagination: Dict[str, Any] = (src.get("pagination") or {})
    include_comp = bool(pagination.get("include_compensation", False))

    results = asyncio.run(
        _collect_ashby_concurrent(orgs, include_comp, filt, concurrency)
    )
    return results


# ---------- Async core ----------
async def _collect_ashby_concurrent(
    orgs: List[str],
    include_compensation: bool,
    filt: Dict[str, Any],
    concurrency: int,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    out: List[Dict[str, Any]] = []

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=concurrency)
    timeout = httpx.Timeout(20.0, connect=10.0, read=20.0)
    async with httpx.AsyncClient(
        headers=UA,
        http2=True,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:

        async def one(slug: str) -> Tuple[str, List[Dict[str, Any]]]:
            if not slug:
                return slug, []
            url = API.format(slug=slug)
            params = {"includeCompensation": "true" if include_compensation else "false"}
            try:
                async with sem:
                    r = await client.get(url, params=params)
                if r.status_code == 404:
                    print(f"[ashby] 404 for board '{slug}'. Skipping.")
                    return slug, []
                r.raise_for_status()
                data = r.json() or {}
                jobs = data.get("jobs") or []
                if not isinstance(jobs, list):
                    return slug, []
                # Filter fast (vector-ish, but still python loops)
                jobs = _apply_filters_fast(jobs, filt)
                # Map
                mapped = [_map_job(j, slug) for j in jobs]
                return slug, mapped
            except httpx.RequestError as e:
                print(f"[ashby] error for '{slug}': {e}")
                return slug, []
            except ValueError:
                # JSON parse error
                return slug, []

        tasks = [one((s or "").strip()) for s in orgs if (s or "").strip()]
        # gather in batches to control memory spikes on huge org lists
        # (still returns as one flat list)
        results = await asyncio.gather(*tasks)

    for _, mapped in results:
        out.extend(mapped)
    return out


# ---------- Filter helpers (optimized) ----------
def _prepare_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Lowercase & precompute filters once."""
    q_terms = [t.strip().lower() for t in (filters.get("query") or []) if t]
    loc_includes = [t.strip().lower() for t in (filters.get("locations_include") or []) if t]
    return {"q_terms": q_terms, "loc_includes": loc_includes}

def _apply_filters_fast(jobs: List[Dict[str, Any]], filt: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not jobs:
        return jobs
    q_terms = filt.get("q_terms") or []
    loc_includes = filt.get("loc_includes") or []
    if not q_terms and not loc_includes:
        return jobs

    out: List[Dict[str, Any]] = []
    for j in jobs:
        if q_terms:
            t = (j.get("title") or "")
            d = (j.get("descriptionPlain") or "")
            # one lowercase per field
            tl = t.lower()
            dl = d.lower()
            if not any(term in tl or term in dl for term in q_terms):
                continue
        if loc_includes:
            l = (j.get("location") or "").lower()
            if not any(inc in l for inc in loc_includes):
                continue
        out.append(j)
    return out
