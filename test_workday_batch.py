import csv
from app.auto_apply import auto_apply_headless, load_profile

def test_workday_batch():
    print("Loading test profile...")
    try:
        profile = load_profile("config/auto_apply_profile.yaml")
        profile.resume_path = r"C:\Users\Amogh Wyawahare\Desktop\Resumes\AI Resume\Resume_AI.pdf"
    except Exception as e:
        print("Profile load error:", e)
        return

    links = []
    try:
        with open("workday_links.csv", "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("apply_url"):
                    links.append({
                        "company": row.get("company"),
                        "ats_type": "workday",
                        "apply_url": row["apply_url"],
                        "job_id": f"test_{row.get('company', 'unknown').replace(' ', '_')}"
                    })
    except Exception as e:
        print("Error reading CSV:", e)
        return

    links = links[1:3]  # Skip Logitech, run Micron and HP
    print(f"Loaded {len(links)} Workday applications to test.")
    
    results = []
    
    for idx, job in enumerate(links):
        print(f"\n[{idx+1}/{len(links)}] Submitting headless app to: {job['company'][:20]}")
        
        result = auto_apply_headless(job, profile, timeout_ms=25000)
        
        print(f"--> RESULT for {job['company']}: Success={result.ok}, Status={result.status}")
        results.append((job['company'], result))
        
    print("\n\n=== BATCH RUN COMPLETE ===")
    for company, res in results:
        flag = "PASS" if res.ok else "FAIL"
        print(f"{flag} - {company}: {res.status}")

if __name__ == "__main__":
    test_workday_batch()
