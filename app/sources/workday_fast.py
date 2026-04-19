# app/sources/workday.py
from __future__ import annotations
import asyncio, json, re, traceback, time
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlencode, urlparse
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

DEBUG = True

# --------------------------
# Models
# --------------------------
@dataclass
class Tenant:
    name: str
    host: str
    dc: str
    site: str
    facets: Dict[str, List[str]]
    export_basename: str
    defaults_q: List[str]

@dataclass
class JobRow:
    tenant: str
    title: str
    location: str
    posted_date: str  # may be Workday label or ISO date until normalization
    apply_url: str

# --------------------------
# Config helpers (YAML → Tenants)
# --------------------------
def tenants_from_cfg(cfg: Dict[str, Any]) -> List[Tenant]:
    defaults_q = (cfg.get("defaults", {}).get("facets", {}) or {}).get("q", [])
    ts: List[Tenant] = []
    for t in cfg.get("tenants", []) or []:
        name = t["name"]; host = t["host"]; dc = t["dc"]; site = t["site"]
        facets = t.get("facets", {}) or {}
        for k in ("q", "jobFamilyGroup", "locationHierarchy1", "timeType"):
            facets.setdefault(k, [])
        export_bn = (t.get("export", {}) or {}).get("basename") or name.lower().replace(" ", "_")
        ts.append(Tenant(name, host, dc, site, facets, export_bn, defaults_q))
    return ts

def base_url(t: Tenant) -> str:
    return f"https://{t.host}.{t.dc}.myworkdayjobs.com/{t.site}"

def build_query_pairs(t: Tenant) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    q_vals = t.facets.get("q") or t.defaults_q or []
    for q in q_vals:
        if q:
            pairs.append(("q", q))
    for k in ("jobFamilyGroup", "locationHierarchy1", "timeType"):
        for v in t.facets.get(k, []) or []:
            if v:
                pairs.append((k, v))
    return pairs

def _log(msg: str) -> None:
    if DEBUG:
        print(msg)

# --------------------------
# De-dupe
# --------------------------
def _dedupe_rows(rows: List[JobRow]) -> List[JobRow]:
    seen = set(); out: List[JobRow] = []
    for r in rows:
        key = (r.tenant, r.title.strip().lower(), r.apply_url)
        if key in seen:
            continue
        seen.add(key); out.append(r)
    return out

# --------------------------
# API-first (CXS), DOM-fallback
# --------------------------
def _endpoint(t: Tenant) -> str:
    return f"https://{t.host}.{t.dc}.myworkdayjobs.com/wday/cxs/{t.host}/{t.site}/jobs"

def _headers(t: Tenant) -> Dict[str, str]:
    origin = f"https://{t.host}.{t.dc}.myworkdayjobs.com"
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": f"{origin}/{t.site}",
        "User-Agent": "Mozilla/5.0",
    }

