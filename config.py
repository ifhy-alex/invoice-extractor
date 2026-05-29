"""
config.py
Shared configuration between extract_invoices.py, audit_invoices.py and dashboard.py.

Paths can be overridden via environment variables:
    INVOICE_PDF_DIR=C:/path/to/pdfs
    INVOICE_OUTPUT_DIR=C:/path/to/output
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
# Default: 'invoices/' folder next to the project, output in 'output/' folder
_PROJECT_DIR = Path(__file__).parent
_PDF_DIR     = os.environ.get("INVOICE_PDF_DIR", str(_PROJECT_DIR / "invoices"))
_OUTPUT_DIR  = os.environ.get("INVOICE_OUTPUT_DIR", str(_PROJECT_DIR / "output"))

# Create output dir if it doesn't exist
Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

PDF_DIR        = _PDF_DIR
CSV_OUT        = str(Path(_OUTPUT_DIR) / "invoices.csv")
CSV_CHARGES    = str(Path(_OUTPUT_DIR) / "invoices_charges.csv")
JSON_OUT       = str(Path(_OUTPUT_DIR) / "invoices.json")
DB_OUT         = str(Path(_OUTPUT_DIR) / "invoices.db")
HTML_DASHBOARD = str(Path(_OUTPUT_DIR) / "dashboard.html")
HTML_AUDIT     = str(Path(_OUTPUT_DIR) / "audit_report.html")

# ── Fields ─────────────────────────────────────────────────────────────────────
ALL_FIELDS = [
    "filename", "carrier", "date", "invoice_no", "pro_number",
    "po_number", "bl_number", "biller", "accts_rec",
    "due_amount", "total_charges", "fuel_surcharge", "discount",
    "origin", "destination", "weight",
    "shipper_name", "shipper_zip", "consignee_name", "consignee_zip",
    "freight_class",
    "payment_terms", "payment_due_date",
    "charges_detail",
    "pages", "extraction_confidence", "error",
]

AUDIT_FIELDS = [
    "date", "invoice_no", "po_number", "bl_number",
    "accts_rec", "due_amount", "total_charges",
    "fuel_surcharge", "discount", "origin", "destination",
    "weight", "shipper_zip", "consignee_zip", "freight_class",
    "payment_due_date",
]

# ── Confidence per carrier ─────────────────────────────────────────────────────
CRITICAL_FIELDS = {
    "SAIA":       ["invoice_no", "due_amount", "origin", "destination", "date"],
    "DAYTON":     ["invoice_no", "due_amount", "origin", "date"],
    "AAA_COOPER": ["pro_number", "due_amount", "origin", "date"],
    "FEDEX":      ["invoice_no", "due_amount", "origin", "destination", "date"],
    "OTHER":      ["invoice_no", "due_amount"],
}
