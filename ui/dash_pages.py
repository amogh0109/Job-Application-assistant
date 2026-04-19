from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Tuple

import pandas as pd
import streamlit as st

from app.store_excel import ExcelJobsStore
from app.eligibility import mark_eligibility
from app.models import Job
from app.auto_apply import load_profile, auto_apply_headless

from ui.dash_data_utils import (
    _str_or_empty,
    _guess_ats_from_url,
    _to_dt,
    _merge_meta_json_row,
    load_jobs_df,
    save_jobs_df,
    compute_queue_df,
    compute_parked_df,
)


def mark_applied_inplace(df: pd.DataFrame, job_id: str) -> Tuple[bool, pd.DataFrame]:
    if df.empty:
        return False, df
    mask = df["job_id"].astype(str) == str(job_id)
    if not mask.any():
        return False, df
    df = df.copy()
    df.loc[mask, "status"] = "applied"

    def _add_applied(meta_json):
        m = _merge_meta_json_row(meta_json)
        m["applied_at"] = datetime.now(timezone.utc).isoformat()
        return json.dumps(m, ensure_ascii=False)

    df.loc[mask, "meta_json"] = df.loc[mask, "meta_json"].apply(_add_applied)
    return True, df


def move_to_parked_inplace(df: pd.DataFrame, job_id: str, reason: str = "manual") -> Tuple[bool, pd.DataFrame]:
    if df.empty:
        return False, df
    mask = df["job_id"].astype(str) == str(job_id)
    if not mask.any():
        return False, df
    df = df.copy()
    df.loc[mask, "status"] = "parked"

    def _set_reason(meta_json):
        m = _merge_meta_json_row(meta_json)
        m["parked_reason"] = (reason or "manual")
        return json.dumps(m, ensure_ascii=False)

    df.loc[mask, "meta_json"] = df.loc[mask, "meta_json"].apply(_set_reason)
    return True, df


def move_to_queue_inplace(df: pd.DataFrame, job_id: str) -> Tuple[bool, pd.DataFrame]:
    if df.empty:
        return False, df
    mask = df["job_id"].astype(str) == str(job_id)
    if not mask.any():
        return False, df
    df = df.copy()
    df.loc[mask, "status"] = "queued"
    return True, df


def load_rules(path: str) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        return {
            "location": {"allowed_regions": []},
            "titles": {"target_list": []},
            "keywords": {"must_have_any": [], "threshold": 0},
        }
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_rules(path: str, data: Dict[str, Any]) -> None:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def reapply_rules_on_jobs(df: pd.DataFrame, rules: Dict[str, Any]) -> pd.DataFrame:
    jobs: list[Job] = []
    for _, r in df.iterrows():
        pd_dt = r.get("posted_date_dt")
        pd_dt_py = None
        if pd_dt is not None and pd.notna(pd_dt):
            pd_dt_py = pd.to_datetime(pd_dt, utc=True, errors="coerce").to_pydatetime()
        posted = _to_dt(r.get("posted_date")) or pd_dt_py or datetime.now(timezone.utc)

        jobs.append(
            Job(
                job_id=str(r.get("job_id", "")),
                title=str(r.get("title") or ""),
                company=str(r.get("company") or ""),
                location=str(r.get("location") or ""),
                remote_type=str(r.get("remote_type") or ""),
                posted_date=posted,
                apply_url=str(r.get("apply_url") or ""),
                ats_type=str(r.get("ats_type") or ""),
                canonical_apply_url=str(r.get("canonical_apply_url") or ""),
                eligible=bool(r.get("eligible", False)),
                status=str(r.get("status") or "new"),
                meta=_merge_meta_json_row(r.get("meta_json")),
            )
        )

    jobs = mark_eligibility(jobs, rules)

    df_out = df.copy()
    for j in jobs:
        mask = df_out["job_id"].astype(str) == str(j.job_id)
        if not mask.any():
            continue
        df_out.loc[mask, "eligible"] = bool(j.eligible)
        df_out.loc[mask, "status"] = j.status
        df_out.loc[mask, "meta_json"] = json.dumps(j.meta or {}, ensure_ascii=False)
    return df_out


def _safe_link(url: str | None, text: str) -> str:
    if not url:
        return text
    try:
        u = str(url).strip()
        if not u:
            return text
        return f"[{text}]({u})"
    except Exception:
        return text


