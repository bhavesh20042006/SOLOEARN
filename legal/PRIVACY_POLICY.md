# SOLOEARN — Privacy Policy

**Last Updated:** November 2026  
**Effective Date:** November 2026

---

## 1. Introduction

SOLOEARN ("we", "us", "our") is a B2B data analytics service operated by Bhavesh Khullar as a sole proprietorship. This Privacy Policy describes how we collect, use, store, and protect information when you use our data analytics, reconciliation, data engineering, and dashboard services.

## 2. Data We Collect

### 2.1 Client Business Data
- **Uploaded datasets**: CSV, Excel, and TSV files you provide for analysis
- **Business context**: Company name, project requirements, service tier selected
- **Communication records**: Outreach messages, proposals, and correspondence

### 2.2 Payment Information
- **UPI transaction references**: UTR numbers for payment verification
- **Invoice details**: Amounts, dates, and payment status

### 2.3 Information We Do NOT Collect
- Personal identification documents (Aadhaar, PAN card copies)
- Banking credentials (UPI PIN, MPIN, OTP, CVV, passwords)
- Personal browsing history or device fingerprints
- Biometric data
- Social media credentials

## 3. How We Use Your Data

| Purpose | Legal Basis |
|---|---|
| Perform contracted data analytics work | Contractual necessity |
| Generate deliverables (Excel, dashboards, reports) | Contractual necessity |
| Issue invoices and verify payments | Legitimate business interest |
| Quality assurance verification | Legitimate business interest |
| Improve service delivery processes | Legitimate business interest |

## 4. Data Storage & Security

### 4.1 Local-First Architecture
- All client data is stored on our **local systems only** in isolated workspaces
- Each client project uses a separate, path-protected workspace directory
- No client data is transmitted to cloud storage, third-party APIs, or external AI services

### 4.2 Security Measures
- **Path traversal protection**: Workspace isolation prevents cross-client data access
- **Formula injection guards**: CWE-1236 protection on all spreadsheet outputs
- **Credential rejection**: System fails closed if any sensitive banking credential is detected in input
- **Append-only audit logs**: All actions are durably logged and cannot be modified or deleted
- **HMAC-SHA256 verification**: Payment webhooks are cryptographically verified

### 4.3 AI Processing
- Local-only AI model (Ollama on localhost) is used for report observations only
- No client data is sent to external AI APIs (OpenAI, Anthropic, Google, etc.)
- AI-generated content is always reviewed before client delivery

## 5. Data Sharing

We do **not** share, sell, rent, or transfer your business data to any third party. Exceptions:
- When required by Indian law, court order, or regulatory authority
- With your explicit written consent

## 6. Data Retention & Deletion

- **Active projects**: Data retained during engagement and for 90 days after final delivery
- **Completed projects**: Deliverables archived for 1 year for warranty/revision purposes
- **Deletion requests**: You may request deletion of all your data at any time by contacting us
- **Backup data**: Local backups are rotated and old backups are permanently deleted

## 7. Your Rights

You have the right to:
- **Access**: Request a copy of all data we hold about your business
- **Rectification**: Correct any inaccurate data
- **Deletion**: Request permanent deletion of your data
- **Portability**: Receive your data in machine-readable format (CSV, JSON)
- **Object**: Object to any processing beyond contracted scope

## 8. Children's Privacy

Our services are exclusively B2B and not directed at individuals under 18 years of age.

## 9. Changes to This Policy

We may update this Privacy Policy periodically. Material changes will be communicated before they take effect.

## 10. Contact

For privacy inquiries, data access requests, or complaints:

- **Email**: [privacy@soloearn.in] *(to be configured)*
- **Business**: Bhavesh Khullar, Sole Proprietor
- **Jurisdiction**: India

---

*This privacy policy is a template and should be reviewed by a legal professional before commercial use.*