def _api_payload(search_text: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    body = {"appliedFacets": {}, "limit": limit, "offset": offset}
    if search_text:
        body["searchText"] = search_text
    return body

def _brand_from_url(u: str) -> str:
    try:
        from urllib.parse import urlparse
        h = (urlparse(u).hostname or "").split(".")
        # e.g., ['micron','wd1','myworkdayjobs','com'] -> 'Micron'
        return h[0].strip().title() if h else ""
    except Exception:
        return ""

def _flatten_jobs_from_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    def walk(x: Any):
        if isinstance(x, dict):
            if "jobPostings" in x and isinstance(x["jobPostings"], list):
                items.extend(x["jobPostings"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return items

def _job_to_row(t: Tenant, it: Dict[str, Any]) -> Optional[JobRow]:
    title = (it.get("title") or it.get("titleText") or "").strip()
    if not title:
        return None

    external_path = it.get("externalPath") or it.get("externalURL") or it.get("externalUrl") or ""
    if not external_path:
        return None
    url = external_path if external_path.startswith("http") else f"https://{t.host}.{t.dc}.myworkdayjobs.com{external_path}"

    # ---- location (robust fallbacks) ----
    loc = ""
    # 1) Common keys
    for k in ("locationsText", "locations", "location"):
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            loc = v.strip(); break
        if isinstance(v, list) and v:
            sv = []
            for elem in v:
                if isinstance(elem, str): sv.append(elem)
                elif isinstance(elem, dict):
                    lab = (elem.get("label") or elem.get("name") or "").strip()
                    if lab: sv.append(lab)
            if sv:
                loc = ", ".join(sv); break
    # 2) Other variants some tenants use
    if not loc:
        v = it.get("primaryLocation") or it.get("normalizedLocation") or it.get("primaryPostingLocation")
        if isinstance(v, str) and v.strip():
            loc = v.strip()
        elif isinstance(v, dict):
            lab = (v.get("label") or v.get("name") or "").strip()
            if lab:
                loc = lab
    # 3) Recover from the /job/<slug>/ segment if still empty
    if not loc and external_path:
        loc = _slug_to_location_from_path(external_path) or ""

    posted = (it.get("postedOn") or it.get("postedDate") or it.get("timePosted") or "").strip()
    return JobRow(tenant=t.name, title=title, location=loc, posted_date=posted, apply_url=url)

# --------------------------
# DOM listing (async Playwright)
# --------------------------
async def _route_block(route):
    req = route.request
    if req.resource_type in ("image","media","font","stylesheet"):
        return await route.abort()
    return await route.continue_()

async def _paginate_light(page, dom_max_steps: int):
    last_count = -1
    for step in range(dom_max_steps):
        try:
            more = page.locator('[data-automation-id="searchPaginationButton"], button:has-text("More"), button:has-text("Show more"), button:has-text("Load more")')
            if await more.count() > 0:
                await more.first.click()
                await page.wait_for_timeout(600)
        except Exception:
            pass
        cur = await page.locator('[data-automation-id="jobTitle"] a, a[href*="/job/"]').count()
        if cur <= last_count:
            h1 = await page.evaluate("() => document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            h2 = await page.evaluate("() => document.body.scrollHeight")
            if h2 <= h1:
                break
        last_count = cur

_LABEL_RE = re.compile(
    r"(?:posted\s*)?(?:today|yesterday|\d+\s*day[s]?\s*ago|30\+\s*day[s]?\s*ago|\d+\s*hour[s]?\s*ago|\d+\s*minute[s]?\s*ago)",
    re.I,
)

async def _extract_list_jobs_async(page) -> List[Dict[str, str]]:
    jobs: List[Dict[str, str]] = []
    links = page.locator('[data-automation-id="jobTitle"] a, a[href*="/job/"]')
    count = await links.count()
    for i in range(count):
        try:
            a = links.nth(i)
            href = await a.get_attribute("href") or ""
            if not href or "/job/" not in href:
                continue
            url = await a.evaluate("el => el.href")
            title = (await a.inner_text() or "").strip()
            # climb to a plausible card container
            card = a.locator(
                "xpath=ancestor::div[contains(@class,'css') or contains(@data-automation-id,'job')][1]"
            )
            if await card.count() == 0:
                card = a.locator("xpath=ancestor::div[1]")

            # location
            location = ""
            try:
                loc_node = card.locator('[data-automation-id="locations"]')
                if await loc_node.count():
                    location = (await loc_node.first.inner_text() or "").strip()
            except Exception:
                pass

            # posted label
            posted = ""
            try:
                pd = card.locator('[data-automation-id="postedOn"], [data-automation-id="textPostedOn"]')
                if await pd.count():
                    posted = (await pd.first.inner_text() or "").strip()
            except Exception:
                pass
            if not posted:
                try:
                    dt_posted = card.locator(
                        "xpath=.//dt[translate(normalize-space(.), 'POSTED ON', 'posted on')='posted on']/following-sibling::dd[1]"
                    )
                    if await dt_posted.count():
                        posted = (await dt_posted.first.inner_text() or "").strip()
                except Exception:
                    pass
            if not posted:
                try:
                    full_text = (await card.inner_text() or "").strip()
                    m = _LABEL_RE.search(full_text)
                    if m:
                        posted = m.group(0).strip()
                except Exception:
                    pass

            jobs.append({"title": title, "location": location, "posted": posted, "url": url})
        except Exception:
            continue
    return jobs

# --------------------------
# Detail enrichment (requests + JSON-LD)  <-- from your test.py
# --------------------------
def _requests_session(base_referer: str) -> "requests.Session":
    import requests
    s = requests.Session()
    s.headers.update({
        # strong desktop UA; some tenants serve “thin” HTML to weaker UAs
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": base_referer,   # IMPORTANT: match the tenant listing URL (with /en-US if used)
    })
    return s

# tolerant JSON-LD loader: survives trailing commas and sloppy arrays
def _json_relaxed_load(s: str):
    try:
        return json.loads(s)
    except Exception:
        s2 = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            return json.loads(s2)
        except Exception:
            return None
        
_JSONLD_DATE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
_JSONLD_TITLE = re.compile(r'"title"\s*:\s*"([^"]+)"')

_JSONLD_DATE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
_JSONLD_TITLE = re.compile(r'"title"\s*:\s*"([^"]+)"')

def _extract_jsonld_fields(html: str) -> dict:
    """
    Prefer JSON-LD JobPosting → title/datePosted/jobLocation/address + hiringOrganization.name.
    Fallback: posted date from HTML if needed.
    Returns subset of: {'title','datePosted','location','company'}
    """
    soup = BeautifulSoup(html, "html.parser")

    def _jsonld_pick_location(obj: dict) -> str:
        jl = obj.get("jobLocation")
        if isinstance(jl, dict):
            addr = jl.get("address")
            if isinstance(addr, dict):
                parts = [
                    (addr.get("addressLocality") or "").strip(),
                    (addr.get("addressRegion") or "").strip(),
                    (addr.get("addressCountry") or "").strip(),
                ]
                parts = [p for p in parts if p]
                if parts:
                    return ", ".join(parts)
        if isinstance(jl, str) and jl.strip():
            return jl.strip()
        return ""

    def _jsonld_pick_company(obj: dict) -> str:
        org = obj.get("hiringOrganization")
        if isinstance(org, dict):
            # Prefer name; fallback to @id which is sometimes a homepage URL
            name = (org.get("name") or "").strip()
            if name:
                return name
            oid = (org.get("@id") or "").strip()
            if oid:
                return _brand_from_url(oid) or oid
        if isinstance(org, str) and org.strip():
            return org.strip()
        return ""

    # 1) JSON-LD first (robust)
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (sc.string or sc.get_text() or sc.text or "").strip()
        if not raw:
            continue
        data = _json_relaxed_load(raw)
        if data is None:
            out = {}
            m_date = _JSONLD_DATE.search(raw); m_title = _JSONLD_TITLE.search(raw)
            if m_title: out["title"] = m_title.group(1)
            if m_date:  out["datePosted"] = m_date.group(1)
            return out if out else {}

        arr = data if isinstance(data, list) else [data]
        for obj in arr:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                out = {}
                if obj.get("title"):      out["title"] = obj["title"]
                if obj.get("datePosted"): out["datePosted"] = obj["datePosted"]
                loc = _jsonld_pick_location(obj)
                if loc: out["location"] = loc
                comp = _jsonld_pick_company(obj)
                if comp: out["company"] = comp
                if out: return out

    # 2) HTML fallback (posted date only)
    node = soup.select_one("[data-automation-id='postedOn'], [data-automation-id='textPostedOn']")
    if node:
        txt = node.get_text(" ", strip=True)
        if txt:
            return {"datePosted": txt}

    dt = soup.find(lambda t: t.name == "dt" and isinstance(t.string, str) and t.string.strip().lower() == "posted on")
    if dt and dt.find_next_sibling("dd"):
        txt = dt.find_next_sibling("dd").get_text(" ", strip=True)
        if txt:
            return {"datePosted": txt}

    m = re.search(r"\bPosted\s+(Today|Yesterday|\d+\s+Day[s]?\s+Ago|30\+\s+Day[s]?\s+Ago|\d+\s+Hour[s]?\s+Ago|\d+\s+Minute[s]?\s+Ago)\b", html, re.I)
    if m:
        return {"datePosted": m.group(0)}
    return {}



# DROP-IN: replace existing function body 1:1 (same name/signature).
async def _backfill_details_async(page, rows: list, limit: int, base_referer: str):
    import re
    sess = _requests_session(base_referer)

    def _looks_thin(html: str) -> bool:
        if not html or len(html) < 1200:
            return True
        if "application/ld+json" not in html and '"@type"' not in html:
            return True
        return False

    print(f"[DEBUG] backfill starting for {len(rows)} rows (limit={limit})")

    for idx, r in enumerate(rows):
        if idx >= limit:
            break
        # We now backfill if missing ISO date OR missing location
        has_iso_date = bool(r.posted_date and re.match(r"^\d{4}-\d{2}-\d{2}$", str(r.posted_date)))
        need = (not has_iso_date) or (not r.location)
        if not need:
            print(f"[DEBUG] row {idx}: has ISO date & location; skipping")
            continue

        try:
            # 1) Try requests first (faster)
            print(f"[DEBUG] row {idx}: requests → {r.apply_url}")
            resp = sess.get(r.apply_url, timeout=20)
            print(f"[DEBUG] row {idx}: status {resp.status_code}, len={len(resp.text) if resp.text else 0}")

            used_requests = False
            if resp.status_code == 200 and resp.text:
                fields = _extract_jsonld_fields(resp.text)
                print(f"[DEBUG] row {idx}: fields (req) → {fields}")

                if fields.get("title") and not r.title:
                    r.title = fields["title"].strip()
                if fields.get("datePosted") and not has_iso_date:
                    r.posted_date = fields["datePosted"].strip()
                    has_iso_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}$", r.posted_date))
                if fields.get("location") and not r.location:
                    r.location = fields["location"].strip()

                # If we now have both, or we at least have a good date and any non-thin HTML, we can skip Playwright
                if (r.location and r.posted_date) or (has_iso_date and not _looks_thin(resp.text)):
                    used_requests = True

            if not used_requests:
                # 2) Playwright fallback
                print(f"[DEBUG] row {idx}: Playwright → {r.apply_url}")
                await page.goto(r.apply_url, timeout=30000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception as e:
                    print(f"[DEBUG] row {idx}: networkidle wait failed → {e}")
                await page.wait_for_timeout(350)

                html = await page.content()
                fields = _extract_jsonld_fields(html)
                ###
                if fields.get("company"):
                    try:
                        if not getattr(r, "_company_from_detail", None):
                            r._company_from_detail = fields["company"].strip()
                    except Exception:
                        pass

                print(f"[DEBUG] row {idx}: fields (pw) → {fields}")
                if fields.get("title") and not r.title:
                    r.title = fields["title"].strip()
                if fields.get("datePosted") and not has_iso_date:
                    r.posted_date = fields["datePosted"].strip()
                if fields.get("location") and not r.location:
                    r.location = fields["location"].strip()

        except Exception as e:
            print(f"[ERROR] row {idx}: backfill exception → {e}")
            continue

# --------------------------
# Posted-date normalization
# --------------------------
PATTERNS = {
    "today":       re.compile(r"\b(?:posted\s*)?today\b", re.I),
    "yesterday":   re.compile(r"\b(?:posted\s*)?yesterday\b", re.I),
    "days":        re.compile(r"\b(?:posted\s*)?(\d+)\s*day[s]?\s*ago\b", re.I),
    "thirty_plus": re.compile(r"\b(?:posted\s*)?30\+\s*day[s]?\s*ago\b", re.I),
    "hours":       re.compile(r"\b(?:posted\s*)?(\d+)\s*hour[s]?\s*ago\b", re.I),
    "minutes":     re.compile(r"\b(?:posted\s*)?(\d+)\s*minute[s]?\s*ago\b", re.I),
}
TENANT_TZ = {
    "nvidia": "America/Los_Angeles",
    # add more tenant → tz mappings as needed
}

def _workday_label_to_days(label: str) -> int | None:
    s = (label or "").strip()
    if PATTERNS["today"].search(s): return 0
    if PATTERNS["yesterday"].search(s): return 1
    m = PATTERNS["days"].search(s)
    if m: return int(m.group(1))
    if PATTERNS["thirty_plus"].search(s): return 30
    if PATTERNS["hours"].search(s) or PATTERNS["minutes"].search(s): return 0
    return None

# NEW: parse location out of /job/<slug>/ when API omits it
def _slug_to_location_from_path(external_path: str) -> str:
    from urllib.parse import unquote
    slug = ""
    m = re.search(r"/job/([^/]+)/", external_path or "")
    if m:
        slug = m.group(1) or ""
    slug = unquote(slug).strip("/")
    if not slug:
        return ""
    # Normalize weird Workday separators, e.g., "Boise-ID---Main-Site"
    slug = slug.replace("---", " - ")
    parts = [p for p in slug.split("-") if p]  # keep words, drop empty
    # City, ST [ - Tail]
    if len(parts) >= 2 and 2 <= len(parts[1]) <= 3:
        city, st = parts[0], parts[1]
        tail = " ".join(parts[2:]).strip()
        loc = f"{city}, {st}"
        return f"{loc} - {tail}" if tail else loc
    # Fallback: best effort join
    return " ".join(parts)

def _normalize_posted_dates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for r in rows:
        if "posted_date_label" not in r:
            r["posted_date_label"] = r.get("posted_date", "")
        raw = r.get("posted_date", "")
        tenant = str(r.get("tenant", "")).lower()
        tzname = TENANT_TZ.get(tenant, "UTC")

        # Already ISO-like? keep
        if isinstance(raw, str) and re.match(r"^\d{4}-\d{2}-\d{2}", raw):
            continue

        days = _workday_label_to_days(raw if isinstance(raw, str) else "")
        if days is not None:
            tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
            now_local = datetime.now(tz)
            d_local = (now_local - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            r["posted_date"] = d_local.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return rows

# DROP-IN: replace existing function (same name/signature).
def _rows_to_dicts(rows: List[JobRow]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        # primary: tenant name; secondary: JSON-LD company; last-ditch: brand from URL
        company = (r.tenant or "").strip()
        if not company:
            company = (getattr(r, "_company_from_detail", "") or "").strip()
        if not company:
            company = _brand_from_url(r.apply_url) or ""

        out.append({
            "title": r.title,
            "company": company,               # ← NEW (what your Excel expects)
            "tenant": r.tenant,               # keep tenant too for debugging
            "location": r.location,
            "posted_date_label": r.posted_date,
            "posted_date": r.posted_date,
            "apply_url": r.apply_url,
            "ats_type": "workday",
        })
    return out



# --------------------------
# Per-tenant scrape
# --------------------------
# DROP-IN: replace existing function (same name/signature).
async def _scrape_tenant_fast(playwright, t: Tenant,
                              api_page_size: int, api_max_pages: int,
                              dom_max_steps: int, detail_backfill: bool, max_detail_opens: int) -> List[JobRow]:
    # ---------- API path (unchanged listing) ----------
    try:
        req = await playwright.request.new_context()
        search_text = " ".join(t.facets.get("q") or t.defaults_q or [])
        rows: List[JobRow] = []
        for page_idx in range(api_max_pages):
            offset = page_idx * api_page_size
            body = _api_payload(search_text, api_page_size, offset)
            resp = await req.post(_endpoint(t), data=json.dumps(body), headers=_headers(t), timeout=15000)
            if not resp.ok:
                if DEBUG: _log(f"[DEBUG] API {t.name} page {page_idx+1}: HTTP {resp.status}")
                break
            data = await resp.json()
            items = _flatten_jobs_from_json(data)
            if not items:
                break
            for it in items:
                row = _job_to_row(t, it)
                if row: rows.append(row)
            if len(items) < api_page_size:
                break
        await req.dispose()

        if rows:
            # EXACT test.py spirit: always enrich details if requested
            if detail_backfill:
                browser = await playwright.chromium.launch(headless=True)
                ctx = await browser.new_context(user_agent="Mozilla/5.0", java_script_enabled=True, locale="en-US")
                await ctx.route("**/*", lambda route: asyncio.create_task(_route_block(route)))
                page = await ctx.new_page()
                # referer with locale improves JSON-LD reliability on tenants like HARMAN
                await _backfill_details_async(page, rows, limit=len(rows), base_referer=base_url(t) + "/en-US")
                await ctx.close(); await browser.close()
            return rows
    except Exception as e:
        if DEBUG: _log(f"[DEBUG] API error {t.name}: {e}")

    # ---------- DOM fallback (unchanged listing) ----------
    try:
        browser = await playwright.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent="Mozilla/5.0", java_script_enabled=True, locale="en-US")
        await ctx.route("**/*", lambda route: asyncio.create_task(_route_block(route)))
        page = await ctx.new_page()
        start = base_url(t) + (("?" + urlencode(build_query_pairs(t), doseq=True)) if build_query_pairs(t) else "")
        _log(f"[DEBUG] open {start}")
        await page.goto(start, timeout=15000)
        await page.wait_for_timeout(600)
        await _paginate_light(page, dom_max_steps)

        jobs = await _extract_list_jobs_async(page)
        rows = [JobRow(tenant=t.name,
                       title=j.get("title",""),
                       location=j.get("location",""),
                       posted_date=(j.get("posted","") or "").strip(),
                       apply_url=j.get("url","")) for j in jobs]

        if detail_backfill and rows:
            # re-use the already-opened page/browser for efficiency
            await _backfill_details_async(page, rows, limit=len(rows), base_referer=base_url(t) + "/en-US")

        await ctx.close(); await browser.close()
        return rows
    except Exception as e:
        if DEBUG: _log(f"[WARN] DOM error {t.name}: {e}")
        return []


# --------------------------
# Orchestrator
# --------------------------
async def _run_all(tenants: List[Tenant],
                   concurrency: int,
                   api_page_size: int,
                   api_max_pages: int,
                   dom_max_steps: int,
                   detail_backfill: bool,
                   max_detail_opens: int) -> List[JobRow]:
    from playwright.async_api import async_playwright
    all_rows: List[JobRow] = []
    async with async_playwright() as p:
        sem = asyncio.Semaphore(concurrency)
        async def worker(t: Tenant):
            async with sem:
                try:
                    rows = await _scrape_tenant_fast(
                        p, t,
                        api_page_size, api_max_pages,
                        dom_max_steps, detail_backfill, max_detail_opens
                    )
                    print(f"[collector] {t.name}: {len(rows)} jobs")
                    return rows
                except Exception as e:
                    print(f"[error] {t.name}: {e}")
                    traceback.print_exc()
                    return []
        results = await asyncio.gather(*(worker(t) for t in tenants))
        for rows in results:
            all_rows.extend(rows)
    return all_rows

# --------------------------
# Public entry-point (unchanged signature)
# --------------------------
CONCURRENCY_DEFAULT = 6
API_PAGE_SIZE_DEFAULT = 50
API_MAX_PAGES_DEFAULT = 30
DOM_MAX_STEPS_DEFAULT = 8
DETAIL_BACKFILL_DEFAULT = True   # <-- enable to leverage JSON-LD enrichment by default
MAX_DETAIL_OPENS_DEFAULT = 6

def collect_workday_fast(cfg: Dict[str, Any],
                         select_name: Optional[str] = None,
                         *,
                         concurrency: int = CONCURRENCY_DEFAULT,
                         api_page_size: int = API_PAGE_SIZE_DEFAULT,
                         api_max_pages: int = API_MAX_PAGES_DEFAULT,
                         dom_max_steps: int = DOM_MAX_STEPS_DEFAULT,
                         detail_backfill: bool = DETAIL_BACKFILL_DEFAULT,
                         max_detail_opens: int = MAX_DETAIL_OPENS_DEFAULT) -> List[Dict[str, Any]]:
    tenants_all = tenants_from_cfg(cfg)
    tenants = [t for t in tenants_all if (not select_name or t.name.strip().lower() == select_name.strip().lower())]
    if not tenants:
        return []
    rows: List[JobRow] = asyncio.run(_run_all(
        tenants, concurrency, api_page_size, api_max_pages, dom_max_steps, detail_backfill, max_detail_opens
    ))
    rows = _dedupe_rows(rows)
    out = _rows_to_dicts(rows)
    out = _normalize_posted_dates(out)
    if DEBUG:
        sample = [o.get("posted_date") for o in out[:5]]
        print(f"[DEBUG] posted_date sample (first 5): {sample}")
    return out

# --------------------------
# Standalone runner
# --------------------------
# --------------------------
# Standalone runner (Micron)
# --------------------------
# --------------------------
# Standalone runner (Micron Technology)
# --------------------------
# --------------------------
# Standalone runner (Logitech)
# --------------------------
if __name__ == "__main__":
    import json, time

    # In-memory config using your tenant block
    cfg = {
        "defaults": {"facets": {"q": []}},
        "tenants": [{
            "name": "Logitech",
            "host": "logitech",
            "dc": "wd5",
            "site": "Logitech",
            "facets": {
                "q": [],
                "jobFamilyGroup": [],
                "locationHierarchy1": [],
                "timeType": []
            },
            "export": {"basename": "logitech"},
            "notes": "URL validated. Facet GUIDs intentionally left empty."
        }]
    }

    t0 = time.time()
    jobs = collect_workday_fast(
        cfg,
        select_name="Logitech",     # exact match to the tenant name above
        detail_backfill=True,       # keep True to enrich date/location via JSON-LD
        max_detail_opens=80         # bump up/down if you want faster runs
        # You can also tune: concurrency=6, api_page_size=50, api_max_pages=30, dom_max_steps=8
    )

    print(f"[done] {len(jobs)} jobs in {time.time() - t0:.1f}s")
    for j in jobs[:5]:  # show a few samples
        print(json.dumps(j, ensure_ascii=False))



