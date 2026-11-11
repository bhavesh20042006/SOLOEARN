"""
scripts/run_m7_local_preflight.py
M7.0.1 Local Preflight Verification CLI Runner
Runs automated end-to-end sandbox testing of invoice creation, dynamic QR/intent,
Kotak Bank statement parsing, duplicate protection, and ledger reconciliation.
"""

import sys
from pathlib import Path

# Fix Windows console encoding for UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure root directory is on PYTHONPATH
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from core.preflight import run_m7_local_preflight, get_kotak_onboarding_guide


def main() -> int:
    print("=" * 72)
    print(" SOLOEARN M7.0.1 LOCAL PREFLIGHT VERIFICATION & KOTAK ONBOARDING")
    print(" Stage A: Personal UPI & Kotak Current Account Automated Test")
    print("=" * 72)

    res = run_m7_local_preflight(verbose=True)

    print("\nPreflight Audit Step Results:")
    print("-" * 72)
    for r in res["results"]:
        status_icon = "[OK]" if r["status"] == "PASSED" else "[FAIL]"
        print(f"Step {r['step']}: {status_icon} [{r['status']}] {r['name']}")
        print(f"         {r['details']}")

    print("-" * 72)
    print(f"Overall Result: {res['steps_completed']}/{res['total_steps']} Steps Passed")

    guide = get_kotak_onboarding_guide()
    print("\n" + "=" * 72)
    print(" KOTAK MAHINDRA BANK ONBOARDING SUMMARY")
    print("=" * 72)
    print(f"* Recommended Account: {guide['recommended_account']}")
    print(f"* Average Quarterly Balance (AQB): Rs.{guide['average_quarterly_balance_inr']:,.2f}")
    print(f"* Udyam Portal: {guide['udyam_portal_url']} (Free Government Registration)")
    print(f"* Merchant App: {guide['kotak_biz_specs']['app_name']} (Zero Rental / Installation)")
    print("\nFour Key Questions to Ask Your Kotak Branch:")
    for q in guide["four_questions_for_kotak_branch"]:
        print(f"  {q}")
    print("=" * 72)

    return 0 if res["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
