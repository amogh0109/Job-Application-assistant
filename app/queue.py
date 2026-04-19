# app/queue.py
from __future__ import annotations
from typing import List
import pandas as pd
from app.models import Job

COLUMNS = [
    "job_id","title","company","location","remote_type",
    "posted_date","apply_url","ats_type","canonical_apply_url",
    "eligible","status","meta_json"
]

def to_dataframe(jobs: List[Job]) -> pd.DataFrame:
    """
    Convert a list of Job objects to a DataFrame.
    Note: posted_date is serialized to ISO string; we also derive posted_date_dt later for sorting.
    """
    if not jobs:
        return pd.DataFrame(columns=COLUMNS + ["posted_date_dt"])

    rows = []
    for j in jobs:
        rows.append({
            "job_id": j.job_id,
            "title": j.title,
            "company": j.company,
            "location": j.location,
            "remote_type": j.remote_type,
            # Store date-only to keep UI/exports consistent
            "posted_date": (j.posted_date.date().isoformat() if getattr(j, "posted_date", None) else None),
            "apply_url": str(j.apply_url) if getattr(j, "apply_url", None) else None,
            "ats_type": j.ats_type,
            "canonical_apply_url": str(j.canonical_apply_url) if getattr(j, "canonical_apply_url", None) else None,
            "eligible": bool(j.eligible),
            "status": j.status,
            "meta_json": j.meta or {},
        })
    df = pd.DataFrame(rows, columns=COLUMNS)
    # derive posted_date_dt for sorting
    df["posted_date_dt"] = pd.to_datetime(df["posted_date"], errors="coerce", utc=True)
    return df

# -----------------------------
# NEW: queue computation (no per-company de-dup)
# -----------------------------
def compute_queue_df_from_jobs(jobs: List[Job]) -> pd.DataFrame:
    """
    Keep ALL queued jobs (including multiple from the same company), sorted newest-first.
    """
    df = to_dataframe(jobs)
    if df.empty:
        return df

    base = df[df["status"] == "queued"].copy()
    if "posted_date_dt" not in base.columns:
        base["posted_date_dt"] = pd.to_datetime(base.get("posted_date"), errors="coerce", utc=True)

    base = base.sort_values("posted_date_dt", ascending=False)
    # Ensure expected columns exist
    for c in COLUMNS:
        if c not in base.columns:
            base[c] = pd.NA
    return base[COLUMNS + ["posted_date_dt"]]

def compute_queue_df(all_jobs_df: pd.DataFrame) -> pd.DataFrame:
    """
    Same as above, but operates on an existing DataFrame with the same schema as to_dataframe().
    """
    if all_jobs_df is None or all_jobs_df.empty:
        return pd.DataFrame(columns=COLUMNS + ["posted_date_dt"])

    base = all_jobs_df[all_jobs_df["status"] == "queued"].copy()

    if "posted_date_dt" not in base.columns:
        base["posted_date_dt"] = pd.to_datetime(base.get("posted_date"), errors="coerce", utc=True)

    base = base.sort_values("posted_date_dt", ascending=False)
    for c in COLUMNS:
        if c not in base.columns:
            base[c] = pd.NA
    return base[COLUMNS + ["posted_date_dt"]]

# -----------------------------
# DEPRECATED: company-level de-dup (kept for reference; not used anymore)
# -----------------------------
def unique_by_company_sorted(_: List[Job]) -> List[Job]:
    """
    Deprecated: previously used to keep only the latest job per company.
    Now we allow multiple jobs per company in the queue; leave this
    function here only to avoid import errors in old call sites.
    """
    return _
