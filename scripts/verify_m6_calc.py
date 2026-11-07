import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tax_engine import compute_required_pretax_profit, compute_income_tax_india_2026

p = compute_required_pretax_profit(1200000.0)
t = compute_income_tax_india_2026(p)
post = p - t["total_tax"]
print(f"Pre-tax Required: INR {p:,.2f}")
print(f"Total Tax: INR {t['total_tax']:,.2f}")
print(f"Post-tax Profit: INR {post:,.2f}")
print(f"Marginal relief applied: INR {t['marginal_relief']:,.2f}")
print(f"Effective Tax Rate: {t['effective_rate_pct']}%")
