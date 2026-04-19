# app/main_collect.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List
import pathlib
import sys

# Ensure project root is on sys.path when running as a script (python app/main_collect.py)
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# De-prioritize the script directory so stdlib modules (e.g., queue) are not shadowed by app/queue.py
sys.path = [p for p in sys.path if p != str(SCRIPT_DIR)] + [str(SCRIPT_DIR)]
import importlib
import yaml
import time

from app.pipeline import normalize_jobs
from app.store_excel import ExcelJobsStore
from app.eligibility import mark_eligibility
from app.queue import compute_queue_df_from_jobs
from app.sources.workday_fast import collect_workday_fast

# Try to import location normalization helpers from top-level script
try:
    from normalize_locations_meta import parse_meta, build_normalized  # type: ignore
except Exception:
    # If running with CWD at repo_root/app, add parent to sys.path and retry
    try:
        import sys
        from pathlib import Path as _Path
        sys.path.append(str(_Path(__file__).resolve().parents[1]))
        from normalize_locations_meta import parse_meta, build_normalized  # type: ignore
    except Exception:
        parse_meta = None  # type: ignore
        build_normalized = None  # type: ignore


# ----------------------------
# Config loading & normalization
# ----------------------------
def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Missing config file: {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def apply_defaults_to_source(src: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Apply top-level defaults to a single source record (non-destructive)."""
    out = dict(src)
    for key in ("enabled", "pagination", "parse", "schedule"):
        if key not in out and key in defaults:
            out[key] = defaults[key]
    return out




def flatten_sources(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build the final list of sources from the config:
    - apply defaults
    - filter out disabled
    (Workday: no legacy tenant expansion; it is driven by config/workday.yaml)
    """
    defaults = cfg.get("defaults", {}) or {}
    sources_cfg: List[Dict[str, Any]] = cfg.get("sources", []) or []
    flattened: List[Dict[str, Any]] = []

    for raw in sources_cfg:
        base = apply_defaults_to_source(raw, defaults)
        flattened.append(base)

    return [s for s in flattened if s.get("enabled", False)]


# ----------------------------
# Dispatch to collectors
# ----------------------------
def _import_optional(module_path: str):
    """
    Import a module if present. Return None if it doesn't exist.
    This lets us wire main now and retrofit source modules one-by-one.
    """
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError:
        return None


def collect_from_source(src: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Route to the correct collector for a single source record.
    NOTE: For now we pass the entire `src` dict to each collector.
          We will update each source file to accept this signature.
    Expected return: List[raw job dicts] (pre-normalization)
    """
    typ = (src.get("type") or "").strip().lower()
    sid = src.get("id") or typ

    # greenhouse
    if typ == "greenhouse":
        mod = _import_optional("app.sources.greenhouse")
        if not mod or not hasattr(mod, "collect_greenhouse"):
            print(f"[skip] greenhouse collector not found for source id={sid}")
            return []
        return mod.collect_greenhouse(src)

    # lever
    if typ == "lever":
        mod = _import_optional("app.sources.lever")
        if not mod or not hasattr(mod, "collect_lever"):
            print(f"[skip] lever collector not found for source id={sid}")
            return []
        return mod.collect_lever(src)

    # workday (per-tenant)
        # workday (new schema only; loads config/workday.yaml)
    # app/main_collect.py  (inside collect_from_source)
    if typ == "workday":
        # Prefer the new fast collector when id == "workday"
        try:
            from app.sources import workday_fast  # new module you added
        except Exception as e:
            workday_fast = None

        try:
            cfg_path = Path("config/workday.yaml")
            workday_cfg = load_yaml(cfg_path)

            # If this source block is explicitly id == "workday", run fast path
            if sid == "workday" and workday_fast:
                sel = (src.get("select_name") or "").strip()
                # sel == "" or "*" -> run all tenants
                if sel == "*" or not sel:
                    return workday_fast.collect_workday_fast(workday_cfg)
                else:
                    return workday_fast.collect_workday_fast(workday_cfg, select_name=sel)

            # Otherwise fall back to your existing module/implementation
            mod = _import_optional("app.sources.workday")
            if not mod or not hasattr(mod, "collect_workday_tenant"):
                print(f"[skip] workday collector not found for source id={sid}")
                return []

            sel = (src.get("select_name") or "").strip()
            if sel and sel != "*":
                workday_cfg["select_name"] = sel
                return mod.collect_workday_tenant(workday_cfg)

            out = []
            for t in (workday_cfg.get("tenants") or []):
                per = dict(workday_cfg)
                per["select_name"] = t.get("name", "")
                jobs = mod.collect_workday_tenant(per)
                print(f"[collect] workday:{t.get('name','?'):<20} -> {len(jobs)}")
                out.extend(jobs)
            return out

        except Exception as e:
            print(f"[error] workday: {e}")
            return []



    # smartrecruiters
    if typ == "smartrecruiters":
        mod = _import_optional("app.sources.smartrecruiters")
        if not mod or not hasattr(mod, "collect_smartrecruiters"):
            print(f"[skip] smartrecruiters collector not found for source id={sid}")
            return []
        return mod.collect_smartrecruiters(src)

    # ashby
    if typ == "ashby":
        mod = _import_optional("app.sources.ashby")
        if not mod or not hasattr(mod, "collect_ashby"):
            print(f"[skip] ashby collector not found for source id={sid}")
            return []
        return mod.collect_ashby(src)

    # linkedin_search (disabled by default, but supported)
    if typ == "linkedin_search":
        mod = _import_optional("app.sources.linkedin_search")
        if not mod or not hasattr(mod, "collect_linkedin_search"):
            print(f"[skip] linkedin_search collector not found for source id={sid}")
            return []
        return mod.collect_linkedin_search(src)

    # indeed_search
    if typ == "indeed_search":
        mod = _import_optional("app.sources.indeed_search")
        if not mod or not hasattr(mod, "collect_indeed_search"):
            print(f"[skip] indeed_search collector not found for source id={sid}")
            return []
        return mod.collect_indeed_search(src)

    # rss: allow a source to define multiple feeds
    if typ == "rss":
        mod = _import_optional("app.sources.rss")
        if not mod or not hasattr(mod, "collect_rss"):
            print(f"[skip] rss collector not found for source id={sid}")
            return []
        feeds = (src.get("feeds") or [])
        # Each feed returns a list of raw job dicts
        out: List[Dict[str, Any]] = []
        for feed in feeds:
            out.extend(mod.collect_rss({"id": sid, "feed": feed, **src}))
        return out

    print(f"[skip] unsupported source type='{typ}' id={sid}")
    return []


def collect_all(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    sources = flatten_sources(cfg)

    print(f"[collect] active sources: {len(sources)}")
    _t0_all = time.perf_counter()
    for s in sources:
        sid = s.get("id") or s.get("type")
        try:
            _t0 = time.perf_counter()
            items = collect_from_source(s)
            dt = time.perf_counter() - _t0
            print(f"[collect] {sid:<35} -> {len(items)} in {dt:.2f}s")
            raw.extend(items)
        except Exception as e:
            print(f"[error] {sid}: {e}")
    dt_all = time.perf_counter() - _t0_all
    print(f"[collect] total raw items: {len(raw)} in {dt_all:.2f}s")
    return raw


# ----------------------------
# Main flow
# ----------------------------
def main():
    cfg = load_yaml("config/sources.yaml")
    rules = load_yaml("config/rules.yaml")

    excel_path = (cfg.get("save") or {}).get("excel_path", "jobs.xlsx")
    _t0_total = time.perf_counter()

    # 1) Collect → Normalize
    _t0 = time.perf_counter()
    raw = collect_all(cfg)
    t_collect = time.perf_counter() - _t0
    _t0 = time.perf_counter()
    jobs = normalize_jobs(raw)
    t_normalize = time.perf_counter() - _t0

    # 2) Eligibility (mutates status on each Job)
    _t0 = time.perf_counter()
    jobs = mark_eligibility(jobs, rules)
    t_elig = time.perf_counter() - _t0

    # 2.5) Normalize location using meta (Country/State/City) if helper is available
    if parse_meta and build_normalized:
        _t0 = time.perf_counter()
        updated_loc = 0
        for j in jobs:
            try:
                country, state, city = parse_meta(j.meta)
                normalized = build_normalized(country, state, city)
                if normalized:
                    if j.location != normalized:
                        j.location = normalized
                        updated_loc += 1
            except Exception:
                # Best-effort; skip problematic rows silently
                pass
        t_locnorm = time.perf_counter() - _t0
        print(f"[locations] updated: {updated_loc} / {len(jobs)} in {t_locnorm:.2f}s")
    else:
        print("[locations] normalization helpers unavailable; skipping")

    # 3) Persist (upsert by job_id)
    _t0 = time.perf_counter()
    store = ExcelJobsStore(excel_path)
    saved, updated = store.upsert_jobs(jobs)
    total = len(store.existing_ids())
    t_upsert = time.perf_counter() - _t0
    print(f"[jobs.xlsx] new: {saved} | updated: {updated} | total rows: {total} in {t_upsert:.2f}s")

    # 4) Queue sheet (ALL queued, newest-first; no per-company dedup)
    _t0 = time.perf_counter()
    df_queue = compute_queue_df_from_jobs(jobs)
    store.write_queue(df_queue)
    t_queue = time.perf_counter() - _t0
    print(f"[queue] rows: {len(df_queue)} in {t_queue:.2f}s")

    # 5) Parked sheet
    _t0 = time.perf_counter()
    from app.queue import to_dataframe  # still handy to dump parked as-is
    parked = [j for j in jobs if j.status == "parked"]
    df_parked = to_dataframe(parked)
    store.write_parked(df_parked)
    t_parked = time.perf_counter() - _t0
    print(f"[parked] rows: {len(df_parked)} in {t_parked:.2f}s")

    t_total = time.perf_counter() - _t0_total
    print(f"[timing] collect={t_collect:.2f}s | normalize={t_normalize:.2f}s | eligibility={t_elig:.2f}s | upsert={t_upsert:.2f}s | queue={t_queue:.2f}s | parked={t_parked:.2f}s | total={t_total:.2f}s")


if __name__ == "__main__":
    main()
