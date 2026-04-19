# app/sources/rss.py
from __future__ import annotations
from typing import Dict, Any, List
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
import xml.etree.ElementTree as ET

UA = {"User-Agent": "AutoApply/1.0 (+rss)"}

# Default fields we’ll look at for posted date (overridable via src["parse"]["posted_date_fields"])
DEFAULT_DATE_FIELDS = ["pubDate", "published", "updated", "dc:date"]


def collect_rss(src: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Collect jobs from a single RSS/Atom feed as described in sources.yaml.

    Expected src example (main dispatcher passes one feed at a time):
    {
      "id": "rss_weworkremotely_programming",
      "type": "rss",
      "enabled": true,
      "feed": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
      "filters": {
        "query": ["ai","ml","nlp"],
        "locations_include": ["Remote","United States"]
      },
      "parse": {
        "posted_date_fields": ["pubDate","published","updated"]  # optional
      }
    }
    Returns: List[raw job dicts] shaped for your normalize layer.
    """
    feed_url: str = (src.get("feed") or "").strip()
    if not feed_url:
        return []

    filters: Dict[str, Any] = (src.get("filters") or {})
    parse_cfg: Dict[str, Any] = (src.get("parse") or {})
    date_fields: List[str] = (parse_cfg.get("posted_date_fields") or DEFAULT_DATE_FIELDS)

    try:
        r = requests.get(feed_url, headers=UA, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[rss] error fetching {feed_url}: {e}")
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"[rss] parse error for {feed_url}: {e}")
        return []

    items = _extract_items(root)
    out: List[Dict[str, Any]] = []
    for itm in items:
        title = _first_text(itm, ["title"])
        link = _first_link(itm)
        desc = _first_text(itm, ["description", "content", "summary"])
        loc  = _infer_location(title, desc)
        posted_iso = _first_date(itm, date_fields)

        rec = {
            "source": "rss",
            "feed_url": feed_url,
            "title": title,
            "company": _infer_company_from_link(link) or _infer_company_from_title(title),
            "location": loc,
            "remote_type": "",  # let eligibility/normalize decide
            "posted_date": posted_iso,  # normalize will coerce
            "apply_url": link,
            "absolute_url": link,
            "ats_type": "rss",
            "meta": {"description": desc},
        }

        if _passes_filters(rec, filters):
            out.append(rec)

    return out


# -----------------------
# XML helpers (RSS/Atom)
# -----------------------
def _extract_items(root: ET.Element) -> List[ET.Element]:
    """
    Support both RSS (<channel><item>) and Atom (<feed><entry>).
    Return a list of item-like elements to process uniformly.
    """
    # RSS
    channel = root.find("channel")
    if channel is not None:
        return channel.findall("item")

    # Atom
    return root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall("entry")


def _first_text(node: ET.Element, tags: List[str]) -> str:
    for t in tags:
        el = node.find(t)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
        # try namespaced versions (dc, atom)
        if ":" in t:
            # caller provided a namespaced tag like dc:date
            ns, name = t.split(":", 1)
            el = node.find(name)  # best effort if ns not bound
            if el is not None and (el.text or "").strip():
                return el.text.strip()
    return ""


def _first_link(node: ET.Element) -> str:
    # RSS <link>
    el = node.find("link")
    if el is not None and (el.text or "").strip():
        return el.text.strip()

    # Atom <link href="...">
    for link in node.findall("{http://www.w3.org/2005/Atom}link"):
        href = link.attrib.get("href")
        if href:
            return href.strip()

    # Fallback: try common non-namespaced <link href="">
    for link in node.findall("link"):
        href = link.attrib.get("href")
        if href:
            return href.strip()

    return ""


def _first_date(node: ET.Element, date_fields: List[str]) -> str:
    for tag in date_fields:
        # exact tag
        el = node.find(tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
        # best-effort: try common namespaced variants
        if ":" in tag:
            _, name = tag.split(":", 1)
            el2 = node.find(name)
            if el2 is not None and (el2.text or "").strip():
                return el2.text.strip()
    # last resort: now
    return datetime.now(timezone.utc).isoformat()


# -----------------------
# Filtering & inference
# -----------------------
def _passes_filters(rec: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    if not filters:
        return True

    q_terms: List[str] = [t for t in (filters.get("query") or []) if t]
    loc_includes: List[str] = [t for t in (filters.get("locations_include") or []) if t]

    title = (rec.get("title") or "").lower()
    desc  = (rec.get("meta", {}).get("description") or "").lower()
    loc   = (rec.get("location") or "").lower()

    if q_terms:
        if not any(term.lower() in title or term.lower() in desc for term in q_terms):
            return False

    if loc_includes:
        if not any(inc.lower() in loc for inc in loc_includes):
            return False

    return True


def _infer_company_from_link(link: str) -> str:
    if not link:
        return ""
    try:
        host = urlparse(link).hostname or ""
        host = host.lower()
        # strip common www.
        if host.startswith("www."):
            host = host[4:]
        # heuristic: take domain root as company (e.g., weworkremotely.com → WeWorkRemotely)
        base = host.split(".")[-2] if "." in host else host
        return base.replace("-", " ").title()
    except Exception:
        return ""


def _infer_company_from_title(title: str) -> str:
    # Extremely light heuristic; leave empty if uncertain.
    t = (title or "").strip()
    return ""


def _infer_location(title: str, description: str) -> str:
    # Basic remote detection; many RSS feeds don’t include structured location.
    text = f"{title or ''} {description or ''}".lower()
    if "remote" in text or "work from home" in text:
        return "Remote"
    return ""
