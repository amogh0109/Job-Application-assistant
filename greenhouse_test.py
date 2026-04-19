# greenhouse_dateposted_robust.py
import re, json
import httpx
from datetime import timezone
from dateutil.parser import isoparse

# ---- JSON-LD finder that handles single object, list, and @graph ----
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

def _iter_jsonld_objects(html: str):
    for m in _JSONLD_RE.finditer(html or ""):
        block = m.group(1).strip()
        try:
            data = json.loads(block)
        except Exception:
            continue
        # Normalize to iterable of dicts
        if isinstance(data, dict):
            yield data
            # If it's a @graph, yield entries
            graph = data.get("@graph")
            if isinstance(graph, list):
                for x in graph:
                    if isinstance(x, dict):
                        yield x
        elif isinstance(data, list):
            for x in data:
                if isinstance(x, dict):
                    yield x

def _iso_or_none(s: str | None):
    if not s:
        return None
    try:
        return isoparse(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def extract_dateposted_from_html(html: str) -> str | None:
    for obj in _iter_jsonld_objects(html):
        t = obj.get("@type")
        # Normalize @type which can be str or list
        types = [t] if isinstance(t, str) else (t or [])
        if "JobPosting" in types:
            # Try common keys
            for key in ("datePosted", "datePublished", "dateCreated"):
                iso = _iso_or_none(obj.get(key))
                if iso:
                    return iso
    return None

def gh_detail_api_date(token: str, job_id: str) -> dict:
    # Returns {"created_at": ..., "updated_at": ..., "iso": ..., "date": ...} best-effort
    api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = client.get(api, timeout=20)
        if r.status_code >= 400:
            return {}
        j = r.json()
    created = _iso_or_none(j.get("created_at"))
    updated = _iso_or_none(j.get("updated_at"))
    # Prefer created_at as proxy for posted
    iso = created or updated
    return {
        "created_at": created,
        "updated_at": updated,
        "iso": iso,
        "date": (iso[:10] if iso else None),
        "raw": j,
    }

def main():
    # <<< Put your job page URL here >>>
    url = "https://job-boards.greenhouse.io/xai/jobs/4876457007"

    # --- 1) Fetch HTML and try JSON-LD ---
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = client.get(url, timeout=20)
        r.raise_for_status()
        html = r.text

    posted_iso = extract_dateposted_from_html(html)
    print("URL:", url)
    print("HTML JSON-LD date:", posted_iso or "Not found")

    # --- 2) If missing, fall back to the public board API detail ---
    # Derive token + id from the URL path: .../TOKEN/jobs/ID
    # This matches job-boards.greenhouse.io/<token>/jobs/<id>
    try:
        token = url.split("/")[3]
        job_id = url.split("/")[5].split("?")[0]
    except Exception:
        token, job_id = None, None

    if (not posted_iso) and token and job_id:
        api_info = gh_detail_api_date(token, job_id)
        if api_info.get("iso"):
            print("API fallback (created/updated):", api_info["iso"])
            print("API fallback date:", api_info["date"])
        else:
            print("API fallback: Not available")
    elif not posted_iso:
        print("Could not parse token/job_id from URL; skipping API fallback.")

if __name__ == "__main__":
    main()
