from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from core.config import DATA_DIR


def render_invoice_html(invoice: dict[str, Any], output_dir: Path | None = None) -> Path:
    """
    Renders a formal, print-ready HTML invoice for the completed client work.
    """
    raw_invoice_number = str(invoice["invoice_number"])
    if not re.fullmatch(r"INV-[0-9]{4}-[0-9]{4,8}", raw_invoice_number):
        raise ValueError("Invoice number has an invalid format")
    inv_num = html.escape(raw_invoice_number, quote=True)
    client = html.escape(str(invoice.get("client_name", "Valued Client")))
    tier = html.escape(str(invoice.get("service_tier", "Data Analytics & Engineering Service")).replace("_", " ").title())
    subtotal = float(invoice.get("amount_subtotal", 0.0))
    tax_rate = float(invoice.get("tax_rate", 0.0))
    tax_amount = float(invoice.get("tax_amount", 0.0))
    total = float(invoice.get("amount_total", 0.0))
    status = html.escape(str(invoice.get("status", "issued")).upper(), quote=True)
    issued_at = html.escape(str(invoice.get("issued_at", ""))[:10], quote=True)
    due_at = html.escape(str(invoice.get("due_at", ""))[:10], quote=True)
    notes = html.escape(str(invoice.get("notes", "")))
    ref = html.escape(str(invoice.get("payment_reference", "")))

    if output_dir is None:
        output_dir = DATA_DIR / "invoices"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{inv_num}.html"

    badge_color = "#10b981" if status == "PAID" else "#f59e0b" if status == "ISSUED" else "#ef4444"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Invoice {inv_num} - {client}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: #1e293b;
    background-color: #f8fafc;
    margin: 0;
    padding: 40px 20px;
  }}
  .invoice-card {{
    max-width: 800px;
    margin: 0 auto;
    background: #ffffff;
    border-radius: 12px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    padding: 48px;
    border: 1px solid #e2e8f0;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    border-bottom: 2px solid #f1f5f9;
    padding-bottom: 24px;
    margin-bottom: 32px;
  }}
  .brand {{
    font-size: 26px;
    font-weight: 800;
    color: #0f172a;
    letter-spacing: -0.02em;
  }}
  .badge {{
    display: inline-block;
    padding: 6px 14px;
    border-radius: 9999px;
    font-size: 13px;
    font-weight: 700;
    background-color: {badge_color};
    color: #ffffff;
    text-transform: uppercase;
  }}
  .meta-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-bottom: 36px;
  }}
  .meta-col h3 {{
    font-size: 12px;
    text-transform: uppercase;
    color: #64748b;
    letter-spacing: 0.05em;
    margin: 0 0 6px 0;
  }}
  .meta-col p {{
    font-size: 15px;
    font-weight: 600;
    margin: 0;
    color: #0f172a;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 32px;
  }}
  th {{
    text-align: left;
    padding: 12px 16px;
    font-size: 12px;
    text-transform: uppercase;
    color: #64748b;
    border-bottom: 2px solid #e2e8f0;
  }}
  td {{
    padding: 16px;
    border-bottom: 1px solid #f1f5f9;
    font-size: 14px;
  }}
  .total-row {{
    display: flex;
    justify-content: flex-end;
    margin-bottom: 32px;
  }}
  .total-box {{
    width: 280px;
  }}
  .total-line {{
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    font-size: 14px;
    color: #475569;
  }}
  .total-line.grand {{
    font-size: 20px;
    font-weight: 800;
    color: #0f172a;
    border-top: 2px solid #0f172a;
    padding-top: 12px;
    margin-top: 6px;
  }}
  .payment-box {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 18px;
    font-size: 13px;
    color: #475569;
  }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .invoice-card {{ box-shadow: none; border: none; padding: 20px; }}
  }}
</style>
</head>
<body>
<div class="invoice-card">
  <div class="header">
    <div>
      <div class="brand">SOLOEARN</div>
      <p style="margin: 4px 0 0 0; color: #64748b; font-size: 13px;">Autonomous Technical Services · Private Workbench</p>
    </div>
    <div style="text-align: right;">
      <span class="badge">{status}</span>
      <p style="margin: 8px 0 0 0; font-size: 18px; font-weight: 700; color: #0f172a;">{inv_num}</p>
    </div>
  </div>

  <div class="meta-grid">
    <div class="meta-col">
      <h3>Billed To</h3>
      <p>{client}</p>
      <p style="font-size: 13px; font-weight: normal; color: #64748b; margin-top: 4px;">Ref: Opportunity #{invoice.get('opportunity_id')}</p>
    </div>
    <div class="meta-col" style="text-align: right;">
      <h3>Invoice Details</h3>
      <p>Issued: {issued_at}</p>
      <p style="font-size: 13px; font-weight: normal; color: #64748b; margin-top: 4px;">Due: {due_at}</p>
      {f'<p style="font-size: 13px; color: #10b981; margin-top: 4px;">Ref: {ref}</p>' if ref else ''}
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Service Description</th>
        <th style="text-align: center;">Deliverable Package</th>
        <th style="text-align: right;">Amount (INR)</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>
          <strong>{tier}</strong>
          <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
            See the associated task QA report and client acceptance record for delivery details.
          </div>
        </td>
        <td style="text-align: center; color: #0f172a; font-weight: 600;">deliverables.zip</td>
        <td style="text-align: right; font-weight: 600;">₹{subtotal:,.2f}</td>
      </tr>
    </tbody>
  </table>

  <div class="total-row">
    <div class="total-box">
      <div class="total-line">
        <span>Subtotal</span>
        <span>₹{subtotal:,.2f}</span>
      </div>
      {f'<div class="total-line"><span>GST / Tax ({tax_rate}%)</span><span>₹{tax_amount:,.2f}</span></div>' if tax_amount > 0 else ''}
      <div class="total-line grand">
        <span>Total Due</span>
        <span>₹{total:,.2f}</span>
      </div>
    </div>
  </div>

  <div class="payment-box">
    <h4 style="margin: 0 0 8px 0; color: #0f172a; font-size: 13px;">Payment Instructions</h4>
    <p style="margin: 0 0 6px 0;">Please use the payment method agreed with the client and quote invoice reference <strong>{inv_num}</strong>.</p>
    <p style="margin: 0; font-size: 12px; color: #64748b;">This document is generated from the local work ledger. The owner must independently confirm receipt against their bank statement before recording payment. Confirm applicable tax and invoice requirements before issuing.</p>
  </div>
</div>
</body>
</html>
"""
    out_file.write_text(html_content, encoding="utf-8")
    return out_file
