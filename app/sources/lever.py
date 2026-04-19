# app/sources/lever.py
from __future__ import annotations
from typing import Dict, Any, List, Tuple
import asyncio
import httpx

API = "https://api.lever.co/v0/postings/{account}?mode=json"
UA = {"User-Agent": "AutoApply/1.0 (+lever-fast)"}


# -------------------------
# Public entrypoint (name unchanged)
# -------------------------
def collect_lever(src: Dict[str, Any], concurrency: int = 10) -> List[Dict[str, Any]]:
    """
    Concurrent Lever collector.
    Usage: raw_jobs = collect_lever(src, concurrency=10)
    """
    orgs: List[str] = (src.get("orgs") or [])
    if not orgs or (len(orgs) == 1 and orgs[0] == "*"):
        print("[lever] orgs=['*'] or empty — provide explicit Lever account slugs.")
        return []

    filters: Dict[str, Any] = (src.get("filters") or {})
    filt = _prepare_filters(filters)

    return asyncio.run(_collect_concurrent(orgs, filt, concurrency))


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

        async def one(account: str) -> Tuple[str, List[Dict[str, Any]]]:
            if not account:
                return account, []
            url = API.format(account=account)
            try:
                async with sem:
                    r = await client.get(url)
                if r.status_code == 404:
                    print(f"[lever] 404 for account '{account}'. Skipping.")
                    return account, []
                r.raise_for_status()
                postings = r.json() or []
                if not isinstance(postings, list):
                    return account, []

                postings = _apply_filters_fast(postings, filt)
                mapped: List[Dict[str, Any]] = []
                for p in postings:
                    cats = p.get("categories") or {}
                    location = cats.get("location") if isinstance(cats, dict) else None
                    apply = p.get("applyUrl") or p.get("hostedUrl") or p.get("url")
                    mapped.append({
                        "source": "lever",
                        "account": account,
                        "job_id": p.get("id"),
                        "title": p.get("text"),
                        "company": account,
                        "location": location,
                        "apply_url": apply,
                        "absolute_url": apply,
                        "created_at": p.get("createdAt"),
                        "updated_at": p.get("updatedAt") or p.get("createdAt"),
                        "raw": p,
                    })
                return account, mapped
            except Exception as e:
                print(f"[lever] error for '{account}': {e}")
                return account, []

        tasks = [one((a or "").strip()) for a in orgs if (a or "").strip()]
        results = await asyncio.gather(*tasks)

    for _, mapped in results:
        out.extend(mapped)
    return out


# -------------------------
# Filters (optimized)
# -------------------------
def _prepare_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    query_terms = [t.strip().lower() for t in (filters.get("query") or []) if t]
    loc_includes = [t.strip().lower() for t in (filters.get("locations_include") or []) if t]
    return {"query_terms": query_terms, "loc_includes": loc_includes}

def _apply_filters_fast(posts: List[Dict[str, Any]], filt: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not posts:
        return posts
    q_terms = filt.get("query_terms") or []
    loc_includes = filt.get("loc_includes") or []
    if not q_terms and not loc_includes:
        return posts

    out: List[Dict[str, Any]] = []
    for p in posts:
        title = (p.get("text") or "").lower()
        cats = p.get("categories") or {}
        loc = (cats.get("location") if isinstance(cats, dict) else "") or ""
        loc_low = loc.lower()

        if q_terms and not any(term in title for term in q_terms):
            continue
        if loc_includes and not any(inc in loc_low for inc in loc_includes):
            continue

        out.append(p)
    return out
