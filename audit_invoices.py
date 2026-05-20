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
            for sep in ["I", "1", "|", "l", " "]:
                ocr_var = f"{parts[0]}/{parts[1]}{sep}{parts[2]}"
                if ocr_var.upper() in text_joined.upper():
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
        results.append((field, val, found))
    return results


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

    for filename, carrier, field_results in all_results:
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
    for filename, carrier, field_results in all_results:
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

        rows_html.append(f"""
        <tr class="{row_class}" data-carrier="{carrier}" data-score="{pct}" data-file="{filename}" data-category="{filename.split('_')[1] if '_' in filename else ''}">
            <td class="col-file">{filename}</td>
            <td class="col-carrier"><span class="badge badge-{carrier.lower()}">{carrier}</span></td>
            <td class="col-score">{pct}%</td>
            {cells}
        </tr>""")

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

  .col-file {{ font-weight: 500; max-width: 280px; font-size: 10px; }}
  .col-carrier {{ text-align: center; }}
  .col-score {{ text-align: center; font-weight: 600; }}

  .row-ok .col-score {{ color: #059669; }}
  .row-warn .col-score {{ color: #d97706; }}
  .row-err .col-score {{ color: #dc2626; }}

  .cell-ok {{ background: #f0fdf4; color: #166534; }}
  .cell-err {{ background: #fef2f2; color: #991b1b; font-weight: 600; }}
  .cell-empty {{ background: #f9fafb; color: #9ca3af; text-align: center; }}

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
    <th>File</th>
    <th>Carrier</th>
    <th>Score</th>
    {headers}
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

<script>
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
  const rows = document.querySelectorAll('#tableBody tr');
  let visible = 0;

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
            all_results.append((filename, row.get("carrier", "?"), results))
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
