"""cicd/check_factuality.py — Read factuality scores and fail if below threshold."""
import json
import sys

THRESHOLD = 0.7  # 70% minimum factuality score

def main():
    with open("factuality_results.json") as f:
        results = json.load(f)
    
    avg_score = results.get("average_factuality_score", 0)
    total = results.get("total_items", 0)
    
    print(f"Factuality: {avg_score:.1%} across {total} items (threshold: {THRESHOLD:.0%})")
    
    if avg_score < THRESHOLD:
        print(f"FAIL: Average factuality {avg_score:.1%} is below threshold {THRESHOLD:.0%}")
        sys.exit(1)
    
    print("PASS: Factuality check passed")

if __name__ == "__main__":
    main()
