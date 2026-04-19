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

def inject_custom_css():
    st.markdown("""
        <style>
        /* Base Streamlit Overrides */
        .block-container {
            max-width: 1200px !important;
            padding-top: 2rem !important;
            padding-bottom: 4rem !important;
            background-color: #f8fafc !important;
        }

        /* Hide the default generic Streamlit Header */
        header[data-testid="stHeader"] {
            display: none !important;
        }

        /* Modern Typography */
        h1, h2, h3 {
            color: #0f172a !important;
            font-family: 'Inter', 'Segoe UI', sans-serif !important;
        }

        /* Job Card Wrapper Overrides (Targets st.container with border=True) */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border: none !important;
            border-radius: 16px !important;
            background: #ffffff !important;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03) !important;
            padding: 24px !important;
            margin-bottom: 24px !important;
            transition: transform 0.2s ease, box-shadow 0.2s ease !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.08), 0 4px 6px -2px rgba(0,0,0,0.04) !important;
        }
        
        /* Filter Pills styling (Select, Multiselect, Inputs) */
        div[data-testid="stTextInput"] div[data-baseweb="input"],
        div[data-testid="stSelectbox"] div[data-baseweb="select"],
        div[data-testid="stMultiSelect"] div[data-baseweb="select"] {
            border-radius: 20px !important;
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            box-shadow: 0 1px 2px 0 rgba(0,0,0,0.05) !important;
            min-height: 38px !important;
        }

        /* Buttons Styling */
        /* Primary (Green 'APPLY NOW') */
        .stButton button[kind="primary"] {
            background-color: #00e676 !important;
            color: #ffffff !important;
            border-radius: 24px !important;
            font-weight: 800 !important;
            letter-spacing: 0.5px !important;
            padding: 0.5rem 1.5rem !important;
            border: none !important;
            box-shadow: 0 4px 10px rgba(0, 230, 118, 0.3) !important;
            transition: all 0.2s ease !important;
        }
        .stButton button[kind="primary"]:hover {
            background-color: #00c853 !important;
            transform: translateY(-1px);
            box-shadow: 0 6px 14px rgba(0, 230, 118, 0.4) !important;
        }
        
        /* Secondary (Open Link / Ask Orion) */
        .stButton button[kind="secondary"] {
            background-color: #ffffff !important;
            color: #1e293b !important;
            border: 1px solid #cbd5e1 !important;
            border-radius: 24px !important;
            font-weight: 700 !important;
            padding: 0.5rem 1.5rem !important;
        }
        .stButton button[kind="secondary"]:hover {
            background-color: #f8fafc !important;
            border-color: #94a3b8 !important;
        }

        /* Job Title Button Hack (Makes View Job buttons look like plain clickable text) */
        div[class*="st-key-view_"] button {
            text-align: left !important;
            justify-content: flex-start !important;
            border: none !important;
            background: transparent !important;
            padding: 0 !important;
            font-size: 18px !important;
            font-weight: 800 !important;
            color: #0f172a !important;
            box-shadow: none !important;
            min-height: auto !important;
            height: auto !important;
            white-space: normal !important;
            line-height: 1.2 !important;
            border-radius: 0 !important;
        }
        div[class*="st-key-view_"] button:hover,
        div[class*="st-key-view_"] button:focus,
        div[class*="st-key-view_"] button:active {
            color: #3b82f6 !important;
            text-decoration: underline !important;
            background: transparent !important;
            box-shadow: none !important;
            transform: none !important;
            border: none !important;
        }

        /* Clean modern tabs */
        div.stTabs [data-baseweb="tab-list"] {
            background-color: transparent !important;
            border-bottom: 2px solid #e2e8f0 !important;
            gap: 32px;
        }
        div.stTabs [data-baseweb="tab"] {
            padding-top: 16px !important;
            padding-bottom: 12px !important;
            font-weight: 700 !important;
            color: #64748b !important;
            font-size: 15px !important;
            border-bottom: 3px solid transparent !important;
        }
        div.stTabs [aria-selected="true"] {
            color: #0f172a !important;
            border-bottom: 3px solid #0f172a !important;
        }
        
        </style>
    """, unsafe_allow_html=True)


def main():
    st.set_page_config(page_title="Apply Bot", layout="wide", initial_sidebar_state="expanded")
    inject_custom_css()
    
    # ---------------------------
    # HEADER AREA
    # ---------------------------
    t1, t2 = st.columns([0.8, 0.2])
    with t1:
        st.markdown(
            """
            <div style="display:flex; align-items:center; gap:16px;">
                <div style="font-size: 28px; font-weight: 900; background: linear-gradient(135deg, #3b82f6, #6366f1, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">Apply Bot</div>
                <div style="font-size: 20px; font-weight: 700; color: #0f172a; margin-top:4px;">JOBS  <span style="color:#94a3b8; font-weight:500; font-size:16px;">&nbsp;›&nbsp; Dashboard</span></div>
            </div>
            """, 
            unsafe_allow_html=True
        )
    with t2:
        if st.button("🔄 Collect Jobs", use_container_width=True, type="secondary"):
            with st.spinner("Collecting new jobs... This might take a while."):
                try:
                    import app.main_collect
                    app.main_collect.main()
                    st.toast("Jobs collected successfully!", icon="✅")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error collecting jobs: {e}")

    # Set up settings inside sidebar exactly like Jobright's left menu feel
    with st.sidebar:
        st.markdown("<div style='font-size: 24px; font-weight: 900; background: linear-gradient(135deg, #0cebeb, #20e3b2, #29ffc6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 32px;'>Jobright</div>", unsafe_allow_html=True)
        st.markdown("**📂 Files Setup**")
        excel_path = st.text_input("Excel file", "jobs.xlsx")
        rules_path = st.text_input("Rules file", "config/rules.yaml")
        profile_path = st.text_input("Auto-Apply profile", "config/auto_apply_profile.yaml")
        st.divider()
        st.caption("Auto Apply bot system. Designed as a modern UI.")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    tabs = st.tabs(["Recommended", "Parked", "Rules"])
    with tabs[0]:
        page_queue(excel_path, profile_path)
    with tabs[1]:
        page_parked(excel_path)
    with tabs[2]:
        page_rules(excel_path, rules_path)


if __name__ == "__main__":
    main()
