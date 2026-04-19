"""
Gemini-based page analyzer: classify page state and extract fields when heuristics fail.

If gemini_api_key is missing or google.generativeai is unavailable, returns None.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    import google.generativeai as genai  # type: ignore

    _HAS_GEMINI = True
except Exception as e:
    print("[DEBUG] Gemini analyzer import failed:", e)
    _HAS_GEMINI = False


PROMPT = """You are extracting web form fields from an ATS job application page.
Given the HTML snippet, classify the page and extract fields.
Return a compact JSON with:
{
  "state": "form" | "login" | "account_creation" | "confirmation" | "other",
  "fields": [
    {"label": "...", "type": "text|textarea|select|radio|checkbox|file|date|password|email", "options": ["opt1", ...] or null}
  ],
  "actions": ["apply", "next", "submit"] // optional
}
Only return JSON. Do not include explanations."""


def analyze_html(html: str, api_key: str, model_name: str) -> Optional[Dict[str, Any]]:
    if not _HAS_GEMINI or not api_key:
        if not _HAS_GEMINI:
            print("[DEBUG] Gemini analyzer skipped: SDK not available")
        else:
            print("[DEBUG] Gemini analyzer skipped: API key missing")
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        # trim html to avoid overlong prompts
        snippet = html[:20000]
        print("[DEBUG] Gemini analyzer prompt length:", len(snippet))
        resp = model.generate_content(f"{PROMPT}\nHTML:\n{snippet}")
        txt = resp.text or ""
        if not txt:
            print("[DEBUG] Gemini analyzer empty response")
            return None
        print("[DEBUG] Gemini analyzer raw response:", txt[:1000])
        txt = txt.strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            if txt.lower().startswith("json"):
                txt = txt[4:]
        start = txt.find("{")
        if start != -1:
            txt = txt[start:]
        try:
            data = json.loads(txt)
        except Exception as e:
            print("[DEBUG] Gemini analyzer JSON parse error:", e)
            return None
        return data if isinstance(data, dict) else None
    except Exception as e:
        print("[DEBUG] Gemini analyzer exception:", e)
        return None
