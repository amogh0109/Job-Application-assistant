import json
import re
from dataclasses import asdict
from playwright.sync_api import sync_playwright
import yaml
from google import genai
from app.auto_apply import load_profile

def get_gemini_client():
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    print("Using key:", c["gemini_api_key"][:10] + "...")
    return genai.Client(api_key=c["gemini_api_key"]), c.get("gemini_model", "gemini-2.5-flash")

def agentic_fill(url, profile):
    client, model = get_gemini_client()
    
    with sync_playwright() as p:
        print("[Agent 1] Analyzing the page DOM...")
        b = p.chromium.launch(headless=False)
        page = b.new_page()
        page.goto(url)
        page.wait_for_load_state("networkidle")
        
        # Step 1: Agent 1 - Map the page fields
        # Extract a simplified DOM containing all inputs, labels, selects, and checkboxes
        html_map = page.evaluate('''() => {
            let els = Array.from(document.querySelectorAll('label, input, select, textarea, div[role="combobox"]'));
            return els.map(e => {
                let id = e.id || '';
                let type = e.tagName.toLowerCase();
                let role = e.getAttribute('role') || '';
                let inputType = e.type || '';
                let name = e.name || '';
                let text = e.textContent.trim().substring(0, 100);
                return `<${type} id="${id}" name="${name}" type="${inputType}" role="${role}">${text}</${type}>`;
            }).join('\\n');
        }''')
        
        system_prompt = f"""
You are Agent 1 (The Form Inspector). 
Your goal is to look at the HTML components of a job application page and the Applicant's profile, and output a JSON array of instructions for Agent 2 to execute.

Applicant Profile:
{json.dumps(asdict(profile), indent=2)}

HTML Map:
{html_map}

Instructions available for Agent 2:
- {{"action": "fill_text", "selector": "CSS_SELECTOR", "value": "TEXT"}}
- {{"action": "upload_file", "selector": "CSS_SELECTOR", "value": "FILE_PATH"}}
- {{"action": "check_box", "selector": "CSS_SELECTOR"}} 
- {{"action": "react_select_dropdown", "selector": "CSS_SELECTOR", "value": "EXACT_TEXT_TO_TYPE_AND_ENTER"}}

Return ONLY valid JSON.
"""
        print("[Agent 1] Requesting mapping from LLM...")
        resp = client.models.generate_content(
            model=model,
            contents=system_prompt,
            config={"temperature": 0.1, "response_mime_type": "application/json"}
        )
        instructions = json.loads(resp.text)
        print(f"[Agent 1] Provided {len(instructions)} execution steps to Agent 2.")
        
        print(f"[Agent 2] Executing {len(instructions)} steps based on Agent 1's mapping...")
        # Step 2: Agent 2 - Execute actions
        for step in instructions:
            action = step.get("action")
            sel = step.get("selector")
            val = step.get("value")
            
            # Fix invalid IDs with numbers, brackets, etc.
            if sel and sel.startswith("#"):
                sel = f'[id="{sel[1:]}"]'
                
            if sel == '[id="candidate-location"]' or sel == '#candidate-location':
                continue
                
            print(f" -> Executing {action} on {sel} (Value: {val})")
            try:
                if action == "fill_text":
                    page.locator(sel).first.fill(val)
                elif action == "upload_file":
                    page.locator(sel).first.set_input_files(val)
                elif action == "check_box":
                    page.locator(sel).first.evaluate("el => el.click()")
                elif action == "react_select_dropdown":
                    combo = page.locator(sel).first
                    combo.click(force=True)
                    page.wait_for_timeout(400)
                    
                    # Wait for options and try to click the one that matches our normalizer
                    import re
                    
                    try:
                        page.locator('div[role="option"]').first.wait_for(timeout=3000)
                    except: pass
                    
                    opts = page.locator('div[role="option"]').all()
                    
                    clicked_opt = False
                    
                    # Word intersection matching to handle "do not" vs "don't"
                    val_words = set(re.sub(r'[^a-z0-9\s]', '', str(val).lower()).split())
                    
                    best_match = None
                    best_score = 0
                    
                    for o in opts:
                        t = o.text_content()
                        t_words = set(re.sub(r'[^a-z0-9\s]', '', t.lower()).split())
                        if not val_words or not t_words: continue
                        
                        score = len(val_words.intersection(t_words)) / float(len(val_words) + 0.1)
                        if score > best_score:
                            best_score = score
                            best_match = o
                            
                    if best_match and best_score > 0.4:
                        best_match.click(force=True)
                        clicked_opt = True
                        
                    # Universal fallback for "Decline to Answer" / "Prefer not to say" variations
                    if not clicked_opt and any(w in val.lower() for w in ['decline', 'wish', 'prefer', 'not']):
                        for o in opts:
                            t = o.text_content().lower()
                            if any(w in t for w in ['decline', 'wish', 'prefer', 'not']):
                                o.click(force=True)
                                clicked_opt = True
                                break
                                
                    if not clicked_opt and opts:
                        # If absolutely nothing matches but options exist, click the last one (usually 'Decline') or first
                        opts[-1].click(force=True)
                        clicked_opt = True
                             
                    if not clicked_opt:
                        combo.fill(val)
                        page.wait_for_timeout(400)
                        page.keyboard.press("ArrowDown")  
                        page.keyboard.press("Enter")
                        
                    page.keyboard.press("Escape")
            except Exception as e:
                print(f"   [Error] Failed to execute {action} on {sel}: {e}")
                
        # --- Fallback safety layer for hallucinated values or skipped checkboxes ---
        try:
            print("[Agent 2] Falling back to global explicit checkbox consent check...")
            for chk in page.locator('input[type="checkbox"]').all():
                if not chk.is_checked(): chk.evaluate("el => el.click()")
                
            print("[Agent 2] Forcing location autocomplete override...")
            loc_box = page.locator('[id="candidate-location"]')
            loc_box.click(force=True)
            page.wait_for_timeout(500)
            
            # Rely on OS-level typing on the active element instead of Playwright's .fill()
            page.keyboard.type(profile.location, delay=100)
            page.wait_for_timeout(3000) # Give Google APIs plenty of time 
            
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(200)
            page.keyboard.press("Enter")
        except Exception as ex: 
            print("Location override error:", ex)
        # --------------------------------------------------------------------------

        print("[Agent 2] Submitting the application...")
        try:
            page.locator('button:has-text("Submit application"), #submit_app, button[type="submit"]').first.click()
            print("[Agent 2] Application Submitted! Waiting 5 seconds to check errors...")
            page.wait_for_timeout(5000)
            
            invalids = page.locator('[aria-invalid="true"], .error').all()
            print(f"[Agent 2] Found {len(invalids)} validation errors after submit:")
            for i in invalids:
                print('   Invalid element:', i.evaluate('el => el.outerHTML'))
                
            page.wait_for_timeout(25000)
        except Exception as e:
            print(f"[Agent 2] Failed to click Submit: {e}")
            
        b.close()

if __name__ == "__main__":
    prof = load_profile('config/auto_apply_profile.yaml')
    agentic_fill('https://job-boards.greenhouse.io/twilio/jobs/7066029', prof)
