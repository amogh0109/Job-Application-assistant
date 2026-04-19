from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlparse

import pandas as pd

from app.store_excel import ExcelJobsStore


# --------------------------
# Helpers (verbatim from ui/dashboard.py)
# --------------------------

def _str_or_empty(x):
    """Return x if it's a real string, else '' (avoids NaN/float issues)."""
    return x if isinstance(x, str) else ""


def _guess_ats_from_url(url: str | None) -> str:
    """Best-effort ATS guess from the apply/canonical URL."""
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if "lever.co" in host: return "Lever"
    if "greenhouse.io" in host or "boards.greenhouse.io" in host: return "Greenhouse"
    if "myworkdayjobs.com" in host or ".wd" in host or "workday" in host: return "Workday"
    if "ashbyhq.com" in host or "jobs.ashbyhq.com" in host: return "Ashby"
    if "smartrecruiters.com" in host or "careers.smartrecruiters.com" in host: return "SmartRecruiters"
    if "workable.com" in host: return "Workable"
    if "icims.com" in host: return "iCIMS"
    if "taleo.net" in host: return "Taleo"
    if "jobvite.com" in host: return "Jobvite"
    if "bamboohr.com" in host: return "BambooHR"
    if "recruitee.com" in host: return "Recruitee"
    return "Unknown"


def _to_dt(s: str | None) -> datetime | None:
    """Parse ISO string -> tz-aware UTC datetime (or None)."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def load_jobs_df(excel_path: str) -> pd.DataFrame:
    """Load the master jobs sheet; ensure columns exist and parse dates."""
    store = ExcelJobsStore(excel_path)
    df = store.list_jobs()
    if df.empty:
        cols = [
            "job_id","title","company","location","remote_type","posted_date",
            "apply_url","ats_type","canonical_apply_url","eligible","status","meta_json"
        ]
        return pd.DataFrame(columns=cols + ["posted_date_dt","meta"])

    expected_cols = [
        "job_id","title","company","location","remote_type","posted_date",
        "apply_url","ats_type","canonical_apply_url","eligible","status","meta_json"
    ]
    for c in expected_cols:
        if c not in df.columns:
            df[c] = None

    # Parse posted_date to datetime for filtering/sorting
    df["posted_date_dt"] = df["posted_date"].apply(_to_dt)

    # Normalize meta_json -> dict
    def _parse_meta(x):
        if isinstance(x, dict):
            return x
        if not x:
            return {}
        try:
            return json.loads(x)
        except Exception:
            return {}
    df["meta"] = df["meta_json"].apply(_parse_meta)

    return df


def save_jobs_df(df: pd.DataFrame, excel_path: str) -> None:
    """Persist the 'jobs' sheet after editing statuses, etc."""
    store = ExcelJobsStore(excel_path)

    cols = [
        "job_id","title","company","location","remote_type","posted_date",
        "apply_url","ats_type","canonical_apply_url","eligible","status","meta_json"
    ]

    def _ensure_json(x):
        if isinstance(x, str):
            return x
        try:
            return json.dumps(x or {}, ensure_ascii=False)
        except Exception:
            return "{}"

    df = df.copy()
    if "meta_json" not in df.columns:
        df["meta_json"] = "{}"
    df["meta_json"] = df["meta_json"].apply(_ensure_json)

    for c in cols:
        if c not in df.columns:
            df[c] = None
    df_out = df[cols].copy()

    store.write_sheet(df_out, "jobs")


def compute_queue_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Queued — all jobs (no company dedupe), sorted newest-first.
    Eligibility is handled in the UI filter (not here).
    Always returns a frame with posted_date_dt column.
    """
    empty_cols = [
        "job_id","title","company","location","remote_type","posted_date","apply_url",
        "ats_type","canonical_apply_url","eligible","status","meta_json","posted_date_dt"
    ]
    if df.empty:
        return pd.DataFrame(columns=empty_cols)

    base = df[df["status"] == "queued"].copy()
    if base.empty:
        return pd.DataFrame(columns=empty_cols)

    # Ensure posted_date_dt exists and is UTC
    if "posted_date_dt" not in base.columns:
        base["posted_date_dt"] = pd.to_datetime(
            base.get("posted_date", pd.Series()), utc=True, errors="coerce"
        )
    else:
        base["posted_date_dt"] = pd.to_datetime(
            base["posted_date_dt"], utc=True, errors="coerce"
        )

    # Sort newest first (NaT last)
    base = base.sort_values("posted_date_dt", ascending=False, na_position="last").copy()

    # Ensure posted_date_dt column exists
    if "posted_date_dt" not in base.columns:
        base["posted_date_dt"] = pd.Series(dtype="datetime64[ns, UTC]")

    return base


def compute_parked_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    pdf = df[df["status"] == "parked"].copy()
    if "posted_date_dt" not in pdf.columns:
        pdf["posted_date_dt"] = pd.to_datetime(pdf.get("posted_date", pd.Series()), utc=True, errors="coerce")
    return pdf


def _merge_meta_json_row(meta_json_value) -> dict:
    """Parse a row's meta_json safely to dict."""
    if isinstance(meta_json_value, dict):
        return meta_json_value.copy()
    if not meta_json_value:
        return {}
    try:
        return json.loads(meta_json_value)
    except Exception:
        return {}

