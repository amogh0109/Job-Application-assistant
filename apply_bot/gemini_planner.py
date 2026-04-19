"""
Gemini action planner: analyze a DOM snapshot and propose actions to progress an application.

The planner returns a structured JSON dict with keys:
- state: string, one of ["apply_gate","login","account_creation","form","verification","confirmation","blocked","other"]
- actions: ordered list of action dicts. Each action uses a limited verb set:
    - click: {"action":"click", "target_text": "..."} or {"action":"click", "target_selector": "..."}
    - fill: {"action":"fill", "field_label": "...", "value": "..."}
    - select: {"action":"select", "field_label": "...", "option_label": "..."}
    - check: {"action":"check", "field_label": "..."}
    - upload: {"action":"upload", "field_label": "...", "value": "..."}  # value can be $PROFILE.resume_path
- fields (optional): list of {label, type, options?} for downstream fillers.

If the Gemini SDK or API key is missing, returns None.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    import google.generativeai as genai  # type: ignore

    _HAS_GEMINI = True
except Exception as e:
    print("[DEBUG] Gemini planner import failed:", e)
    _HAS_GEMINI = False


PROMPT = """### YOUR GOAL:
You are a human applicant filling out a job application. Act intuitively based on what is on the screen.

### HOW TO THINK:
1. **ANALYZE REALITY**: Look at the page. What is the most important thing happening?
   - Entering an Email Code/PIN? -> state: "verification"
   - Correcting a form error? -> state: "form"
   - Success/Thank you message? -> state: "confirmation"
2. **PRIORITIZE OTP**: If the page asks for a code, DO NOT fill form fields. Just provide the "verification_code" action.

### ACTION FORMAT:
- `fill`, `select`, `upload`, `click`, `verification_code`.
- For `value`, use placeholders from: `$PROFILE.first_name`, `$PROFILE.last_name`, `$PROFILE.full_name`,
  `$PROFILE.preferred_name`, `$PROFILE.email`, `$PROFILE.phone`, `$PROFILE.country`, `$PROFILE.linkedin_url`,
  `$PROFILE.github`, `$PROFILE.portfolio`, `$PROFILE.resume_path`, `$PROFILE.cover_letter`.

### OUTPUT FORMAT (STRICT JSON):
You MUST return a JSON object with two keys: "state" and "actions".
Example:
{
  "state": "form",
  "actions": [
    {"action": "fill", "target": "First Name", "value": "$PROFILE.first_name"}
  ]
}

### OBSERVATIONS:
- Skip fields with `data-is-filled="true"`.
- If Submit is `data-is-disabled="true"`, ignore it and find what's missing (errors/asterisks).
- **DROPDOWNS**: If a dropdown/select has `data-available-options="[...]"`, you MUST select ONE of those exact options. Do NOT type arbitrary text.
- **FINAL STEP**: If all required fields look complete, your NEXT action MUST be `{"action": "click", "target": "Submit Application"}` (or equivalent).
- RETURN ONLY JSON."""


def plan_actions(html: str, api_key: str, model_name: str, phase: int = 0, logger: Any = None) -> Optional[Dict[str, Any]]:
    """
    Call Gemini to produce an action plan. Returns dict or None on failure/missing key.
    phase: 0=text fields, 1=dropdowns, 2=checkboxes/radios, 3=file uploads, 4=submit
    """
    def log(msg):
        if logger:
             # simple structured log
             logger.log({"debug": True, "component": "gemini_planner", "message": msg})
        else:
             print(f"[DEBUG] {msg}")

    if not _HAS_GEMINI:
        log("Gemini planner skipped: SDK not available")
        return None
    if not api_key:
        log("Gemini planner skipped: API key missing")
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        # Increase context window significantly for Flash models
        snippet = html[:500000]
        
        # Phase-specific instructions
        phase_instructions = {
            0: "### CURRENT PHASE: TEXT FIELDS ONLY\n**DO NOT SUBMIT THE FORM YET.**\nFocus ONLY on filling text inputs, email inputs, phone inputs, and textareas.\nReturn ONLY 'fill' actions for text fields.\nIgnore dropdowns, checkboxes, radios, file uploads, and submit buttons.\nIf all text fields are filled, return empty actions array: {\"state\": \"form\", \"actions\": []}",
            1: "### CURRENT PHASE: DROPDOWNS ONLY\n**DO NOT SUBMIT THE FORM YET.**\nFocus ONLY on selecting options from dropdown menus (select elements and custom dropdowns).\nReturn ONLY 'select' actions.\nIgnore text fields, checkboxes, file uploads, and submit buttons.\nIf all dropdowns are filled, return empty actions array: {\"state\": \"form\", \"actions\": []}",
            2: "### CURRENT PHASE: CHECKBOXES & RADIOS ONLY\n**DO NOT SUBMIT THE FORM YET.**\nFocus ONLY on checking/selecting checkboxes and radio buttons.\nReturn ONLY 'click' actions for checkboxes/radios.\nIgnore text fields, dropdowns, file uploads, and submit buttons.\nIf all checkboxes/radios are selected, return empty actions array: {\"state\": \"form\", \"actions\": []}",
            3: "### CURRENT PHASE: FILE UPLOADS ONLY\n**DO NOT SUBMIT THE FORM YET.**\nFocus ONLY on uploading files (resume, cover letter).\nUse 'upload' or 'click' actions for file inputs.\nIgnore all other fields and submit buttons.\nIf all files are uploaded, return empty actions array: {\"state\": \"form\", \"actions\": []}",
            4: "### CURRENT PHASE: SUBMIT\nAll previous phases are complete. Your ONLY action should be to click the Submit button.\nIf you see any errors or missing required fields, report them in the state but still propose the submit click action."
        }
        
        phase_prompt = phase_instructions.get(phase, "")
        full_prompt = f"{PROMPT}\n\n{phase_prompt}\n\nHTML:\n{snippet}"
        
        log(f"Gemini planner prompt sending, phase={phase}, length: {len(snippet)}")
        
        txt = ""
        # Simple retry for 429
        for attempt in range(2):
            try:
                resp = model.generate_content(full_prompt)
                txt = resp.text or ""
                break
            except Exception as e:
                if "429" in str(e) and attempt == 0:
                    log("Gemini 429 detected. Waiting 2s before retry...")
                    import time
                    time.sleep(2)
                    continue
                raise e

        if not txt:
            log("Gemini planner empty response")
            return None
        log(f"Gemini planner raw response: {txt[:1000]}")
        # strip common code fences
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
            log(f"Gemini planner JSON parse error: {e}")
            return None
        if isinstance(data, dict) and "state" in data:
            return data
    except Exception as e:
        log(f"Gemini planner exception: {e}")
        return None
    return None
