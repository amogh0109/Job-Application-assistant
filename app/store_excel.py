from __future__ import annotations
from dataclasses import dataclass
from typing import List
from pathlib import Path
from datetime import datetime
import pandas as pd
from app.models import Job

COLUMNS = [
    "job_id", "title", "company", "location", "remote_type",
    "posted_date", "apply_url", "ats_type", "canonical_apply_url",
    "eligible", "status", "meta_json"
]

@dataclass
class ExcelJobsStore:
    path: str = "jobs.xlsx"
    sheet: str = "jobs"

    # ---------- Helpers ----------
    def _empty_df(self) -> pd.DataFrame:
        return pd.DataFrame(columns=COLUMNS)

    def _load_df(self) -> pd.DataFrame:
        p = Path(self.path)
        if not p.exists():
            return self._empty_df()
        try:
            df = pd.read_excel(self.path, sheet_name=self.sheet, engine="openpyxl")
            # Ensure all columns exist
            for col in COLUMNS:
                if col not in df.columns:
                    df[col] = None
            return df[COLUMNS]
        except ValueError:
            # Sheet not found
            return self._empty_df()

    def _save_df(self, df: pd.DataFrame) -> None:
        # Keep column order & types; also apply basic Excel niceties
        with pd.ExcelWriter(self.path, engine="openpyxl", mode="w") as writer:
            df.to_excel(writer, sheet_name=self.sheet, index=False)
            ws = writer.book[self.sheet]
            # Freeze header row and add autofilter
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

    # ---------- Public API ----------
    def upsert_jobs(self, jobs: List[Job]) -> tuple[int, int]:
        """Insert/update jobs by job_id. Returns (saved, updated_or_skipped)."""
        df = self._load_df()
        if df.empty:
            base = self._empty_df()
        else:
            base = df

        # Convert incoming jobs to DataFrame
        rows = []
        for j in jobs:
            rows.append({
                "job_id": j.job_id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "remote_type": j.remote_type,
                # Save as date-only in Excel to avoid time components
                "posted_date": j.posted_date.date().isoformat(),
                "apply_url": str(j.apply_url),
                "ats_type": j.ats_type,
                "canonical_apply_url": str(j.canonical_apply_url),
                "eligible": bool(j.eligible),
                "status": j.status,
                "meta_json": __import__("json").dumps(j.meta or {}),
            })
        new_df = pd.DataFrame(rows, columns=COLUMNS)

        # Merge on job_id (upsert semantics)
        if base.empty:
            merged = new_df
            saved = len(new_df)
            skipped = 0
        else:
            # Drop duplicates coming in
            new_df = new_df.drop_duplicates(subset=["job_id"], keep="last")
            # Align dtypes
            for col in COLUMNS:
                if col not in base.columns:
                    base[col] = None
            base = base[COLUMNS]

            # Index on job_id for fast combine
            base_idx = base.set_index("job_id")
            new_idx = new_df.set_index("job_id")

            # Support for Pandas 3.0 safely
            overlap_ids = base_idx.index.intersection(new_idx.index)
            base_idx = base_idx.drop(overlap_ids)
            missing_ids = new_idx.index.difference(base_idx.index)
            appended = pd.concat([base_idx, new_idx], axis=0)

            merged = appended.reset_index()
            saved = len(missing_ids)
            skipped = len(overlap_ids)  # treated as updated/overwritten

        # Finalize column order and save
        merged = merged[COLUMNS]
        self._save_df(merged)
        return saved, skipped

    def existing_ids(self) -> set:
        df = self._load_df()
        return set(df["job_id"].dropna().astype(str)) if not df.empty else set()

    def list_jobs(self, status: str | None = None) -> pd.DataFrame:
        df = self._load_df()
        if status:
            return df[df["status"] == status].copy()
        return df.copy()
    # app/store_excel.py  (inside ExcelJobsStore)

    def write_sheet(self, df: "pd.DataFrame", sheet_name: str) -> None:
        import pandas as pd
        from pathlib import Path
        p = Path(self.path)

        # ---- sanitize: Excel can't handle tz-aware datetimes ----
        df = df.copy()
        for col in df.columns:
            if pd.api.types.is_datetime64tz_dtype(df[col]):
                # keep UTC values but drop tz info
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce").dt.tz_convert(None)

        if p.exists():
            with pd.ExcelWriter(
                self.path,
                engine="openpyxl",
                mode="a",
                if_sheet_exists="replace",
            ) as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                wb = writer.book
                ws = wb[sheet_name]
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions
        else:
            with pd.ExcelWriter(self.path, engine="openpyxl", mode="w") as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                wb = writer.book
                ws = wb[sheet_name]
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions


    def write_queue(self, df_queue: "pd.DataFrame") -> None:
        self.write_sheet(df_queue, "queue")

    def write_parked(self, df_parked: "pd.DataFrame") -> None:
        self.write_sheet(df_parked, "parked")