def page_queue(excel_path: str, profile_path: str):
    st.header("Queue")

    df = load_jobs_df(excel_path)
    if df.empty:
        st.info("No jobs found. Run your collector to populate jobs.xlsx.")
        return

    # Toolbar: search, ATS multiselect, date chips, eligible toggle
    t1, t2, t3, t4 = st.columns([3, 3, 3, 1])
    with t1:
        search = st.text_input("Search (title or company)", "").strip()
    with t2:
        def _ats_display_name_row(r):
            return _str_or_empty(r.get("ats_type")) or _guess_ats_from_url(_str_or_empty(r.get("canonical_apply_url")) or _str_or_empty(r.get("apply_url"))) or "Unknown"
        ats_options = sorted(set(df.apply(_ats_display_name_row, axis=1).tolist()))
        ats_selected = st.multiselect(
            "ATS", ats_options, default=[], key="queue_toolbar_ats", placeholder="Choose options"
        )
    with t3:
        days_choice = st.radio("Posted within", ["1","2","3","7","14","All"], index=2, horizontal=True)
    with t4:
        eligible_only = st.checkbox("Eligible only", value=False)

    # Build and filter frame
    qdf = compute_queue_df(df).copy()
    total_in_queue = len(qdf)
    if search:
        s = search.lower()
        qdf = qdf[qdf["title"].astype(str).str.lower().str.contains(s, na=False) | qdf["company"].astype(str).str.lower().str.contains(s, na=False)]
    if ats_selected and len(ats_selected) != len(ats_options):
        view_ats = qdf.apply(_ats_display_name_row, axis=1)
        qdf = qdf[view_ats.isin(ats_selected)]
    if days_choice != "All":
        days = int(days_choice)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        qdf["posted_date_dt"] = pd.to_datetime(qdf.get("posted_date_dt") or qdf.get("posted_date"), utc=True, errors="coerce")
        qdf = qdf[qdf["posted_date_dt"] >= cutoff]
    if eligible_only and "eligible" in qdf.columns:
        qdf = qdf[qdf["eligible"] == True]
    qdf["posted_date_dt"] = pd.to_datetime(qdf.get("posted_date_dt") or qdf.get("posted_date"), utc=True, errors="coerce")
    qdf = qdf.sort_values("posted_date_dt", ascending=False, na_position="last")

    full_qdf = compute_queue_df(df)
    total_in_queue = len(full_qdf)

    # Apply filters
    qdf = full_qdf.copy()
    if "posted_date_dt" not in qdf.columns:
        qdf["posted_date_dt"] = pd.Series(dtype="datetime64[ns, UTC]")

    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        qdf["posted_date_dt"] = pd.to_datetime(qdf["posted_date_dt"], utc=True, errors="coerce")
        qdf = qdf[qdf["posted_date_dt"] >= cutoff]

    if eligible_only and "eligible" in qdf.columns:
        qdf = qdf[qdf["eligible"] == True]

    if title_kw:
        qdf = qdf[qdf["title"].astype(str).str.contains(title_kw, case=False, na=False)]
    if company_kw:
        qdf = qdf[qdf["company"].astype(str).str.contains(company_kw, case=False, na=False)]

    # Column headers with inline controls (ATS filter + date sort)
    hdr_title, hdr_ats, hdr_date, hdr_elig, hdr_apply, hdr_action = st.columns([6, 2, 2, 2, 2, 2])
    with hdr_title:
        st.markdown("**Title**")
    with hdr_ats:
        st.markdown("**ATS**")
        def _ats_display_name(r):
            return _str_or_empty(r.get("ats_type")) or _guess_ats_from_url(_str_or_empty(r.get("canonical_apply_url")) or _str_or_empty(r.get("apply_url"))) or "Unknown"
        ats_series_all = qdf.apply(_ats_display_name, axis=1)
        ats_options = sorted(set(ats_series_all.tolist()))
        selected_ats = st.multiselect(" ", ats_options, default=ats_options, label_visibility="collapsed", key="queue_ats_filter")
        if selected_ats:
            qdf = qdf[ats_series_all.isin(selected_ats)]
    with hdr_date:
        st.markdown("**Posted**")
        date_order = st.radio(" ", ["Desc", "Asc"], index=0, horizontal=True, label_visibility="collapsed", key="queue_date_order")
    with hdr_elig:
        st.markdown("**Eligibility**")
    with hdr_apply:
        st.markdown("**Apply**")
    with hdr_action:
        st.markdown("**Action**")

    # Apply date sort
    asc = date_order == "Asc"
    qdf["posted_date_dt"] = pd.to_datetime(qdf["posted_date_dt"], utc=True, errors="coerce")
    qdf = qdf.sort_values("posted_date_dt", ascending=asc, na_position="last")

    # Pagination: use offset + page size (controls at bottom)
    page_size = int(st.session_state.get("queue_page_size", 50))
    offset = int(st.session_state.get("queue_offset", 0))
    if page_size <= 0:
        page_size = 50
    # Clamp offset to valid page start
    last_start = 0 if len(qdf) == 0 else max(0, ((len(qdf) - 1) // page_size) * page_size)
    if offset > last_start:
        offset = last_start
        st.session_state["queue_offset"] = offset

    end = min(offset + page_size, len(qdf))
    st.caption(f"Total in queue: {total_in_queue} | Showing: {end - offset} (rows {offset + 1}-{end} of {len(qdf)})")

    qdf_display = qdf.iloc[offset:end].copy()

    # Rows
    if qdf_display.empty:
        st.info("No queued jobs match the current filters.")
        return

    for _, r in qdf_display.iterrows():
        job_id = str(r.get("job_id", "") or "")
        title = _str_or_empty(r.get("title"))
        company = _str_or_empty(r.get("company"))
        location = _str_or_empty(r.get("location"))
        posted_raw = _str_or_empty(r.get("posted_date"))
        apply_url = _str_or_empty(r.get("apply_url"))
        ats_url = _str_or_empty(r.get("canonical_apply_url"))
        ats_explicit = _str_or_empty(r.get("ats_type")).strip()
        ats_guess = _guess_ats_from_url(ats_url or apply_url)
        ats_name = ats_explicit or ats_guess or "Unknown"
        eligible = bool(r.get("eligible", False))

        row = st.container()
        with row:
            c1, c_ats, c2, c3, c4, c5 = st.columns([6, 2, 2, 2, 2, 2])
            with c1:
                title_md = _safe_link(apply_url, title) if title else "(no title)"
                st.markdown(f"**{title_md}**")
                st.caption(f"{company} · {location}  |  id: `{job_id}`")
            with c_ats:
                ats_display = _safe_link(ats_url, ats_name) if ats_url else (ats_name or "Unknown")
                st.markdown(ats_display)
                st.caption("ATS")
            with c2:
                st.write(posted_raw if posted_raw else "—")
                st.caption("posted")
            with c3:
                st.write("✅ Eligible" if eligible else "—")
                st.caption("eligibility")
            with c4:
                open_link = _safe_link(apply_url, "Open") if apply_url else "—"
                st.markdown(open_link)
                st.caption("apply link")
            with c5:
                if st.button("Auto Apply", key=f"auto_apply_{job_id}"):
                    try:
                        profile = load_profile(profile_path)
                    except Exception as e:
                        st.error(f"Profile error: {e}")
                        continue

                    result = auto_apply_headless(
                        {
                            "job_id": job_id,
                            "title": title,
                            "company": company,
                            "apply_url": apply_url,
                            "canonical_apply_url": ats_url,
                            "ats_type": ats_explicit,
                        },
                        profile,
                    )

                    df2 = df.copy()
                    mask_row = df2["job_id"].astype(str) == job_id

                    def _append(meta_json):
                        try:
                            m = json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
                        except Exception:
                            m = {}
                        m.setdefault("auto_apply", []).append(
                            {
                                "mode": "ats-headless",
                                "ok": bool(result.ok),
                                "status": result.status,
                                "ats": ats_name,
                                "details": result.details,
                                "ts": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        return json.dumps(m, ensure_ascii=False)

                    if mask_row.any():
                        df2.loc[mask_row, "meta_json"] = df2.loc[mask_row, "meta_json"].apply(_append)

                    if result.ok and result.status.endswith("submitted"):
                        ok_applied, df2 = mark_applied_inplace(df2, job_id)
                        if ok_applied:
                            save_jobs_df(df2, excel_path)
                            store = ExcelJobsStore(excel_path)
                            store.write_queue(compute_queue_df(df2))
                            store.write_parked(df2[df2["status"] == "parked"])
                            st.success(f"Marked applied: {job_id}")
                            st.rerun()
                        else:
                            st.warning("Job ID vanished while updating.")
                    else:
                        save_jobs_df(df2, excel_path)
                        st.info(f"Auto-apply status: {result.status}.")

    # Bottom pager controls
    pg1, pg2 = st.columns([3, 1])
    with pg1:
        new_ps = st.number_input(
            "Max rows to display",
            min_value=10,
            max_value=500,
            step=10,
            value=int(page_size),
            key="queue_page_size",
        )
    with pg2:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("Next ›", key="queue_next_page"):
            new_off = offset + int(st.session_state.get("queue_page_size", page_size))
            if new_off >= len(qdf):
                new_off = 0
            st.session_state["queue_offset"] = new_off
            st.rerun()

    st.divider()

    # Legacy actions (unchanged)
    st.subheader("Actions")
    c1, c2, c3 = st.columns([2, 2, 3])
    queued_ids = qdf["job_id"].astype(str).tolist() if not qdf.empty and "job_id" in qdf.columns else []

    with c1:
        if queued_ids:
            job_id_apply = st.selectbox("Select job_id to mark applied", queued_ids, index=0, key="apply_sel")
            if st.button("Mark Applied", key="btn_mark_applied"):
                ok, df2 = mark_applied_inplace(df, job_id_apply)
                if ok:
                    save_jobs_df(df2, excel_path)
                    store = ExcelJobsStore(excel_path)
                    store.write_queue(compute_queue_df(df2))
                    store.write_parked(df2[df2["status"] == "parked"])
                    st.success(f"Marked applied: {job_id_apply}")
                    st.rerun()
                else:
                    st.warning("Job ID not found.")
        else:
            st.selectbox("Select job_id to mark applied", [], key="apply_sel", disabled=True)
            st.button("Mark Applied", key="btn_mark_applied", disabled=True)

    with c2:
        if queued_ids:
            job_id_park = st.selectbox("Select job_id to park", queued_ids, index=0, key="park_sel")
            reason = st.text_input("Reason (optional)", "manual", key="park_reason")
            if st.button("Move to Parked", key="btn_move_parked"):
                ok, df2 = move_to_parked_inplace(df, job_id_park, reason)
                if ok:
                    save_jobs_df(df2, excel_path)
                    store = ExcelJobsStore(excel_path)
                    store.write_queue(compute_queue_df(df2))
                    store.write_parked(df2[df2["status"] == "parked"])
                    st.success(f"Moved to parked: {job_id_park}")
                    st.rerun()
                else:
                    st.warning("Job ID not found.")
        else:
            st.selectbox("Select job_id to park", [], key="park_sel", disabled=True)
            st.text_input("Reason (optional)", "manual", key="park_reason", disabled=True)
            st.button("Move to Parked", key="btn_move_parked", disabled=True)

    with c3:
        st.info("Tip: Use per-row Auto Apply for speed. Dropdowns are here as a backup.")


def page_parked(excel_path: str):
    st.header("Parked")
    df = load_jobs_df(excel_path)
    if df.empty:
        st.info("No jobs found.")
        return

    pdf = compute_parked_df(df).copy()

    def reason_from_meta(meta_json: Any) -> str:
        try:
            m = json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
            return str(m.get("parked_reason", ""))
        except Exception:
            return ""

    pdf["parked_reason"] = pdf["meta_json"].apply(reason_from_meta)

    show_cols = ["job_id", "title", "company", "location", "posted_date", "parked_reason", "apply_url"]
    present_cols = [c for c in show_cols if c in pdf.columns]
    display_df = pdf[present_cols].reset_index(drop=True)

    st.subheader("Click a row to select")
    remembered = st.session_state.get("selected_parked_job_id")
    prechecked = (
        display_df["job_id"].astype(str).eq(remembered) if remembered is not None else pd.Series([False] * len(display_df))
    )
    display_df = display_df.copy()
    display_df.insert(0, "_Select", prechecked)

    column_config = {c: st.column_config.TextColumn(c, disabled=True) for c in display_df.columns if c != "_Select"}
    column_config["_Select"] = st.column_config.CheckboxColumn("_Select", help="Select one row", default=False)

    edited = st.data_editor(
        display_df,
        key="parked_grid",
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        column_config=column_config,
    )

    selected_rows = edited.index[edited["_Select"] == True].tolist()
    if len(selected_rows) > 1:
        keep = selected_rows[0]
        edited.loc[:, "_Select"] = False
        edited.loc[keep, "_Select"] = True
        st.session_state["parked_grid"]["edited_rows"] = {}
        st.session_state["selected_parked_job_id"] = str(edited.loc[keep, "job_id"])
        st.rerun()

    if len(selected_rows) == 1:
        sel_idx = selected_rows[0]
        st.session_state["selected_parked_job_id"] = str(edited.loc[sel_idx, "job_id"])

    st.subheader("Move back to Queue")
    job_choices = edited["job_id"].astype(str).tolist() if "job_id" in edited.columns else []
    selectbox_key = "parked_select_job_id"

    if job_choices:
        remembered = st.session_state.get("selected_parked_job_id")
        if remembered in job_choices:
            st.session_state[selectbox_key] = remembered
        elif selectbox_key not in st.session_state:
            st.session_state[selectbox_key] = job_choices[0]

        job_id = st.selectbox("Selected job_id", job_choices, key=selectbox_key)

        if st.button("Move to Queue"):
            ok, df2 = move_to_queue_inplace(df, job_id)
            if ok:
                save_jobs_df(df2, excel_path)
                store = ExcelJobsStore(excel_path)
                store.write_queue(compute_queue_df(df2))
                store.write_parked(df2[df2["status"] == "parked"])
                st.success(f"Moved to queue: {job_id}")
                st.session_state.pop("selected_parked_job_id", None)
                st.rerun()
            else:
                st.warning("Job ID not found.")
    else:
        st.selectbox("Selected job_id", [], disabled=True)
        st.button("Move to Queue", disabled=True)


def page_rules(excel_path: str, rules_path: str):
    st.header("Rules")
    rules = load_rules(rules_path)

    st.subheader("Keywords")
    kw = rules.get("keywords", {})
    kw_list = st.text_area("must_have_any (comma separated)", ", ".join(kw.get("must_have_any", [])))
    threshold = st.number_input("threshold (min hits across title/company)", min_value=0, max_value=20, value=int(kw.get("threshold", 0)))

    st.subheader("Titles (optional)")
    titles = rules.get("titles", {})
    titles_list = st.text_area("target_list (comma separated)", ", ".join(titles.get("target_list", [])))

    st.subheader("Location (optional)")
    location = rules.get("location", {})
    loc_list = st.text_area("allowed_regions (comma separated)", ", ".join(location.get("allowed_regions", [])))

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Rules"):
            new_rules = {
                "keywords": {"must_have_any": [x.strip() for x in kw_list.split(",") if x.strip()], "threshold": int(threshold)},
                "titles": {"target_list": [x.strip() for x in titles_list.split(",") if x.strip()]},
                "location": {"allowed_regions": [x.strip() for x in loc_list.split(",") if x.strip()]},
            }
            save_rules(rules_path, new_rules)
            st.success("Saved rules.yaml")

    with c2:
        if st.button("Re-apply Rules to Current Jobs"):
            df = load_jobs_df(excel_path)
            new_rules = load_rules(rules_path)
            df2 = reapply_rules_on_jobs(df, new_rules)
            save_jobs_df(df2, excel_path)

            store = ExcelJobsStore(excel_path)
            qdf = compute_queue_df(df2)
            store.write_queue(qdf)
            store.write_parked(df2[df2["status"] == "parked"])
            st.success("Eligibility re-applied and sheets updated.")


# --------------------------
# Simple Table View Queue (clean toolbar + pager)
# --------------------------
def page_queue_simple(excel_path: str, profile_path: str):
    st.header("Queue")

    df = load_jobs_df(excel_path)
    if df.empty:
        st.info("No jobs found. Run your collector to populate jobs.xlsx.")
        return

    # Toolbar
    t1, t2, t3, t4 = st.columns([3, 3, 3, 1])
    with t1:
        search = st.text_input("Search (title or company)", "").strip()
    with t2:
        def _ats_display_name_row(r):
            return _str_or_empty(r.get("ats_type")) or _guess_ats_from_url(
                _str_or_empty(r.get("canonical_apply_url")) or _str_or_empty(r.get("apply_url"))
            ) or "Unknown"
        ats_options = sorted(set(df.apply(_ats_display_name_row, axis=1).tolist()))
        ats_selected = st.multiselect(
            "ATS", ats_options, default=[], key="queue_toolbar_ats", placeholder="Choose options"
        )
    with t3:
        days_choice = st.radio("Posted within", ["1","2","3","7","14","All"], index=2, horizontal=True)
    with t4:
        eligible_only = st.checkbox("Eligible only", value=False)

    # Build and filter
    qdf = compute_queue_df(df).copy()
    def _ensure_posted_dt(frame: pd.DataFrame) -> pd.DataFrame:
        col = "posted_date_dt" if "posted_date_dt" in frame.columns else ("posted_date" if "posted_date" in frame.columns else None)
        if col is not None:
            frame["posted_date_dt"] = pd.to_datetime(frame[col], utc=True, errors="coerce")
        else:
            frame["posted_date_dt"] = pd.NaT
        return frame
    qdf = _ensure_posted_dt(qdf)
    total_in_queue = len(qdf)
    if search:
        s = search.lower()
        qdf = qdf[
            qdf["title"].astype(str).str.lower().str.contains(s, na=False)
            | qdf["company"].astype(str).str.lower().str.contains(s, na=False)
        ]
    if ats_selected:                               # ← ONLY filter when user picks something
        view_ats = qdf.apply(_ats_display_name_row, axis=1)
        qdf = qdf[view_ats.isin(ats_selected)]
    if days_choice != "All":
        days = int(days_choice)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        qdf = qdf[qdf["posted_date_dt"] >= cutoff]
    if eligible_only and "eligible" in qdf.columns:
        qdf = qdf[qdf["eligible"] == True]

    # Sort newest first
    qdf = qdf.sort_values("posted_date_dt", ascending=False, na_position="last")

    # Pager state
    page_size = int(st.session_state.get("queue_page_size", 50))
    offset = int(st.session_state.get("queue_offset", 0))
    if page_size <= 0:
        page_size = 50
    last_start = 0 if len(qdf) == 0 else max(0, ((len(qdf) - 1) // page_size) * page_size)
    if offset > last_start:
        offset = last_start
        st.session_state["queue_offset"] = offset
    end = min(offset + page_size, len(qdf))
    st.caption(
        f"Total in queue: {total_in_queue} | Showing: {end - offset} (rows {offset + 1}-{end} of {len(qdf)})"
    )

        # Slice + table
    qslice = qdf.iloc[offset:end].copy()
    table = pd.DataFrame(
        {
            "title": qslice["title"].astype(str),
            "company": qslice["company"].astype(str),
            "location": qslice["location"].astype(str),
            "ats_name": qslice.apply(_ats_display_name_row, axis=1),
            "posted_date": qslice["posted_date"].astype(str),
            "eligible": qslice.get("eligible", pd.Series([False] * len(qslice))).astype(bool),
            "apply_url": qslice["apply_url"].astype(str),
            "ats_url": qslice.get("canonical_apply_url", pd.Series([""] * len(qslice))).astype(str),
        }
    )

    st.data_editor(
        table,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        height=720,
        column_config={
            "title": st.column_config.TextColumn("Title", disabled=True, width="large"),
            "company": st.column_config.TextColumn("Company", disabled=True, width="medium"),
            "location": st.column_config.TextColumn("Location", disabled=True, width="medium"),
            "ats_name": st.column_config.TextColumn("ATS", disabled=True, width="small"),
            "posted_date": st.column_config.TextColumn("Posted", disabled=True, width="small"),
            "eligible": st.column_config.CheckboxColumn("Eligible", disabled=True),
            "apply_url": st.column_config.LinkColumn("Apply", display_text="Open"),
            "ats_url": st.column_config.LinkColumn("ATS Link", display_text="Open"),
        },
        column_order=[
            "title",
            "company",
            "location",
            "ats_name",
            "posted_date",
            "eligible",
            "apply_url",
            "ats_url",
        ],
    )


    # Bottom pager controls
    # Bottom pager controls (Prev + Next above Rows; only one Next button)
    pg_l, pg_c, pg_r = st.columns([1, 6, 2])

    with pg_l:
        if st.button("‹ Prev", key="queue_prev_page_b"):
            new_off = offset - page_size
            if new_off < 0:
                new_off = 0 if len(qdf) == 0 else max(0, ((len(qdf) - 1) // page_size) * page_size)
            st.session_state["queue_offset"] = new_off
            st.rerun()

    with pg_c:
        st.markdown("&nbsp;", unsafe_allow_html=True)

    with pg_r:
        if st.button("Next ›", key="queue_next_page_b"):
            new_off = offset + int(st.session_state.get("queue_page_size", page_size))
            if new_off >= len(qdf):
                new_off = 0
            st.session_state["queue_offset"] = new_off
            st.rerun()

        # only one rows input, below the Prev/Next row
        st.number_input(
            "Rows per page",
            min_value=10,
            max_value=500,
            step=10,
            value=int(page_size),
            key="queue_page_size",
        )
    


# Override the older queue with the simple table view
page_queue = page_queue_simple
