import os
import json
import yara

# =========================
# CONFIG
# =========================

RULE_FILE = "rules/index.yar"

TARGET_PATH = r"C:\Users\chapa mahindra\OneDrive\Desktop\nutrition project\nutritionprojects"

# =========================
# LOAD RULES
# =========================

print("Compiling YARA rules...")
rules = yara.compile(filepath=RULE_FILE)
print("Rules compiled successfully!\n")

# =========================
# RISK SCORE
# =========================

def calculate_risk(matches):
    score = min(len(matches) * 20, 100)

    if score >= 80:
        severity = "Critical"
    elif score >= 60:
        severity = "High"
    elif score >= 40:
        severity = "Medium"
    else:
        severity = "Low"

    return score, severity

# =========================
# SCAN FILE
# =========================

def scan_file(file_path):
    try:
        matches = rules.match(file_path)

        matched_rules = [match.rule for match in matches]

        score, severity = calculate_risk(matched_rules)

        return {
            "file": file_path,
            "matched_rules": matched_rules,
            "risk_score": score,
            "severity": severity
        }

    except Exception as e:
        print(f"[ERROR] Could not scan {file_path}: {e}")
        return None

# =========================
# SCAN DIRECTORY
# =========================

def scan_directory(directory):
    results = []

    print(f"Scanning directory:\n{directory}\n")

    for root, _, files in os.walk(directory):

        for file in files:

            full_path = os.path.join(root, file)

            result = scan_file(full_path)

            if result and result["matched_rules"]:
                results.append(result)

                print("=" * 60)
                print("FILE:", full_path)
                print("RULES:", ", ".join(result["matched_rules"]))
                print("RISK SCORE:", result["risk_score"])
                print("SEVERITY:", result["severity"])

    return results

# =========================
# MAIN
# =========================

all_results = []

if os.path.isfile(TARGET_PATH):

    result = scan_file(TARGET_PATH)

    if result:
        all_results.append(result)

elif os.path.isdir(TARGET_PATH):

    all_results = scan_directory(TARGET_PATH)

else:

    print("Target path does not exist.")

# =========================
# SAVE RESULTS
# =========================

with open("scan_results.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, indent=4)

print("\nScan complete.")
print(f"Threats found: {len(all_results)}")
print("Results saved to scan_results.json")