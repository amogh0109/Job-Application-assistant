# verify_ats_yaml.py
from __future__ import annotations
import argparse, asyncio, json, re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import aiohttp
import pandas as pd
import yaml
from bs4 import BeautifulSoup

CONCURRENCY = 40
REQ_TIMEOUT = 12
UA = "ATS-Verifier/1.0 (+research; non-bot intent)"

# Strong ATS signatures (checked against final URL and HTML)
ATS_SIGS: List[Tuple[str, re.Pattern]] = [
    ("Workday", re.compile(r"(myworkdayjobs\.com|/wday/cxs/|Workday)", re.I)),
    ("Greenhouse", re.compile(r"(boards\.greenhouse\.io|greenhouse\.io|window\.greenhouse)", re.I)),
    ("Lever", re.compile(r"(jobs\.lever\.co|lever\.co/|window\.lever)", re.I)),
    ("Ashby", re.compile(r"(jobs\.ashbyhq\.com|ashbyhq\.com|Ashby)", re.I)),
    ("iCIMS", re.compile(r"(icims\.com|icims\.net|/icims/|iCIMS)", re.I)),
    ("SmartRecruiters", re.compile(r"(smartrecruiters\.com|jobs\.smartrecruiters\.com)", re.I)),
    ("Oracle Recruiting (ORC/Taleo)", re.compile(r"(oraclecloud\.com/hcm|/hcmUI/|taleo\.net|ORC)", re.I)),
    ("SAP SuccessFactors", re.compile(r"(successfactors\.com|career[0-9]*\.successfactors)", re.I)),
    ("Eightfold", re.compile(r"(eightfold\.ai|careers\.eightfold\.ai)", re.I)),
    ("Workable", re.compile(r"(apply\.workable\.com|workable\.com)", re.I)),
    ("BambooHR", re.compile(r"(bamboohr\.com/jobs)", re.I)),
    ("Jobvite", re.compile(r"(jobs\.jobvite\.com|jobvite\.com)", re.I)),
    ("JazzHR", re.compile(r"(applytojob\.com|jazzhr\.com)", re.I)),
    ("Breezy", re.compile(r"(breezy\.hr)", re.I)),
    ("Paylocity", re.compile(r"(paylocity\.com/careers|my\.paylocity)", re.I)),
    ("Dayforce (Ceridian)", re.compile(r"(dayforcehcm\.com|mydayforce|Dayforce Recruiting)", re.I)),
]

WORKDAY_HOST_RE = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)",
    re.I
)

@dataclass
class Company:
    company: str
    careers: str

@dataclass
class Result:
    company: str
    careers_url: str
    final_url: str
    ats: str
    verified: bool
    evidence: str
    notes: str

async def fetch(session: aiohttp.ClientSession, url: str) -> Tuple[str, str, str]:
    try:
        async with session.get(url, allow_redirects=True, timeout=REQ_TIMEOUT) as resp:
            ctype = (resp.headers.get("content-type") or "").lower()
            text = ""
            if "text" in ctype or "json" in ctype:
                text = await resp.text(errors="ignore")
            return str(resp.url), ctype, text
    except Exception:
        return url, "", ""

async def fetch_json_ok(session: aiohttp.ClientSession, url: str) -> bool:
    try:
        async with session.get(url, allow_redirects=True, timeout=REQ_TIMEOUT,
                               headers={"Accept": "application/json"}) as resp:
            if resp.status != 200:
                return False
            ctype = (resp.headers.get("content-type") or "").lower()
            text = await resp.text(errors="ignore")
            if "application/json" in ctype or text.strip().startswith(("{", "[")):
                return True
            return False
    except Exception:
        return False

def detect_ats(final_url: str, html: str) -> Optional[str]:
    blob = f"{final_url}\n{html[:200000]}"
    for vendor, pat in ATS_SIGS:
        if pat.search(blob):
            return vendor
    return None

async def verify_workday(session: aiohttp.ClientSession, final_url: str, html: str) -> Tuple[bool, str]:
    m = WORKDAY_HOST_RE.search(final_url) or WORKDAY_HOST_RE.search(html)
    if not m:
        return False, ""
    host, dc, site = m.group(1), m.group(2), m.group(3)
    site = site.strip("/")
    candidates = [
        f"https://{host}.{dc}.myworkdayjobs.com/wday/cxs/{host}/{site}/jobs?top=1",
        f"https://{host}.{dc}.myworkdayjobs.com/wday/cxs/{host}/{site}/en-US/jobs?top=1",
    ]
    for api in candidates:
        if await fetch_json_ok(session, api):
            return True, api
    return False, ""

