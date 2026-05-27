# Invoice Extractor

Automated freight invoice data extraction from PDF. Processes ~950 invoices in ~11 seconds using PyMuPDF and carrier-specific regex patterns. No AI, no API costs.

**100% verified extraction accuracy** across 948 invoices from 4 carriers. All PDFs at HIGH confidence.

---

## Supported Carriers

| Carrier | Fields Extracted |
|---------|-----------------|
| **SAIA Motor Freight** | date, invoice_no, po_number, bl_number, biller, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight, charges breakdown |
| **Dayton Freight** | date, invoice_no, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight, payment_terms, payment_due_date, charges breakdown |
| **FedEx Freight** | date (invoice date), invoice_no, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, discount, origin, destination, weight, payment_terms, payment_due_date, charges breakdown |
| **AAA Cooper** | date, pro_number, po_number, bl_number, accts_rec, due_amount, total_charges, fuel_surcharge, origin, destination, weight, payment_terms, payment_due_date, charges breakdown |

### Charges Breakdown (per invoice)

Each invoice includes a detailed JSON breakdown of individual charges:

- **FedEx**: Base freight, Deficit WT, California Compliance, Demand Surcharge, Fuel Surcharge %, Less Discount, Earned Discount
- **SAIA**: Fuel Surcharge (FS), Single Shipment Charge (SS), Liftgate (LGATE), Residential (RESDEL), Discount
- **Dayton**: Base freight (with weight/rate), Fuel Surcharge % (FS), Charges Subject to Discount, Discount (with factor), Other Charges
- **AAA Cooper**: Base freight, Discount %, Fuel Surcharge %, State Compliance (by state), Liftgate

---

## Requirements

```
pip install pymupdf tqdm
```

Python 3.8+ required.

---

## Project Structure

```
pdf-project/
├── invoices/              ← your PDF files go here (not tracked by git)
├── output/                ← all generated files (not tracked by git)
│   ├── invoices.csv
│   ├── invoices_charges.csv
│   ├── invoices.json
│   ├── invoices.db
│   ├── dashboard.html
│   └── audit_report.html
├── config.py              → Paths (auto-configured, override via env vars)
├── extract_invoices.py    → PDF → text → regex → CSV + JSON + SQLite
├── audit_invoices.py      → Verifies extracted data against source PDFs
├── dashboard.py           → Generates interactive HTML dashboard
├── validate_invoices.py   → Logical/contextual data validation
└── json_to_db.py          → Alternative JSON-to-SQLite importer (optional)
```

---

## How to Use (Step by Step)

### Step 1: Install dependencies

```cmd
pip install pymupdf tqdm
```

### Step 2: Set up your PDF folder

Place your invoice PDFs in a folder called `invoices/` next to the project:

```
pdf-project/
├── invoices/          ← put your PDFs here
├── output/            ← generated automatically
├── extract_invoices.py
├── config.py
└── ...
```

Or set custom paths via environment variables:

```cmd
:: Windows
set INVOICE_PDF_DIR=C:\path\to\your\pdfs
set INVOICE_OUTPUT_DIR=C:\path\to\output

:: Linux/Mac
export INVOICE_PDF_DIR=/path/to/your/pdfs
export INVOICE_OUTPUT_DIR=/path/to/output
```

### Step 3: Run extraction

```cmd
# First run — processes all PDFs
py -3 extract_invoices.py --force

# Subsequent runs — only processes new PDFs (incremental)
py -3 extract_invoices.py
```

Output:
- `invoices.csv` — one row per invoice with all extracted fields
- `invoices.db` — SQLite database with indexes and duplicate detection

### Step 4: Generate dashboard

```cmd
py -3 dashboard.py
```

Open `dashboard.html` in your browser. Features:
- KPIs: Total billed, count, average, total fuel, total weight, discounts, routes, max invoice
- Charts: by carrier, by origin/destination, weight distribution, timeline, fuel by carrier, cost per pound
- Filters: carrier, category, origin, destination
- Expandable charge breakdown in Top 10 table

### Step 5: Generate audit report

```cmd
py -3 audit_invoices.py --all
```

Open `audit_report.html` in your browser. Features:
- Field-level accuracy verification (each value searched in source PDF)
- Expandable charge verification (click ▶ to see each charge with ✓/✗ status)
- Compare mode (click "Compare" to see PDF + extracted data side by side)
- Filters by carrier, category, score, filename
- Details auto-close when opening another or when filtering

### Step 6 (optional): Logical validation

```cmd
# Summary of issues
py -3 validate_invoices.py

# Details of files with problems
py -3 validate_invoices.py --fix
```

Validates logical consistency: date formats, valid terminal codes, reasonable amounts, charges that aren't weights in disguise, etc.

---

## Full Pipeline (single command)

```cmd
py -3 extract_invoices.py --force & py -3 dashboard.py & py -3 audit_invoices.py --all
```

This will:
1. Extract all PDFs (~11 seconds)
2. Generate the interactive dashboard
3. Generate the full audit report (~3 minutes)

---

## How It Works

### Extraction Pipeline

