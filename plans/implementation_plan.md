## Gemini-Driven Action Loop: Implementation Plan

1) **Add Planner Module**
   - Create `apply_bot/gemini_planner.py` with a prompt that takes a trimmed DOM snapshot and returns JSON: `{state, actions:[{action: click|fill|select|check|upload, target_text|target_selector|field_label, value|option_label, hints}], fields: [...]}`.
   - Configure via `gemini_api_key`/`gemini_model`; handle missing key gracefully.

2) **Planner→Executor in Navigation**
   - In `NavigationController`, add a loop: snapshot DOM (main + iframes if needed) → call planner → execute actions with Playwright primitives (click/fill/select/check/set_input_files).
   - Map special tokens like `$PROFILE.email`, `$PROFILE.workday_password`, `$PROFILE.resume_path` to profile values.
   - After executing actions, re-run the planner unless in review/confirmation/fail.

3) **Login/Account Gate Handling**
   - Teach executor to follow planner actions for login/account creation: attempt sign-in first with profile email/password; on failure, create account with the same creds; if no password provided, fail clearly.

4) **State/Termination Checks**
   - Use planner `state` plus confirmation heuristics to decide when to stop: submitted → done; review mode → stop before final submit; blocked → fail; otherwise continue loop.

5) **Fallbacks/Heuristics**
   - Keep `QuestionBlockExtractor`/AnswerEngine/FormFiller as fallback when planner returns fields; otherwise rely on planner actions.
   - Retain ATS registry for ATS-specific tweaks if needed.

6) **Logging/Debug**
   - Continue logging concise JSONL and saving debug HTML/PNG on failures; include planner state/actions and execution errors for troubleshooting.

7) **Config/Profile**
   - Ensure `config.yaml` includes Gemini key/model; `profile` includes `workday_password` (used for both sign-in and account creation).

Testing: run on a single Workday and Greenhouse URL to validate the planner loop.
