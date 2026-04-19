"""
Streamlit UI entrypoint.
Delegates page rendering and data helpers to ui.dash_pages and ui.dash_data_utils.
"""

from __future__ import annotations

import os
import sys
import streamlit as st

# Allow importing app/ and ui/ as packages when run via `streamlit run ui/dashboard.py`
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ui.dash_pages import (
    page_queue,
    page_parked,
    page_rules,
)


def main():
    st.set_page_config(page_title="Auto Apply", layout="wide")
    st.title("Auto Apply — Phase 1 UI")

    with st.sidebar:
        st.subheader("Settings")
        excel_path = st.text_input("Excel file", "jobs.xlsx")
        rules_path = st.text_input("Rules file", "config/rules.yaml")
        profile_path = st.text_input("Auto-Apply profile (YAML/JSON)", "config/auto_apply_profile.yaml")

        st.divider()
        st.caption(
            "Open the Queue tab to start applying. Use Parked to review filtered-out jobs. "
            "Adjust Rules as needed and re-apply."
        )

    tabs = st.tabs(["Queue", "Parked", "Rules"])
    with tabs[0]:
        page_queue(excel_path, profile_path)
    with tabs[1]:
        page_parked(excel_path)
    with tabs[2]:
        page_rules(excel_path, rules_path)


if __name__ == "__main__":
    main()

