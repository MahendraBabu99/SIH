import os
import yara

# Load and compile rules from rules/index.yar
print("Compiling YARA rules...")
rules = yara.compile("rules/index.yar")
print("Rules compiled successfully!\n")

# Path to scan (file or directory)
target_path = r"C:\Users\chapa mahindra\OneDrive\Desktop\nutrition project"

def scan_file(file_path):
    try:
        matches = rules.match(file_path)
        if matches:
            print(f"[!] Threat(s) found in: {file_path}")
            for match in matches:
                print(f"    -> Matched Rule: {match.rule}")
        else:
            print(f"[OK] Clean: {file_path}")
    except Exception as e:
        print(f"[ERROR] Could not scan {file_path}: {e}")

if os.path.isfile(target_path):
    print(f"Scanning file: {target_path}")
    scan_file(target_path)
elif os.path.isdir(target_path):
    print(f"Scanning directory: {target_path}\n")
    for root, _, files in os.walk(target_path):
        for file in files:
            full_path = os.path.join(root, file)
            scan_file(full_path)
else:
    print(f"Target path '{target_path}' does not exist.")