1. **PDF → Text**: PyMuPDF extracts digital text from each page
2. **Carrier Detection**: Keywords identify the carrier
3. **Field Extraction**: Carrier-specific regex patterns pull structured data
4. **Coordinate Extraction** (SAIA): Uses x/y positions to map data to columns relative to the header row
5. **Charge Parsing**: Extracts all individual line item charges per invoice
6. **Range Validation**: Rejects values outside reasonable ranges
7. **Confidence Scoring**: HIGH/MEDIUM/LOW based on critical fields extracted per carrier
8. **Output**: CSV + SQLite with indexes and unique constraints

### Design Decisions

- **No AI/ML** — Deterministic regex extraction. Same input always produces same output.
- **Multi-page reading** — Reads all pages (fuel surcharge and discount often on page 2)
- **Invoice Date for FedEx** — Extracts the second date (invoice date), not the ship date
- **Incremental processing** — Only processes new PDFs unless `--force` is used
- **Range validation** — Rejects amounts > $99,999, fuel > $5,000, weight > 50,000 lbs
- **OCR tolerance** — Handles corrupted date separators, 0/O substitution, fragmented numbers

---

## Current Results

| Metric | Value |
|--------|-------|
| PDFs processed | 948 |
| HIGH confidence | 948 (100%) |
| MEDIUM confidence | 0 |
| LOW confidence | 0 |
| Audit accuracy | 100.0% (11,209/11,209 fields) |
| Extraction time | ~11 seconds |

---

## Audit Commands

```cmd
# Full HTML report
py -3 audit_invoices.py --all

# Quick sample (5 random per carrier)
py -3 audit_invoices.py

# Specific PDF
py -3 audit_invoices.py --file WC_AP001_001_of_001_1011_20251024114802.pdf

# Specific carrier
py -3 audit_invoices.py --carrier FEDEX --n 10
```

---

## Sample SQL Queries

```sql
-- Total billed per carrier
SELECT carrier, COUNT(*) as invoices,
       ROUND(SUM(CAST(due_amount AS REAL)), 2) as total
FROM invoices
GROUP BY carrier;

-- All charges for a specific invoice
SELECT description, amount, code
FROM invoice_charges
WHERE filename = 'WC_AP001_001_of_001_1011_20251024114802.pdf';

-- Total fuel surcharge by carrier
SELECT carrier, COUNT(*) as items, ROUND(SUM(amount), 2) as total_fuel
FROM invoice_charges
WHERE description LIKE '%FUEL%'
GROUP BY carrier;

-- FedEx invoices with California Compliance
SELECT filename, due_amount, charges_detail
FROM invoices
WHERE carrier = 'FEDEX' AND charges_detail LIKE '%CALIFORNIA%';

-- Average fuel surcharge by carrier
SELECT carrier, ROUND(AVG(CAST(fuel_surcharge AS REAL)), 2) as avg_fuel
FROM invoices
WHERE fuel_surcharge != ''
GROUP BY carrier;

-- Search by PO number
SELECT * FROM invoices WHERE po_number = '4500783483';

-- Top 10 most expensive invoices
SELECT filename, carrier, due_amount, origin, destination
FROM invoices
ORDER BY CAST(due_amount AS REAL) DESC
LIMIT 10;

-- Most frequent routes
SELECT origin || ' → ' || destination as route, COUNT(*) as count
FROM invoices
WHERE origin != '' AND destination != ''
GROUP BY route
ORDER BY count DESC
LIMIT 10;

-- Charge type breakdown (from invoice_charges table)
SELECT description, COUNT(*) as occurrences, ROUND(SUM(amount), 2) as total
FROM invoice_charges
GROUP BY description
ORDER BY occurrences DESC;
```

---

## Output Files

| File | Description | How to Open |
|------|-------------|-------------|
| `invoices.json` | Full JSON with charges as nested arrays | Any text editor, VS Code, or programmatically |
| `invoices.csv` | Flat CSV, one row per invoice, 24 columns (charges as JSON string) | Double-click → Excel |
| `invoices_charges.csv` | Flat CSV, one row per charge line item | Double-click → Excel |
| `invoices.db` | SQLite with tables: `invoices` + `invoice_charges` | [DB Browser for SQLite](https://sqlitebrowser.org/) or Python |
| `dashboard.html` | Interactive Chart.js dashboard | Double-click → opens in browser |
| `audit_report.html` | Verification report with PDF compare mode | Double-click → opens in browser |

To open from the terminal:

```cmd
start output\dashboard.html
start output\audit_report.html
```

### Charges Table Schema (SQLite & CSV)

| Column | Description |
|--------|-------------|
| filename | Source PDF filename |
| carrier | SAIA, DAYTON, FEDEX, AAA_COOPER |
| invoice_no | Invoice/PRO number |
| date | Invoice date |
| description | Charge description (e.g. "FUEL SURCHARGE 7.25%") |
| amount | Dollar amount (negative for discounts) |
| code | Charge code if available (e.g. "FS") |
| weight | Weight if part of the charge line |
| rate | Rate if part of the charge line |

---

## Notes

- PDFs must contain extractable digital text (not scanned images)
- The PDF viewer in audit compare mode works best in Firefox/Edge. Chrome may block local file access.
- `.gitignore` excludes generated files (CSV, DB, HTML) — only source code is tracked
- To add a new carrier: create an `extract_new_carrier(text)` function and add it to the `EXTRACTORS` dict
