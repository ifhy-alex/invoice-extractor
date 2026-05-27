"""
config.py
Shared configuration between extract_invoices.py, audit_invoices.py and dashboard.py.
"""

# ── Paths ──────────────────────────────────────────────────────────────────────
PDF_DIR        = r"C:\Users\alexf\OneDrive\Escritorio\invoices"
CSV_OUT        = r"C:\Users\alexf\OneDrive\Escritorio\invoices.csv"
DB_OUT         = r"C:\Users\alexf\OneDrive\Escritorio\invoices.db"
HTML_DASHBOARD = r"C:\Users\alexf\OneDrive\Escritorio\dashboard.html"
HTML_AUDIT     = r"C:\Users\alexf\OneDrive\Escritorio\audit_report.html"

# ── Fields ─────────────────────────────────────────────────────────────────────
ALL_FIELDS = [
    "filename", "carrier", "date", "invoice_no", "pro_number",
    "po_number", "bl_number", "biller", "accts_rec",
    "due_amount", "total_charges", "fuel_surcharge", "discount",
    "origin", "destination", "weight",
    "shipper_name", "consignee_name", "payment_terms", "payment_due_date",
    "charges_detail",
    "pages", "extraction_confidence", "error",
]

AUDIT_FIELDS = [
    "date", "invoice_no", "po_number", "bl_number",
    "accts_rec", "due_amount", "total_charges",
    "fuel_surcharge", "discount", "origin", "destination",
    "weight", "payment_due_date",
]

# ── Confidence per carrier ─────────────────────────────────────────────────────
CRITICAL_FIELDS = {
    "SAIA":       ["invoice_no", "due_amount", "origin", "destination", "date"],
    "DAYTON":     ["invoice_no", "due_amount", "origin", "date"],
    "AAA_COOPER": ["pro_number", "due_amount", "origin", "date"],
    "FEDEX":      ["invoice_no", "due_amount", "origin", "destination", "date"],
    "OTHER":      ["invoice_no", "due_amount"],
}
