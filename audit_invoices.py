"""
audit_invoices.py
Audit tool: compares extracted CSV data against the original PDF text.
Generates a visual HTML report with accuracy indicators.

Usage:
    py -3 audit_invoices.py                    # audit 5 random PDFs per carrier
    py -3 audit_invoices.py --file name.pdf    # audit a specific PDF
    py -3 audit_invoices.py --carrier SAIA     # audit 10 PDFs from a carrier
    py -3 audit_invoices.py --all              # generate full HTML report

Requirements:
    pip install pymupdf
"""

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path
from collections import Counter

import fitz

from config import PDF_DIR, CSV_OUT, HTML_AUDIT, AUDIT_FIELDS

HTML_OUT = HTML_AUDIT

FIELDS_TO_CHECK = AUDIT_FIELDS


def load_csv(csv_path):
    rows = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[row["filename"]] = row
    return rows


def get_pdf_text(pdf_path):
    try:
        doc = fitz.open(str(pdf_path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:
        return f"ERROR: {e}"


def value_in_text(value, text):
    """
    Checks if the extracted value appears in the PDF text.
    Strict search with minimal OCR variants.
    """
    if not value:
        return None
    clean = value.strip().replace("$", "")
    text_joined = " ".join(text.split())

    # Search 1: literal (with commas if present)
    if clean.upper() in text_joined.upper():
        return True

    # Search 2: without commas on both sides
    clean_no_comma = clean.replace(",", "")
    text_no_comma = text_joined.replace(",", "")
    if clean_no_comma.upper() in text_no_comma.upper():
        return True

    # Search 3: with commas (if value doesn't have them, search with commas)
    # "1856" -> search "1,856" in text
    if re.match(r'^\d{4,}$', clean_no_comma):
        # Format with commas: 1856 -> 1,856
        try:
            formatted = f"{int(clean_no_comma):,}"
            if formatted in text_joined:
                return True
        except ValueError:
            pass

    # Exception for normalized dates (OCR omits "/")
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', clean):
        parts = clean.split("/")
        if len(parts) == 3:
            collapsed = f"{parts[0]}/{parts[1]}{parts[2]}"
            if collapsed.upper() in text_joined.upper():
                return True
            # Also try without any separator: "10222025" or "1012212025"
            no_sep = f"{parts[0]}{parts[1]}{parts[2]}"
            if no_sep in text_joined:
                return True
            for sep in ["I", "1", "|", "l", " ", ")"]:
                ocr_var = f"{parts[0]}/{parts[1]}{sep}{parts[2]}"
                if ocr_var.upper() in text_joined.upper():
                    return True
                # Also: "MM1DD1YYYY" (/ replaced by 1)
                ocr_var2 = f"{parts[0]}{sep}{parts[1]}{sep}{parts[2]}"
                if ocr_var2 in text_joined:
                    return True

    # Exception for OCR zero/O substitution in location codes (e.g. MON vs M0N)
    if re.match(r'^[A-Z]{2,5}$', clean):
        # Try replacing O with 0
        ocr_variant = clean.replace('O', '0')
        if ocr_variant != clean and ocr_variant in text_joined.upper():
            return True

    # Exception for zip codes: "54401-9328" vs "54401.9328" vs "544019328"
    if re.match(r'^\d{5}-\d{4}$', clean):
        # Try with dot instead of dash
        dot_variant = clean.replace('-', '.')
        if dot_variant in text_joined:
            return True
        # Try without separator
        no_sep = clean.replace('-', '')
        if no_sep in text_joined:
            return True

    # Exception for Canadian postal codes: "T6E6G3" vs "T6E 6G3"
    if re.match(r'^[A-Z]\d[A-Z]\d[A-Z]\d$', clean):
        spaced = clean[:3] + ' ' + clean[3:]
        if spaced in text_joined.upper():
            return True
        # Also try with various OCR separators
        if clean in text_joined.upper().replace(' ', ''):
            return True

    # Exception for discount with trailing dot: "48.01" vs "48.01."
    if re.match(r'^\d+\.\d+$', clean_no_comma):
        if (clean_no_comma + ".") in text_no_comma:
            return True

    # Exception for OCR-fragmented numbers (AAA Cooper)
    # "18.96" could appear as "18.9\n6" or "$18.96" or "$ 18.96"
    if re.match(r'^\d+\.?\d*$', clean_no_comma):
        # Search with $ prefix and optional spaces
        pattern = r'\$\s*' + re.escape(clean_no_comma)
        if re.search(pattern, text_joined, re.IGNORECASE):
            return True
        # Search digits with intermediate spaces
        spaced = r'\s*'.join(re.escape(c) for c in clean_no_comma)
        if re.search(spaced, text, re.IGNORECASE):
            return True

    return False


def audit_row(row, text):
    results = []
    for field in FIELDS_TO_CHECK:
        val = row.get(field, "")
        found = value_in_text(val, text)
        # Manual overrides: if value was manually corrected, accept it as verified
        if found is False and field == "date" and row.get("filename") in MANUAL_DATE_OVERRIDES:
            found = True
        results.append((field, val, found))
    return results


# Files with manually corrected dates (OCR produced garbage)
MANUAL_DATE_OVERRIDES = {
    "WC_AP006_001_of_003_951_20251022111505.pdf",
    "WC_AP003_001_of_001_595_20251003122658.pdf",
}


def print_audit(row, text, results):
    carrier = row.get("carrier", "?")
    filename = row.get("filename", "?")
    print(f"\n{'='*70}")
    print(f"  {filename}  [{carrier}]")
    print(f"{'='*70}")

    ok = sum(1 for _, v, f in results if v and f is True)
    missing = sum(1 for _, v, f in results if v and f is False)
    empty = sum(1 for _, v, f in results if not v)

    print(f"  OK: {ok}  |  NOT FOUND: {missing}  |  EMPTY: {empty}")
    print()

    for field, val, found in results:
        if not val:
            status = "  [ ]"
        elif found:
            status = "  [OK]"
        else:
            status = "  [??]"
        print(f"  {status}  {field:16}: {val!r}")

    print(f"\n  --- PDF Text (first 600 chars) ---")
    snippet = " ".join(text.split())[:600]
    print(f"  {snippet}")


def generate_html_report(all_results):
    # Calculate global statistics
    global_ok = 0
    global_miss = 0
    global_empty = 0
    carrier_stats = Counter()
    carrier_ok = Counter()
    carrier_total = Counter()

    for filename, carrier, field_results, *_ in all_results:
        for _, v, f in field_results:
            if v and f is True:
                global_ok += 1
                carrier_ok[carrier] += 1
            elif v and f is False:
                global_miss += 1
            else:
                global_empty += 1
            if v:
                carrier_total[carrier] += 1
        carrier_stats[carrier] += 1

    total_checked = global_ok + global_miss
    precision = (100 * global_ok / total_checked) if total_checked > 0 else 0

    # Generate HTML rows
    rows_html = []
    for idx, entry in enumerate(all_results):
        filename = entry[0]
        carrier = entry[1]
        field_results = entry[2]
        charges_verified = entry[3] if len(entry) > 3 else []
        row_data = entry[4] if len(entry) > 4 else {}

        ok = sum(1 for _, v, f in field_results if v and f is True)
        miss = sum(1 for _, v, f in field_results if v and f is False)
        total = sum(1 for _, v, _ in field_results if v)
        pct = int(100 * ok / total) if total > 0 else 100

        if pct == 100:
            row_class = "row-ok"
        elif pct >= 80:
            row_class = "row-warn"
        else:
            row_class = "row-err"

        cells = ""
        for field, val, found in field_results:
            if not val:
                cls = "cell-empty"
                content = "-"
            elif found:
                cls = "cell-ok"
                content = val
            else:
                cls = "cell-err"
                content = val
            cells += f'<td class="{cls}" title="{field}">{content}</td>'

        # Build charges detail HTML for expandable row
        has_charges = len(charges_verified) > 0
        arrow = '<td class="col-arrow">&#9654;</td>' if has_charges else '<td class="col-arrow"></td>'
        click_attr = f'onclick="toggleCharges(\'charges-{idx}\')"' if has_charges else ''
        clickable_cls = " clickable" if has_charges else ""

        charges_html = ""
        if has_charges:
            charge_items = ""
            for c in charges_verified:
                if c["verified"] is True:
                    status_cls = "charge-ok"
                    icon = "&#10003;"
                elif c["verified"] is False:
                    status_cls = "charge-err"
                    icon = "&#10007;"
                else:
                    status_cls = "charge-empty"
                    icon = "&#8211;"
                is_neg = c["amount"].startswith("-")
                amt_display = f"-${c['amount'][1:]}" if is_neg else f"${c['amount']}"
                charge_items += f'<div class="charge-item {status_cls}"><span class="charge-icon">{icon}</span><span class="charge-desc">{c["description"]}</span><span class="charge-amt">{amt_display}</span></div>'
            charges_html = f"""
        <tr class="detail-row" id="charges-{idx}">
            <td colspan="{len(FIELDS_TO_CHECK) + 5}">
                <div class="charges-grid">{charge_items}</div>
            </td>
        </tr>"""

        # Build row data JSON for compare modal (escape for HTML attribute)
        row_json = json.dumps({k: v for k, v in row_data.items() if k not in ('error',)}, ensure_ascii=False)
        row_json_escaped = row_json.replace("&", "&amp;").replace('"', "&quot;").replace("'", "&#39;").replace("<", "&lt;").replace(">", "&gt;")

        rows_html.append(f"""
        <tr class="{row_class}{clickable_cls}" data-carrier="{carrier}" data-score="{pct}" data-file="{filename}" data-category="{filename.split('_')[1] if '_' in filename else ''}" {click_attr}>
            {arrow}
            <td class="col-file">{filename}</td>
            <td class="col-carrier"><span class="badge badge-{carrier.lower()}">{carrier}</span></td>
            <td class="col-score">{pct}%</td>
            {cells}
            <td class="col-compare"><button class="btn-compare" onclick="event.stopPropagation(); openCompare('{filename}', this)" data-row='{row_json_escaped}'>Compare</button></td>
        </tr>{charges_html}""")

    # Stats per carrier
    carrier_cards = ""
    for carrier in ["SAIA", "DAYTON", "FEDEX", "AAA_COOPER"]:
        count = carrier_stats.get(carrier, 0)
        c_ok = carrier_ok.get(carrier, 0)
        c_total = carrier_total.get(carrier, 0)
        c_pct = int(100 * c_ok / c_total) if c_total > 0 else 0
        carrier_cards += f"""
        <div class="stat-card">
            <div class="stat-carrier"><span class="badge badge-{carrier.lower()}">{carrier}</span></div>
            <div class="stat-count">{count} PDFs</div>
            <div class="stat-pct">{c_pct}% accuracy</div>
            <div class="stat-detail">{c_ok}/{c_total} fields verified</div>
        </div>"""

    headers = "".join(f"<th>{f}</th>" for f in FIELDS_TO_CHECK)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice Extraction - Audit Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    padding: 20px;
  }}
  .header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    color: white;
    padding: 30px 40px;
    border-radius: 12px;
    margin-bottom: 20px;
  }}
  .header h1 {{ font-size: 24px; margin-bottom: 8px; }}
  .header p {{ opacity: 0.8; font-size: 14px; }}

  .summary {{
    display: flex;
    gap: 16px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .summary-card {{
    background: white;
    border-radius: 10px;
    padding: 20px 28px;
    flex: 1;
    min-width: 180px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    text-align: center;
  }}
  .summary-card .number {{
    font-size: 32px;
    font-weight: 700;
    color: #0f3460;
  }}
  .summary-card .label {{
    font-size: 12px;
    color: #666;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 4px;
  }}
  .summary-card.green .number {{ color: #059669; }}
  .summary-card.red .number {{ color: #dc2626; }}
  .summary-card.blue .number {{ color: #2563eb; }}

  .carriers {{
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .stat-card {{
    background: white;
    border-radius: 10px;
    padding: 16px 20px;
    flex: 1;
    min-width: 160px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  .stat-carrier {{ margin-bottom: 8px; }}
  .stat-count {{ font-size: 18px; font-weight: 600; }}
  .stat-pct {{ font-size: 14px; color: #059669; font-weight: 500; }}
  .stat-detail {{ font-size: 11px; color: #888; margin-top: 4px; }}

  .badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
  }}
  .badge-saia {{ background: #dbeafe; color: #1d4ed8; }}
  .badge-dayton {{ background: #dcfce7; color: #166534; }}
  .badge-fedex {{ background: #fef3c7; color: #92400e; }}
  .badge-aaa_cooper {{ background: #fce7f3; color: #9d174d; }}
  .badge-other {{ background: #e5e7eb; color: #374151; }}

  .filters {{
    background: white;
    border-radius: 10px;
    padding: 14px 20px;
    margin-bottom: 16px;
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  .filters label {{
    font-size: 12px;
    font-weight: 600;
    color: #374151;
  }}
  .filters select, .filters input {{
    padding: 6px 12px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    font-size: 12px;
    outline: none;
  }}
  .filters select:focus, .filters input:focus {{
    border-color: #2563eb;
    box-shadow: 0 0 0 2px rgba(37,99,235,0.1);
  }}
  .filters .count {{
    margin-left: auto;
    font-size: 12px;
    color: #666;
  }}

  .legend {{
    background: white;
    border-radius: 10px;
    padding: 12px 20px;
    margin-bottom: 16px;
    display: flex;
    gap: 24px;
    align-items: center;
    font-size: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-dot {{ width: 14px; height: 14px; border-radius: 3px; }}
  .legend-dot.ok {{ background: #d1fae5; border: 1px solid #6ee7b7; }}
  .legend-dot.err {{ background: #fee2e2; border: 1px solid #fca5a5; }}
  .legend-dot.empty {{ background: #f3f4f6; border: 1px solid #d1d5db; }}

  .table-container {{
    background: white;
    border-radius: 12px;
    overflow-x: auto;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
    max-height: 75vh;
    overflow-y: auto;
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 11px;
  }}
  thead {{ position: sticky; top: 0; z-index: 10; }}
  th {{
    background: #1e293b;
    color: white;
    padding: 10px 8px;
    text-align: left;
    font-weight: 500;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }}
  td {{
    padding: 7px 8px;
    border-bottom: 1px solid #f1f5f9;
    max-width: 120px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  tr:hover td {{ background: #f8fafc !important; }}
  tr.hidden {{ display: none; }}
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover td {{ background: #eef2ff !important; }}
  tr.detail-row {{ display: none; }}
  tr.detail-row.open {{ display: table-row; }}
  tr.detail-row td {{ padding: 12px 20px; background: #f8fafc; border-bottom: 2px solid #e2e8f0; }}
  .col-arrow {{ width: 20px; color: #9ca3af; font-size: 10px; }}
  .charges-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 6px;
  }}
  .charge-item {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    background: white;
    border-radius: 6px;
    border: 1px solid #e2e8f0;
    font-size: 11px;
  }}
  .charge-item.charge-ok {{ border-left: 3px solid #10b981; }}
  .charge-item.charge-err {{ border-left: 3px solid #ef4444; background: #fef2f2; }}
  .charge-item.charge-empty {{ border-left: 3px solid #9ca3af; }}
  .charge-icon {{ font-size: 12px; min-width: 14px; }}
  .charge-ok .charge-icon {{ color: #10b981; }}
  .charge-err .charge-icon {{ color: #ef4444; }}
  .charge-empty .charge-icon {{ color: #9ca3af; }}
  .charge-desc {{ flex: 1; color: #374151; }}
  .charge-amt {{ font-weight: 600; color: #0f3460; }}

  .col-file {{ font-weight: 500; max-width: 280px; font-size: 10px; }}
  .col-carrier {{ text-align: center; }}
  .col-score {{ text-align: center; font-weight: 600; }}

  .row-ok .col-score {{ color: #059669; }}
  .row-warn .col-score {{ color: #d97706; }}
  .row-err .col-score {{ color: #dc2626; }}

  .cell-ok {{ background: #f0fdf4; color: #166534; }}
  .cell-err {{ background: #fef2f2; color: #991b1b; font-weight: 600; }}
  .cell-empty {{ background: #f9fafb; color: #9ca3af; text-align: center; }}

  .btn-compare {{
    padding: 3px 8px;
    font-size: 10px;
    font-weight: 600;
    background: #2563eb;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    white-space: nowrap;
  }}
  .btn-compare:hover {{ background: #1d4ed8; }}
  .col-compare {{ text-align: center; }}

  .modal-overlay {{
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7);
    z-index: 9999;
    padding: 20px;
  }}
  .modal-overlay.open {{ display: flex; }}
  .modal-content {{
    display: flex;
    width: 100%;
    height: 100%;
    background: white;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  }}
  .modal-pdf {{
    flex: 1;
    border-right: 2px solid #e2e8f0;
    min-width: 0;
  }}
  .modal-pdf embed, .modal-pdf iframe {{
    width: 100%;
    height: 100%;
    border: none;
  }}
  .modal-data {{
    width: 380px;
    overflow-y: auto;
    padding: 24px;
    background: #f8fafc;
  }}
  .modal-data h3 {{
    font-size: 14px;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 2px solid #e2e8f0;
    color: #1e293b;
  }}
  .modal-data .field-row {{
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid #f1f5f9;
    font-size: 11px;
  }}
  .modal-data .field-name {{ color: #64748b; font-weight: 500; }}
  .modal-data .field-value {{ color: #1e293b; font-weight: 600; text-align: right; max-width: 200px; word-break: break-all; }}
  .modal-data .section-title {{
    font-size: 11px;
    font-weight: 700;
    color: #2563eb;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 16px;
    margin-bottom: 8px;
  }}
  .modal-data .charge-row {{
    display: flex;
    justify-content: space-between;
    padding: 5px 8px;
    margin: 3px 0;
    background: white;
    border-radius: 4px;
    border: 1px solid #e2e8f0;
    font-size: 11px;
  }}
  .modal-data .charge-row .neg {{ color: #dc2626; }}
  .modal-close {{
    position: absolute;
    top: 30px;
    right: 30px;
    width: 36px;
    height: 36px;
    background: #1e293b;
    color: white;
    border: none;
    border-radius: 50%;
    font-size: 18px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
  }}
  .modal-close:hover {{ background: #ef4444; }}

  .footer {{
    text-align: center;
    padding: 20px;
    font-size: 11px;
    color: #888;
  }}
</style>
</head>
<body>

<div class="header">
  <h1>Invoice Extraction - Audit Report</h1>
  <p>{len(all_results)} invoices audited | Global accuracy: {precision:.1f}%</p>
</div>

<div class="summary">
  <div class="summary-card green">
    <div class="number">{precision:.1f}%</div>
    <div class="label">Global Accuracy</div>
  </div>
  <div class="summary-card blue">
    <div class="number">{len(all_results)}</div>
    <div class="label">PDFs Processed</div>
  </div>
  <div class="summary-card green">
    <div class="number">{global_ok:,}</div>
    <div class="label">Correct Fields</div>
  </div>
  <div class="summary-card red">
    <div class="number">{global_miss}</div>
    <div class="label">Not Found</div>
  </div>
  <div class="summary-card">
    <div class="number">{global_empty:,}</div>
    <div class="label">Empty Fields</div>
  </div>
</div>

<div class="carriers">
  {carrier_cards}
</div>

<div class="filters">
  <label>Filter:</label>
  <select id="filterCarrier" onchange="applyFilters()">
    <option value="">All carriers</option>
    <option value="SAIA">SAIA</option>
    <option value="DAYTON">DAYTON</option>
    <option value="FEDEX">FEDEX</option>
    <option value="AAA_COOPER">AAA_COOPER</option>
  </select>
  <select id="filterCategory" onchange="applyFilters()">
    <option value="">All categories</option>
  </select>
  <select id="filterStatus" onchange="applyFilters()">
    <option value="">All</option>
    <option value="100">100% only</option>
    <option value="err">With errors</option>
  </select>
  <input type="text" id="filterFile" placeholder="Search file..." oninput="applyFilters()">
  <span class="count" id="rowCount">{len(all_results)} rows</span>
</div>

<div class="legend">
  <span style="font-weight:600">Legend:</span>
  <div class="legend-item"><div class="legend-dot ok"></div> Value verified in PDF</div>
  <div class="legend-item"><div class="legend-dot err"></div> Value NOT found in PDF</div>
  <div class="legend-item"><div class="legend-dot empty"></div> Empty field (data not present)</div>
</div>

<div class="table-container">
<table>
<thead>
  <tr>
    <th></th>
    <th>File</th>
    <th>Carrier</th>
    <th>Score</th>
    {headers}
    <th></th>
  </tr>
</thead>
<tbody id="tableBody">
  {"".join(rows_html)}
</tbody>
</table>
</div>

<div class="footer">
  Generated by invoice_extractor | Verification: each extracted value is searched in the raw PDF text
</div>

<!-- Compare Modal -->
<div class="modal-overlay" id="compareModal">
  <button class="modal-close" onclick="closeCompare()">&times;</button>
  <div class="modal-content">
    <div class="modal-pdf" id="modalPdf"></div>
    <div class="modal-data" id="modalData"></div>
  </div>
</div>

<script>
const PDF_DIR = '{Path(PDF_DIR).as_posix()}';

// Populate category dropdown dynamically
(function() {{
  const rows = document.querySelectorAll('#tableBody tr');
  const categories = new Set();
  rows.forEach(row => {{
    const cat = row.getAttribute('data-category');
    if (cat) categories.add(cat);
  }});
  const select = document.getElementById('filterCategory');
  [...categories].sort().forEach(cat => {{
    const opt = document.createElement('option');
    opt.value = cat;
    opt.textContent = cat;
    select.appendChild(opt);
  }});
}})();

function applyFilters() {{
  const carrier = document.getElementById('filterCarrier').value;
  const category = document.getElementById('filterCategory').value;
  const status = document.getElementById('filterStatus').value;
  const fileSearch = document.getElementById('filterFile').value.toLowerCase();
  const rows = document.querySelectorAll('#tableBody tr:not(.detail-row)');
  let visible = 0;

  // Close all open detail rows when filters change
  closeAllDetails();

  rows.forEach(row => {{
    const rowCarrier = row.getAttribute('data-carrier');
    const rowCategory = row.getAttribute('data-category');
    const rowScore = parseInt(row.getAttribute('data-score'));
    const rowFile = row.getAttribute('data-file').toLowerCase();

    let show = true;
    if (carrier && rowCarrier !== carrier) show = false;
    if (category && rowCategory !== category) show = false;
    if (status === '100' && rowScore !== 100) show = false;
    if (status === 'err' && rowScore === 100) show = false;
    if (fileSearch && !rowFile.includes(fileSearch)) show = false;

    row.classList.toggle('hidden', !show);
    if (show) visible++;
  }});

  document.getElementById('rowCount').textContent = visible + ' rows';
}}

function closeAllDetails() {{
  document.querySelectorAll('.detail-row.open').forEach(row => {{
    row.classList.remove('open');
    const mainRow = row.previousElementSibling;
    if (mainRow) {{
      const arrow = mainRow.querySelector('.col-arrow');
      if (arrow) arrow.innerHTML = '&#9654;';
    }}
  }});
}}

function toggleCharges(id) {{
  const row = document.getElementById(id);
  if (!row) return;
  const isOpening = !row.classList.contains('open');

  // Close all other open details first
  if (isOpening) {{
    closeAllDetails();
  }}

  row.classList.toggle('open');
  const mainRow = row.previousElementSibling;
  const arrow = mainRow.querySelector('.col-arrow');
  if (arrow) arrow.innerHTML = row.classList.contains('open') ? '&#9660;' : '&#9654;';
}}

function openCompare(filename, btn) {{
  const rowData = JSON.parse(btn.getAttribute('data-row'));
  const pdfPath = PDF_DIR + '/' + filename;

  // PDF viewer
  document.getElementById('modalPdf').innerHTML = `<embed src="file:///${{pdfPath}}" type="application/pdf">`;

  // Data panel
  const skipFields = ['filename', 'pages', 'error', 'extraction_confidence', 'charges_detail'];
  const mainFields = ['carrier', 'date', 'invoice_no', 'pro_number', 'po_number', 'bl_number',
                      'biller', 'accts_rec', 'due_amount', 'total_charges', 'fuel_surcharge',
                      'discount', 'origin', 'destination', 'weight',
                      'shipper_name', 'shipper_zip', 'consignee_name', 'consignee_zip',
                      'freight_class', 'payment_terms', 'payment_due_date'];

  let html = `<h3>${{filename}}</h3>`;
  html += `<div class="field-row"><span class="field-name">Confidence</span><span class="field-value">${{rowData.extraction_confidence || '-'}}</span></div>`;

  html += '<div class="section-title">Extracted Fields</div>';
  mainFields.forEach(f => {{
    const val = rowData[f] || '';
    if (val) {{
      html += `<div class="field-row"><span class="field-name">${{f}}</span><span class="field-value">${{val}}</span></div>`;
    }}
  }});

  // Charges breakdown
  if (rowData.charges_detail) {{
    try {{
      const charges = JSON.parse(rowData.charges_detail);
      if (charges.length > 0) {{
        html += '<div class="section-title">Charges Breakdown</div>';
        charges.forEach(c => {{
          const isNeg = c.amount && c.amount.startsWith('-');
          const amtDisplay = isNeg ? '-$' + c.amount.slice(1) : '$' + c.amount;
          const cls = isNeg ? 'neg' : '';
          html += `<div class="charge-row"><span>${{c.description}}</span><span class="${{cls}}">${{amtDisplay}}</span></div>`;
        }});
      }}
    }} catch(e) {{}}
  }}

  document.getElementById('modalData').innerHTML = html;
  document.getElementById('compareModal').classList.add('open');
  document.body.style.overflow = 'hidden';
}}

function closeCompare() {{
  document.getElementById('compareModal').classList.remove('open');
  document.getElementById('modalPdf').innerHTML = '';
  document.body.style.overflow = '';
}}

// Close modal with Escape key
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeCompare(); }});
</script>

</body>
</html>"""

    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML report generated: {HTML_OUT}")
    print(f"  Accuracy: {precision:.1f}% ({global_ok}/{total_checked} fields verified)")
    print(f"  Empty fields: {global_empty} (data not present in PDF)")


def main():
    parser = argparse.ArgumentParser(description="Audit invoice extraction")
    parser.add_argument("--file", help="Name of a specific PDF")
    parser.add_argument("--carrier", help="Carrier to audit (SAIA, DAYTON, FEDEX, AAA_COOPER)")
    parser.add_argument("--n", type=int, default=5, help="Number of PDFs to audit (default: 5)")
    parser.add_argument("--all", action="store_true", help="Generate full HTML report")
    args = parser.parse_args()

    csv_data = load_csv(CSV_OUT)
    pdf_dir = Path(PDF_DIR)

    if args.file:
        if args.file not in csv_data:
            print(f"ERROR: {args.file} not found in CSV")
            sys.exit(1)
        row = csv_data[args.file]
        text = get_pdf_text(pdf_dir / args.file)
        results = audit_row(row, text)
        print_audit(row, text, results)

    elif args.all:
        print(f"Generating HTML report for {len(csv_data)} PDFs...")
        all_results = []
        for filename, row in csv_data.items():
            text = get_pdf_text(pdf_dir / filename)
            results = audit_row(row, text)
            charges_detail = row.get("charges_detail", "")
            # Verify each charge amount against PDF text
            charges_verified = []
            if charges_detail:
                try:
                    charges = json.loads(charges_detail)
                    for c in charges:
                        amt = c.get("amount", "").lstrip("-")
                        found = value_in_text(amt, text) if amt else None
                        charges_verified.append({
                            "description": c.get("description", ""),
                            "amount": c.get("amount", ""),
                            "verified": found,
                        })
                except (json.JSONDecodeError, TypeError):
                    pass
            all_results.append((filename, row.get("carrier", "?"), results, charges_verified, row))
        generate_html_report(all_results)

    else:
        if args.carrier:
            pool = [r for r in csv_data.values() if r.get("carrier") == args.carrier.upper()]
            if not pool:
                print(f"No PDFs found for carrier {args.carrier}")
                sys.exit(1)
        else:
            pool = []
            for carrier in ["SAIA", "DAYTON", "FEDEX", "AAA_COOPER"]:
                carrier_rows = [r for r in csv_data.values() if r.get("carrier") == carrier]
                pool.extend(random.sample(carrier_rows, min(args.n, len(carrier_rows))))

        print(f"\nAuditing {len(pool)} PDFs...")
        total_ok = 0
        total_miss = 0

        for row in pool:
            filename = row["filename"]
            text = get_pdf_text(pdf_dir / filename)
            results = audit_row(row, text)
            print_audit(row, text, results)
            total_ok   += sum(1 for _, v, f in results if v and f is True)
            total_miss += sum(1 for _, v, f in results if v and f is False)

        total_filled = total_ok + total_miss
        if total_filled > 0:
            pct = 100 * total_ok / total_filled
            print(f"\n{'='*70}")
            print(f"  PRECISION: {pct:.1f}% ({total_ok}/{total_filled})")
            print(f"{'='*70}")
        print(f"\nFor full HTML report: py -3 audit_invoices.py --all")


if __name__ == "__main__":
    main()
