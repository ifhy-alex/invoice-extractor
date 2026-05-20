# Invoice Extractor

Extracts data from freight invoices in PDF format and saves to CSV and SQLite.

**No AI. No models. ~1000 PDFs in ~10 seconds.**

## Supported Carriers

| Carrier | Extracted Fields |
|---------|-----------------|
| SAIA Motor Freight | date, invoice_no, po_number, bl_number, biller, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight |
| Dayton Freight | date, invoice_no, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight |
| AAA Cooper | date, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, origin, destination, weight |
| FedEx Freight | date, invoice_no, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight |

## Requirements

```
pip install pymupdf tqdm
```

## Files

| File | Description |
|------|-------------|
| `config.py` | Shared configuration (paths, fields, constants) |
| `extract_invoices.py` | Main script — extracts data and generates CSV + SQLite |
| `audit_invoices.py` | Audit tool — verifies extracted data against original PDFs |
| `dashboard.py` | Generates an interactive HTML dashboard with charts |
| `json_to_db.py` | Alternative for importing JSONs to SQLite (not needed in normal flow) |

## Quick Start

### 1. Extract all PDFs

Edit paths in `config.py`:

```python
PDF_DIR = r"C:\path\to\your\pdfs"
CSV_OUT = r"C:\path\output\invoices.csv"
DB_OUT  = r"C:\path\output\invoices.db"
```

Then run:

```cmd
py -3 extract_invoices.py --force
```

Output:
- `invoices.csv` — one row per invoice
- `invoices.db` — SQLite database with indexes

Incremental mode (only processes new PDFs):

```cmd
py -3 extract_invoices.py
```

### 2. Audit results

```cmd
# Random sample (5 per carrier)
py -3 audit_invoices.py

# A specific PDF
py -3 audit_invoices.py --file name.pdf

# Only one carrier
py -3 audit_invoices.py --carrier SAIA --n 10

# Full HTML report (all PDFs)
py -3 audit_invoices.py --all
```

### 3. Generate dashboard

```cmd
py -3 dashboard.py
```

Opens `dashboard.html` in your browser with interactive charts and filters.

## Extraction Confidence

Each invoice gets a confidence score (HIGH / MEDIUM / LOW) based on how many critical fields were successfully extracted per carrier. Use the confidence filter in the dashboard to identify invoices that may need manual review.

## Sample SQL Queries

```sql
-- SAIA invoices with amount > $300
SELECT filename, date, po_number, bl_number, due_amount, origin, destination
FROM invoices
WHERE carrier = 'SAIA' AND CAST(due_amount AS REAL) > 300
ORDER BY date;

-- Total billed per carrier
SELECT carrier, COUNT(*) as invoices, SUM(CAST(due_amount AS REAL)) as total
FROM invoices
GROUP BY carrier;

-- Search by PO number
SELECT * FROM invoices WHERE po_number = '4500783483';

-- Low confidence invoices for manual review
SELECT filename, carrier, due_amount
FROM invoices
WHERE extraction_confidence = 'LOW';
```

## How It Works

Freight invoice PDFs contain extractable digital text (not scans). PyMuPDF extracts text in milliseconds and carrier-specific regex patterns identify each field. No OCR or AI models are used.

For SAIA invoices, coordinate-based extraction (using PyMuPDF's `get_text("dict")`) maps data to columns by their x/y position relative to the header row, making it robust against column shifts.

## Architecture

```
config.py              → Shared paths, fields, constants
extract_invoices.py    → PDF → text → regex → CSV + SQLite
audit_invoices.py      → CSV values vs PDF text verification
dashboard.py           → CSV → interactive HTML with Chart.js
```
