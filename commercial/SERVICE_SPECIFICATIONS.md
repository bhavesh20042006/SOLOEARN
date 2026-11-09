# SOLOEARN Client Service Specifications (M6.2)

SOLOEARN delivers deterministic, locally executed data analysis, engineering, and reporting services for small businesses, e-commerce brands, retailers, and accounting professionals.

All client data is processed strictly on-device in isolated project workspaces without third-party cloud uploads or AI training on private client assets.

---

## Service 1: Business Analytics & Excel Pack (Recommended Initial Product)

**Target Audience:** D2C/E-commerce brands (Shopify, Amazon sellers), retail chains, wholesale distributors, and accounting firms needing rapid, verified sales/inventory intelligence.

- **Proposed Introductory Price:** **₹5,000** (Fixed-price pilot)
- **Standard Delivery SLA:** **24 to 48 Hours**
- **Typical Effort:** 2.0 to 3.0 productive hours
- **Contribution Margin:** **89.5%** (Estimated Net Owner Profit: ~₹4,250 after fees and tax reserve)

### Scope of Work
1. **Intake & Sanitization:**
   - Ingestion of transactional CSV exports (up to 250,000 records).
   - Automated formula injection sanitization (CWE-1236 protection) preventing spreadsheet exploits while preserving legitimate negative numbers and financial calculations.
   - Deduplication and missing-value audit.
2. **Professional Multi-Tab Excel Model (`business_analysis.xlsx`):**
   - **Tab 1: Cleaned Data** — Formatted headers, auto-filters, frozen panes, and readable column widths.
   - **Tab 2: Data Quality Profile** — Column-by-column null analysis, datatype identification, unique record counts, and anomaly log.
   - **Tab 3: Numeric Summary** — Mean, median, standard deviation, quartile distributions, and total volume metrics for all financial/operational variables.
3. **Visual Analytics & Chart Suite (`charts/`):**
   - High-resolution distribution histograms of core financial metrics (order value, discount impact, unit pricing).
   - Horizontal category contribution charts (sales by city, top-selling product categories, monthly order volume).
4. **Executive Interactive HTML Report (`report.html`):**
   - Standalone browser-viewable executive briefing.
   - Key Performance Indicator (KPI) cards (Total Volume, Unique Customers, Net Revenue, Deduplicated Rows).
   - Objective, descriptive analyst observations (no fabricated forecasts).
   - Inline visual charts and interactive data quality tables.
5. **Quality Assurance Guarantee:**
   - Pre-delivery audit by SOLOEARN's Independent QA Agent.
   - Formula integrity and mathematical consistency verified.
   - Complete deliverable packaged into a single clean ZIP archive (`deliverables.zip`).

### Exclusions & Boundaries
- Does not include real-time database connection or ERP integration.
- Does not include subjective speculative market forecasting.
- Raw data must be provided as a structured CSV or exported table.

---

## Service 2: CSV Data Reconciliation & Deduplication Automation

**Target Audience:** Accounting teams, marketplace sellers, logistics providers reconciling statements against internal registers.

- **Proposed Price:** **₹10,000**
- **Delivery SLA:** **48 Hours**
- **Contribution Margin:** **91.0%** (Estimated Net Owner Profit: ~₹8,650)

### Scope of Work
1. **Two-Way File Reconciliation:**
   - Automated matching of two transactional CSV datasets (e.g., Bank Statement vs. Invoicing Register, or Marketplace Payout vs. Order Book).
   - Discrepancy reporting: Matched Records, Unmatched Invoices, Over/Under Variance rows.
2. **Automated Python Pipeline Script (`reconciliation_engine.py`):**
   - Clean, documented, deterministic Python script provided to the client for recurring local execution.
   - Unit tests (`test_reconciliation.py`) validating matching logic.
3. **Reconciliation Workbook & Variance Summary (`reconciliation_summary.xlsx`):**
   - Cleaned matched register.
   - Flagged discrepancy exception report for accounting follow-up.
4. **Client Deliverable Package (`deliverables.zip`):**
   - Python automation script, test suite, Excel audit workbook, and execution guide.

---

## Service 3: Production Data Engineering & ETL Pipeline

**Target Audience:** Growing businesses with repetitive manual data cleaning tasks across inventory, CRM, or billing exports.

- **Proposed Price:** **₹15,000**
- **Delivery SLA:** **3 to 4 Business Days**
- **Contribution Margin:** **91.5%** (Estimated Net Owner Profit: ~₹13,100)

### Scope of Work
1. **Automated Pipeline Engine (`pipeline.py`):**
   - Modular extract, transform, and load (ETL) pipeline script.
   - Schema validation with strict type enforcement.
   - Data cleaning, normalization, date parsing, and missing-value imputation.
2. **Schema Manifest & Documentation:**
   - `schema_manifest.json` detailing expected input/output fields and validation rules.
   - `data_dictionary.md` describing field definitions and business logic.
3. **Automated Test Suite (`test_pipeline.py`):**
   - Comprehensive test assertions verifying pipeline resilience against corrupted inputs.
4. **Client Delivery Package:**
   - Full Python pipeline module, CLI runner, test suite, and operational runbook.

---

## Service 4: Executive Interactive HTML5 Dashboard

**Target Audience:** Business owners, agency heads, or retail managers wanting a lightweight, offline-accessible dashboard to explore company KPIs without expensive BI subscriptions (PowerBI, Tableau).

- **Proposed Price:** **₹20,000**
- **Delivery SLA:** **4 to 5 Business Days**
- **Contribution Margin:** **92.0%** (Estimated Net Owner Profit: ~₹17,400)

### Scope of Work
1. **Standalone Zero-Dependency Dashboard (`dashboard.html`):**
   - Self-contained HTML5 file with embedded responsive CSS and pure JavaScript.
   - Works 100% offline in any modern web browser without internet connection or server hosting.
2. **Interactive Visual Exploration:**
   - Client-side dynamic search and table filtering.
   - Interactive KPI metric cards with period-over-period comparisons.
   - SVG visual charts with responsive hover tooltips.
3. **User Documentation:**
   - `dashboard_user_guide.md` explaining navigation, KPI definitions, and browser compatibility.
4. **Client Delivery Package:**
   - Standalone dashboard, KPI metrics JSON, and documentation in `deliverables.zip`.

---

## Summary Matrix

| Service Package | Introductory Price | Turnaround | Primary Output | Target Buyer |
|---|---|---|---|---|
| **Business Analytics & Excel Pack** | **₹5,000** | 24–48h | Excel Model + Charts + HTML Report | E-Commerce, Retailers, Wholesalers |
| **CSV Reconciliation Automation** | **₹10,000** | 48h | Python Matcher Script + Excel Audit | Accountants, Marketplace Sellers |
| **Data Engineering Pipeline** | **₹15,000** | 3–4 days | Production ETL Script + Schema Manifest | Growing Operations Teams |
| **Executive Interactive Dashboard** | **₹20,000** | 4–5 days | Offline Zero-Dependency HTML5 Dashboard | Business Owners, Agency Heads |