async def secondary_probe(session: aiohttp.ClientSession, final_url: str) -> Tuple[bool, str]:
    # Light-weight JSON checks that don't need headless:
    if "jobs.lever.co" in final_url:
        test = final_url[:-1] + ".json" if final_url.endswith("/") else final_url + ".json"
        if await fetch_json_ok(session, test):
            return True, test
    if "boards.greenhouse.io" in final_url or "jobs.ashbyhq.com" in final_url:
        return True, final_url
    return False, ""

async def process_one(session: aiohttp.ClientSession, row: Company) -> Result:
    url = (row.careers or "").strip()
    if not url:
        return Result(row.company, "", "", "Unknown", False, "", "No careers URL in YAML")

    final_url, _, html = await fetch(session, url)
    ats = detect_ats(final_url, html or "")
    verified, evidence, notes = False, "", ""

    if ats == "Workday":
        verified, evidence = await verify_workday(session, final_url, html or "")
        if not verified:
            notes = "Workday signal, but CxS JSON not reachable (custom config/gating?)."
    elif ats:
        ok, ev = await secondary_probe(session, final_url)
        verified = ok or True
        evidence = ev or final_url
    else:
        ats = "Unknown"
        notes = "No strong ATS markers found (custom/JS-heavy?)."

    return Result(
        company=row.company,
        careers_url=url,
        final_url=final_url,
        ats=ats,
        verified=verified,
        evidence=evidence,
        notes=notes
    )

def read_yaml(path: str) -> List[Company]:
    data = yaml.safe_load(open(path, "r", encoding="utf-8")) or {}
    items = []
    for obj in data.get("companies", []):
        name = str(obj.get("company", "")).strip()
        careers = str(obj.get("careers", "")).strip()
        if name:
            items.append(Company(name, careers))
    return items

def write_outputs(results: List[Result]) -> None:
    # 1) CSV & JSON
    df = pd.DataFrame([asdict(r) for r in results]).sort_values(["ats","company"])
    df.to_csv("ats_report.csv", index=False)
    with open("ats_report.json", "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    # 2) Grouped Markdown
    groups: Dict[str, List[str]] = {}
    for r in results: groups.setdefault(r.ats, []).append(r.company)
    lines = ["# ATS → Companies", ""]
    for ats in sorted(groups.keys(), key=str.lower):
        lines.append(f"## {ats}")
        for name in sorted(groups[ats], key=str.lower):
            lines.append(f"- {name}")
        lines.append("")
    with open("ats_by_vendor.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 3) Unknown list for fast follow-up
    unknowns = [r.company for r in results if r.ats == "Unknown"]
    with open("unknown_list.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(unknowns))

def write_yaml_with_ats(src_yaml: str, results: List[Result]) -> None:
    data = yaml.safe_load(open(src_yaml, "r", encoding="utf-8")) or {}
    by_name = {r.company: r for r in results}
    new_companies = []
    for obj in data.get("companies", []):
        name = str(obj.get("company", "")).strip()
        if not name:
            continue
        r = by_name.get(name)
        if r:
            obj["ats"] = r.ats
            obj["verified"] = bool(r.verified)
            if r.evidence:
                obj["evidence"] = r.evidence
            if r.notes:
                obj["notes"] = r.notes
        new_companies.append(obj)
    with open("companies_f500_with_ats.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump({"companies": new_companies}, f, sort_keys=False, allow_unicode=True)

async def main_async(yaml_path: str):
    companies = read_yaml(yaml_path)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, ssl=False)
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, connector=connector) as session:
        tasks = [process_one(session, c) for c in companies]
        results: List[Result] = []
        for fut in asyncio.as_completed(tasks):
            results.append(await fut)
    # stable ordering
    results.sort(key=lambda r: (r.ats.lower(), r.company.lower()))
    write_outputs(results)
    write_yaml_with_ats(yaml_path, results)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", required=True, help="Path to companies_f500.yaml (company + careers)")
    args = ap.parse_args()
    asyncio.run(main_async(args.yaml))

if __name__ == "__main__":
    main()
