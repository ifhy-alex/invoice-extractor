"""
validate_invoices.py
Smart validation: checks extracted data for logical consistency and contextual correctness.
Unlike the audit (which only checks if a value exists in the PDF), this validates:
- Values are in the correct context (not just present anywhere)
- Amounts are reasonable for their field
- Origin/destination are valid terminal codes (2-4 uppercase letters)
- Dates are valid
- Pro numbers match expected formats per carrier
- Charges don't contain weights or other non-charge values

Usage:
    py -3 validate_invoices.py          # validate all
    py -3 validate_invoices.py --fix    # show what would need manual review
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from collections import Counter

import fitz

from config import PDF_DIR, CSV_OUT


def validate_row(row, text):
    """Validate a single row for logical/contextual correctness. Returns list of issues."""
    issues = []
    carrier = row.get('carrier', '')
    
    # 1. Date validation
    date = row.get('date', '')
    if date:
        # Must be MM/DD/YY or MM/DD/YYYY
        if not re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', date):
            issues.append(('date', f"Invalid format: '{date}'"))
        else:
            parts = date.split('/')
            m, d = int(parts[0]), int(parts[1])
            if m < 1 or m > 12:
                issues.append(('date', f"Invalid month: {m}"))
            if d < 1 or d > 31:
                issues.append(('date', f"Invalid day: {d}"))
    
    # 2. Origin/Destination validation
    origin = row.get('origin', '')
    destination = row.get('destination', '')
    if origin and not re.match(r'^[A-Z]{2,5}$', origin):
        issues.append(('origin', f"Not a valid terminal code: '{origin}'"))
    if destination and not re.match(r'^[A-Z]{2,5}$', destination):
        issues.append(('destination', f"Not a valid terminal code: '{destination}'"))
    
    # 3. Amount validation
    for field in ['due_amount', 'total_charges', 'fuel_surcharge', 'discount']:
        val = row.get(field, '')
        if val:
            try:
                n = float(val.replace(',', '').replace('$', ''))
                if field == 'fuel_surcharge' and n > 500:
                    issues.append((field, f"Suspiciously high: ${val}"))
                if field in ('due_amount', 'total_charges') and n > 50000:
                    issues.append((field, f"Suspiciously high: ${val}"))
            except ValueError:
                issues.append((field, f"Not a valid number: '{val}'"))
    
    # 4. Weight validation
    weight = row.get('weight', '')
    if weight:
        try:
            w = int(weight.replace(',', ''))
            if w > 50000:
                issues.append(('weight', f"Suspiciously high: {weight}"))
        except ValueError:
            issues.append(('weight', f"Not a valid number: '{weight}'"))
    
    # 5. Pro number / Invoice number format per carrier
    inv = row.get('invoice_no', '')
    if carrier == 'SAIA' and inv:
        if not re.match(r'^\d{10,12}$', inv):
            issues.append(('invoice_no', f"SAIA expects 10-12 digits: '{inv}'"))
    elif carrier == 'FEDEX' and inv:
        if not re.match(r'^\d{7,12}$', inv):
            issues.append(('invoice_no', f"FedEx expects 7-12 digits: '{inv}'"))
    elif carrier == 'DAYTON' and inv:
        if not re.match(r'^\d{6,12}$', inv):
            issues.append(('invoice_no', f"Dayton expects 6-12 digits: '{inv}'"))
    elif carrier == 'AAA_COOPER' and inv:
        if not re.match(r'^\d{7,9}$', inv):
            issues.append(('invoice_no', f"AAA Cooper expects 7-9 digits: '{inv}'"))
    
    # 6. Charges validation
    charges_str = row.get('charges_detail', '')
    if charges_str:
        try:
            charges = json.loads(charges_str)
            for c in charges:
                amt = c.get('amount', '').lstrip('-')
                desc = c.get('description', '')
                try:
                    n = float(amt.replace(',', ''))
                    # Weight disguised as charge (X.XXX format = thousands)
                    if '.' in amt and len(amt.split('.')[1]) == 3 and n > 1000:
                        issues.append(('charges', f"Possible weight as charge: {desc} ${amt}"))
                    # Charge > total amount
                    total = row.get('due_amount', '') or row.get('total_charges', '')
                    if total:
                        try:
                            t = float(total.replace(',', ''))
                            if n > t * 5 and n > 1000:
                                issues.append(('charges', f"Charge > 5x total: {desc} ${amt} (total=${total})"))
                        except ValueError:
                            pass
                except ValueError:
                    issues.append(('charges', f"Invalid amount: {desc} '{amt}'"))
        except (json.JSONDecodeError, TypeError):
            issues.append(('charges', f"Invalid JSON"))
    
    # 7. Context validation: origin should appear near "Origin" keyword
    # Only for carriers that use "ORIGIN" keyword (AAA Cooper)
    if origin and len(origin) <= 4 and carrier == 'AAA_COOPER':
        origin_context = re.search(r'(?:ORIGIN|Origin)[^\n]{0,50}' + re.escape(origin), text)
        stmt_context = re.search(r'-' + re.escape(origin) + r'-', text)
        if not origin_context and not stmt_context:
            issues.append(('origin', f"'{origin}' not found near 'ORIGIN' keyword"))
    
    # 8. Payment due date validation (FedEx)
    pdd = row.get('payment_due_date', '')
    if pdd and date:
        # Payment due should be after invoice date
        try:
            from datetime import datetime
            fmt = '%m/%d/%Y' if len(pdd.split('/')[-1]) == 4 else '%m/%d/%y'
            fmt_d = '%m/%d/%Y' if len(date.split('/')[-1]) == 4 else '%m/%d/%y'
            d_pdd = datetime.strptime(pdd, fmt)
            d_date = datetime.strptime(date, fmt_d)
            diff = (d_pdd - d_date).days
            if diff < 0:
                issues.append(('payment_due_date', f"Due date before invoice date: {pdd} < {date}"))
            elif diff > 90:
                issues.append(('payment_due_date', f"Due date >90 days after invoice: {diff} days"))
        except (ValueError, IndexError):
            pass
    
    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate extracted invoice data")
    parser.add_argument("--fix", action="store_true", help="Show details for manual review")
    args = parser.parse_args()

    with open(CSV_OUT, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pdf_dir = Path(PDF_DIR)
    
    print(f"Validating {len(rows)} invoices...")
    
    all_issues = []
    issue_counter = Counter()
    carrier_issues = Counter()
    
    for row in rows:
        fname = row['filename']
        pdf_path = pdf_dir / fname
        
        if pdf_path.exists():
            doc = fitz.open(str(pdf_path))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
        else:
            text = ""
        
        issues = validate_row(row, text)
        if issues:
            all_issues.append((fname, row['carrier'], issues))
            for field, msg in issues:
                issue_counter[field] += 1
                carrier_issues[row['carrier']] += 1
    
    # Summary
    total_issues = sum(len(issues) for _, _, issues in all_issues)
    files_with_issues = len(all_issues)
    clean_files = len(rows) - files_with_issues
    
    print(f"\n{'='*70}")
    print(f"VALIDATION RESULTS")
    print(f"{'='*70}")
    print(f"  Total files: {len(rows)}")
    print(f"  Clean (no issues): {clean_files} ({100*clean_files//len(rows)}%)")
    print(f"  With issues: {files_with_issues}")
    print(f"  Total issues: {total_issues}")
    
    if issue_counter:
        print(f"\n  Issues by field:")
        for field, count in issue_counter.most_common():
            print(f"    {field:20s}: {count}")
        
        print(f"\n  Issues by carrier:")
        for carrier, count in carrier_issues.most_common():
            print(f"    {carrier:15s}: {count}")
    
    if args.fix and all_issues:
        print(f"\n{'='*70}")
        print(f"DETAILS (files needing review)")
        print(f"{'='*70}")
        for fname, carrier, issues in all_issues[:50]:
            print(f"\n  [{carrier}] {fname}")
            for field, msg in issues:
                print(f"    {field}: {msg}")
    
    if not all_issues:
        print(f"\n  ✓ All files passed validation!")


if __name__ == "__main__":
    main()
