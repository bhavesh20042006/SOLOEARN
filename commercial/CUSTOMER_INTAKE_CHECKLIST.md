# SOLOEARN Customer Intake & Data Preparation Checklist

Thank you for selecting SOLOEARN for your data intelligence and business modeling needs. To ensure rapid turnaround and accurate analysis, please review the requirements below.

---

## 1. Data Privacy & Confidentiality Guarantee

Before sharing any business dataset, please note our strict operational commitments:
- **100% Local Processing:** All data cleaning, calculations, chart rendering, and report compilation run strictly on-device on a private workstation.
- **Zero Cloud Uploads:** Your raw data is never uploaded to third-party cloud analytics services or shared external servers.
- **Zero AI Training:** Your proprietary records are never used to train public machine learning or language models.
- **Non-Disclosure Agreement (NDA):** We are happy to execute standard mutual non-disclosure agreements prior to file intake upon request.

---

## 2. File Format & Size Specifications

| Parameter | Specification | Details |
|---|---|---|
| **File Format** | `.csv` (Comma-Separated Values) | UTF-8 or standard ASCII encoded. |
| **Max Record Count** | Up to **250,000 rows** per job | Ensures rapid 24–48 hour delivery. |
| **Max File Size** | Up to **25 MB** per CSV | Larger datasets can be batched or sampled. |
| **Headers** | Clean First Row | The first row should contain unique column names. |

---

## 3. Recommended Column Structures by Use Case

### A. E-Commerce / Retail Sales Data (Business Analytics Pack)
Ideal columns to include:
- `order_id` (Unique invoice or order reference)
- `order_date` (Date of transaction: `YYYY-MM-DD` preferred)
- `product_name` or `sku` (Item purchased)
- `category` (Broad product grouping)
- `city` / `state` (Shipping or store location)
- `quantity` (Number of units sold)
- `unit_price` (Price per unit before discounts)
- `discount_pct` or `discount_amount` (Discount applied)
- `net_revenue` or `total_amount` (Final order value)

### B. Statement Reconciliation Data
For two-way reconciliations:
- **File 1 (Internal Register):** `invoice_id`, `client_name`, `date`, `amount_inr`, `tax_inr`
- **File 2 (Bank / Payout Statement):** `tx_date`, `description`, `ref_number`, `credit_inr`, `debit_inr`

---

## 4. Pre-Submission Quality Checklist

Before sending your data:
- [ ] Export file directly from your source system (Shopify, WooCommerce, Tally, Zoho, ERP, etc.) as a `.csv`.
- [ ] Open the file in Notepad or Excel to verify headers are intact on Row 1.
- [ ] Mask or remove personal customer contact data (e.g., telephone numbers, email addresses, credit card numbers) that is not relevant to sales/financial analysis.
- [ ] Confirm the primary business questions you want answered (e.g., top margin products, geographic sales distribution, discount impact).

---

## 5. Client Intake Questionnaire

Please share the answers to these four brief questions when sending your file:

1. **Company & Contact:**
   - Company / Brand Name:
   - Industry (E-commerce / Retail / Wholesale / Accounting / Other):
   - Primary Contact Person & Role:

2. **Selected Service Tier:**
   - [ ] Tier 1: Business Analytics & Excel Pack (₹5,000 introductory pilot)
   - [ ] Tier 2: CSV Reconciliation Automation (₹10,000)
   - [ ] Tier 3: Data Engineering ETL Pipeline (₹15,000)
   - [ ] Tier 4: Executive Interactive Dashboard (₹20,000)

3. **Key Objectives:**
   - What are the top 2–3 decisions or insights you hope to gain from this report?
   - Any specific metrics or groupings critical to your leadership team?

4. **Turnaround & Invoicing:**
   - Required completion date:
   - Invoice entity name & GSTIN (if claiming input tax credit):
   - Payment method (UPI / IMPS / Bank NEFT):
