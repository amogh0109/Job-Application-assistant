from app.auto_apply import auto_apply_headless, load_profile

def test_workday():
    print("Loading test profile...")
    try:
        profile = load_profile("config/auto_apply_profile.yaml")
        # Ensure we use the exact path you specified to be safe
        profile.resume_path = r"C:\Users\Amogh Wyawahare\Desktop\Resumes\AI Resume\Resume_AI.pdf"
    except Exception as e:
        print("Profile load error:", e)
        return

    job_row = {
        "ats_type": "workday",
        "apply_url": "https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/Lausanne-Switzerland/Sr-AI-ML-Engineer--Video--R-D-_144373?q=AI",
        "job_id": "test_logitech_001"
    }

    print(f"Submitting headless application to: {job_row['apply_url']}")
    print("Please wait while Playwright navigates the application in the background (HEADLESS MODE)...")
    
    # We will test the exact integrated pipeline function
    result = auto_apply_headless(job_row, profile, timeout_ms=60000)
    
    print("\n--- FINAL RESULT ---")
    print(f"Success: {result.ok}")
    print(f"Status: {result.status}")
    print(f"Details: {result.details}")

if __name__ == "__main__":
    test_workday()
