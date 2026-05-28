"""
extract_invoices.py - v6
Extracts data from freight invoices: SAIA, Dayton, AAA Cooper, FedEx.
No AI - uses PyMuPDF. Processes ~1000 PDFs in ~10 seconds.

Changes v6:
- find() without DOTALL by default (prevents excessive captures)
- sanitize_amount() validates amount and weight ranges
- SAIA layout relative to header (handles filled DUE C/L)
- executemany for SQLite inserts
- Shared config.py
- Incremental processing with --force

Usage:
    py -3 extract_invoices.py           # only new PDFs
    py -3 extract_invoices.py --force   # reprocess all

Requirements:
    pip install pymupdf tqdm
"""

import argparse
import csv
import re
import sqlite3
from pathlib import Path

import fitz
from tqdm import tqdm

from config import PDF_DIR, CSV_OUT, DB_OUT, ALL_FIELDS, CRITICAL_FIELDS, CSV_CHARGES, JSON_OUT


def find(pattern, text, default="", flags=re.IGNORECASE):
    """Search pattern in text. No DOTALL by default to prevent excessive captures."""
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else default


# ── FIX 3: normalize OCR-fragmented numbers ───────────────────────────────────
def normalizar_ocr_numeros(text):
    """
    AAA Cooper OCR fragments amounts across multiple lines:
      "$ 6 . \n7 5"  ->  "$6.75"
      "$1 7 . \n9 5" ->  "$17.95"
    If the next line is only digits and spaces (no letters or $),
    join it to the current line removing internal spaces.
    """
    lines = text.split("\n")
    resultado = []
    i = 0
    while i < len(lines):
        linea = lines[i]
        if i + 1 < len(lines):
            siguiente = lines[i + 1].strip()
            if re.match(r'^[\d\s]+$', siguiente) and siguiente:
                numero_limpio = re.sub(r'\s+', '', linea.rstrip() + siguiente)
                resultado.append(numero_limpio)
                i += 2
                continue
        resultado.append(linea)
        i += 1
    return "\n".join(resultado)


# ── FIX 1: parse dates with corrupt OCR ───────────────────────────────────────
def parsear_fecha(raw):
    """
    Normalizes dates where OCR omits or corrupts separators:
      "10/2812025"  ->  "10/28/2025"  (missing second /)
      "10/3112025"  ->  "10/31/2025"
      "10/16/2025"  ->  no change (already correct)
      "09129120251" ->  "09/29/2025"  (/ replaced by 1, trailing garbage)
      "10/17120251" ->  "10/17/2025"  (extra digits after year)
    """
    if not raw:
        return ""
    # Clean everything except digits and /
    raw = re.sub(r'[^\d/]', '', raw.strip())
    # Already correct
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
        return raw
    # Missing second /: "10/2812025" -> extract month, day(2), year(4)
    m = re.match(r'^(\d{1,2})/(\d{2})(\d{4})$', raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # Extra trailing digits after year: "10/17120251" -> "10/17" + "12025" + "1"
    # Take first 2 digits after / as day, then next 4 as year
    m = re.match(r'^(\d{1,2})/(\d{2})\d?(\d{4})', raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # No slash at all: "09129120251" -> MM(2) + separator(1) + DD(2) + separator(1) + YYYY(4) + garbage
    # Pattern: first 2 = month, skip 1, next 2 = day, skip 1, next 4 = year
    m = re.match(r'^(\d{2})\d(\d{2})\d(\d{4})', raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31 and 2020 <= year <= 2030:
            return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # Short format without second /: "10/2825"
    m = re.match(r'^(\d{1,2})/(\d{2})(\d{2})$', raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # Pure digits 8: "10222025" -> "10/22/2025"
    m = re.match(r'^(\d{2})(\d{2})(\d{4})$', raw)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31 and 2020 <= year <= 2030:
            return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return ""


def detect_carrier(text):
    t = text.upper()
    if "SAIA MOTOR FREIGHT" in t or "DUE SAIA" in t:
        return "SAIA"
    if "DAYTON FREIGHT" in t:
        return "DAYTON"
    if "AAA COOPER" in t or "AAACOOPER" in t:
        return "AAA_COOPER"
    if "FEDEX" in t or "FXFE" in t or "FXF " in t:
        return "FEDEX"
    return "OTHER"


# ── Zip code and class extraction helpers ──────────────────────────────────────
def extract_zip(text_after_state):
    """Extract zip code (5 digits or 5-4 format) from text following a state abbreviation."""
    m = re.search(r'\b(\d{5}(?:[-\.]\d{4})?)\b', text_after_state)
    if m:
        return m.group(1).replace('.', '-')
    return ""


def extract_freight_class(text):
    """Extract freight class (2-3 digit number like 55, 065, 100, 150)."""
    # Common patterns per carrier:
    # SAIA: "PT IT 150560 \n55\n" (after NMFC number)
    # FedEx: in charges section "1,856 \n055 \n35.630"
    # Dayton: "Class \nWGT" header then "55 \n718"
    # AAA Cooper: "NMFC-Sub \nClass \n... \n150560 - \n55"
    
    # Pattern 1: NMFC number followed by class on next line
    m = re.search(r'150560[-\s]*\d*\s*\n\s*(\d{2,3})\b', text)
    if m:
        return m.group(1)
    
    # Pattern 2: After "Class" header
    m = re.search(r'\bClass\s*\n(?:WGT[^\n]*\n)?(?:[^\n]*\n){0,3}?(\d{2,3})\s+\d{2,5}', text)
    if m:
        return m.group(1)
    
    # Pattern 3: NMFC-Sub + Class header then value
    m = re.search(r'NMFC[-\s]*Sub\s*\n\s*Class\s*\n.*?(\d{2,3})\s*\n', text, re.DOTALL)
    if m and int(m.group(1)) in (50, 55, 60, 65, 70, 77, 85, 92, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500):
        return m.group(1)
    
    # Pattern 4: In FedEx charges area "weight \n class \n rate"
    m = re.search(r'\d{2,5}\s*\n\s*(0\d{2})\s*\n\s*\d+\.\d{3}', text)
    if m:
        return m.group(1)
    
    # Pattern 5: "IT 150560 \n55" (SAIA)
    m = re.search(r'IT\s+\d{5,6}\s*\n\s*(\d{2,3})\b', text)
    if m:
        return m.group(1)
    
    return ""


# ── Amount range validation ────────────────────────────────────────────────────
def sanitize_amount(val, min_val=0, max_val=99999):
    """Validates that an amount is within a reasonable range. Returns '' if invalid."""
    if not val:
        return ""
    try:
        n = float(val.replace(",", "").replace("$", ""))
        if min_val <= n <= max_val:
            return val
        return ""  # fuera de rango = dato corrupto
    except ValueError:
        return ""


def sanitize_weight(val):
    """Valid weight: between 1 and 50000 lbs."""
    return sanitize_amount(val, min_val=1, max_val=50000)


def sanitize_extracted(extracted):
    """Apply range validation to numeric fields in the result."""
    extracted["due_amount"] = sanitize_amount(extracted.get("due_amount", ""))
    extracted["total_charges"] = sanitize_amount(extracted.get("total_charges", ""))
    extracted["fuel_surcharge"] = sanitize_amount(extracted.get("fuel_surcharge", ""), max_val=5000)
    extracted["discount"] = sanitize_amount(extracted.get("discount", ""), max_val=50000)
    extracted["weight"] = sanitize_weight(extracted.get("weight", ""))
    return extracted


# ── Charge detail extraction ──────────────────────────────────────────────────
import json


def extract_charges_fedex(text):
    """Extract ALL individual charge line items from FedEx invoice."""
    charges = []

    # Find the charges section between CHARGES header and Invoicing Summary
    section = re.search(r'CHA.GES\s*\n(.*?)(?:Invoicing Summary|Rate Tariff)', text, re.DOTALL)
    if not section:
        # Fallback: OCR corrupts "CHARGES" header (e.g. "• GES", "'ESC • • i")
        # Look for the block between freight data and "Invoicing Summary"
        inv_idx = text.find('Invoicing Summary')
        if inv_idx > 0:
            # Take the 1500 chars before "Invoicing Summary" as the charges block
            block_start = max(0, inv_idx - 1500)
            block = text[block_start:inv_idx]
        else:
            return ""
    else:
        block = section.group(1)

    # Main freight line: "PAPER GUMMED NOT CLOTH LINED \n106 \n175 \n357.290 \n378.73"
    # or "49 \nTRANSFER METAL \n1,856 \n055 \n35.630 \n661.29"
    # Pattern: description + weight + class + rate + amount (with optional pieces before)
    freight = re.search(r'([A-Z][A-Z &\-]+?)\s*\n[\d,]+\s*\n\d{2,3}\s*\n[\d\.]+\s*\n([\d,\.]+)', block)
    if freight:
        freight_amt = freight.group(2)
        # Skip if the freight amount equals the total (means it's a flat rate, not a breakdown)
        total_due = find(r'Total Amount Due\s+([\d,\.]+)', text)
        if not total_due:
            total_due = find(r'PLEASE PAY THIS AMOUNT\s+([\d,\.]+)', text)
        # Skip if amount looks like a weight (e.g. "5.516" = 5,516 lbs)
        # or a route reference (e.g. "7180")
        valid_charge = True
        try:
            amt_float = float(freight_amt.replace(',', ''))
            # Must have decimal point to be a dollar amount
            if '.' not in freight_amt:
                valid_charge = False
            # If amount has format X.XXX (thousands with dot as separator), skip
            elif len(freight_amt.split('.')[1]) == 3 and amt_float > 1000:
                valid_charge = False
        except ValueError:
            valid_charge = False
        if valid_charge and freight_amt != total_due:
            charges.append({"description": freight.group(1).strip(), "amount": freight_amt})

    # Deficit weight: "0000144 DEFICIT WT -LOWER CHARGES \n144 \n35.630 \n51.31"
    deficit = re.search(r'DEFICIT WT[^\n]*\n\d+\s*\n[\d\.]+\s*\n([\d,\.]+)', block)
    if deficit:
        charges.append({"description": "DEFICIT WT CHARGE", "amount": deficit.group(1)})

    # Generic coded charges: "002400 CALIFORNIA COMPLIANCE \n24.00"
    # or "003000 DEMAND SURCHARGE TIER 1 \n30.00"
    # Pattern: 5-9 digit code + DESCRIPTION (may include numbers) \n amount
    coded_charges = re.findall(r'\d{5,9}\s+([A-Z][A-Z &\-/\.\d]+?)\s*\n(-?[\d,\.]+\.?\d*)', block)
    for desc, amt in coded_charges:
        desc = desc.strip()
        amt = amt.rstrip('.')
        # Skip noise lines
        if any(skip in desc for skip in ['ORIGINAL REVENUE', 'RATED AS', 'ZONE NUMBER', 'INSPECTION', 'VALIDATION']):
            continue
        if re.match(r'^-?\d+$', amt) and abs(int(amt)) > 100:  # skip pure integers > 100 (likely references)
            continue
        # Must look like a dollar amount
        if '.' not in amt and not amt.startswith('-'):
            continue
        # Validate amount is reasonable (< $5000)
        try:
            if abs(float(amt.replace(',', ''))) > 5000:
                continue
        except ValueError:
            continue
        charges.append({"description": desc, "amount": amt})

    # Fuel surcharge: "001047 FUEL SURCHG LTL SHPT 7.00% \n10.47"
    fuel = re.search(r'FUEL SURCHG\s+LTL\s+SHPT\s+([\d\.]+%)\s*\n(-?[\d,\.]+)', block)
    if fuel:
        # Check if already captured by coded_charges
        fuel_already = any("FUEL" in c["description"] for c in charges)
        if not fuel_already:
            fuel_amt = fuel.group(2)
            # Validate: fuel surcharge should be >= $1.00 and <= $500
            try:
                fuel_val = float(fuel_amt)
                if 1.0 <= fuel_val <= 500:
                    charges.append({"description": f"FUEL SURCHARGE {fuel.group(1)}", "amount": fuel_amt})
            except ValueError:
                pass

    # Less discount: "775 LESS DISCOUNT \n.775 \n552.27-" or "661 LESS DISCOUNT \n.661 \n947.14."
    less_disc = re.search(r'LESS DISCOUNT\s*\n[\.\d]+\s*\n([\d,\.]+)', block)
    if less_disc:
        amt = less_disc.group(1).rstrip('.-')
        # Fix OCR: "1,767,53" -> "1,767.53" (last comma should be decimal point)
        if re.match(r'^\d{1,3},\d{3},\d{2}$', amt):
            amt = amt[::-1].replace(',', '.', 1)[::-1]  # replace last comma with dot
        charges.append({"description": "LESS DISCOUNT", "amount": f"-{amt}"})

    # Earned Discount (in Invoicing Summary section)
    earned = find(r'Earned Discount\s*\n([\d,\.]+)', text, flags=re.IGNORECASE)
    if not earned:
        earned = find(r'Earned Discount\s+([\d,\.]+)', text, flags=re.IGNORECASE)
    if earned:
        earned = earned.rstrip('.')
        charges.append({"description": "EARNED DISCOUNT", "amount": f"-{earned}"})

    return json.dumps(charges) if charges else ""


def extract_charges_saia(text):
    """Extract ALL individual charge line items from SAIA invoice."""
    charges = []

    # Base freight: "55 337 299.00" (class weight amount)
    rate_match = re.search(r'\d+\s+\d{2,3}\s+(\d{2,5})\s+([\d,\.]+)\s*$', text, re.MULTILINE)
    if rate_match:
        # Validate: amount should look like a dollar amount (have a decimal point or be < 10000)
        amt = rate_match.group(2)
        try:
            amt_float = float(amt.replace(',', ''))
            if amt_float < 10000 and '.' in amt:
                charges.append({"description": "BASE FREIGHT", "amount": amt})
        except ValueError:
            pass

    # Generic SAIA charges: "DESCRIPTION \n CODE \n amount"
    # FUEL SURCHARGE \n FS \n 21.68
    # SINGLE SHIPMENT CHARGE \n SS \n 29.00
    # LIFTGATE DELV/HAND UNLOAD \n LGATE \n 33.00
    # RESIDENTIAL DELIVERY \n RESDEL \n 166.00
    saia_charges = re.findall(
        r'([A-Z][A-Z &/\-\.]+?)\s*\n([A-Z]{2,6})\s*\n([\d,\.]+)',
        text
    )
    for desc, code, amt in saia_charges:
        desc = desc.strip()
        # Skip noise — only keep known charge codes
        valid_codes = ('FS', 'SS', 'LGATE', 'RESDEL', 'DISCN', 'NOT', 'CDA', 'DSDD', 'TPFO')
        if code not in valid_codes:
            continue
        # Clean trailing comma/dot
        amt = amt.rstrip(',.')
        # Skip if amount looks like an invoice number (>8 digits)
        if len(amt.replace(',', '').replace('.', '')) > 6:
            continue
        # Validate: must be a valid number and <= $5000
        try:
            if abs(float(amt.replace(',', ''))) > 5000:
                continue
        except ValueError:
            continue
        charges.append({"description": desc, "amount": amt, "code": code})

    # Discount: "DISCOUNT DISCN CNT 50.40" or "DISCN \n CNT \n 50.40"
    disc = find(r'DISCOUNT\s+DISCN\s+CNT\s+([\d,\.]+)', text)
    if not disc:
        disc = find(r'DISCN\s+CNT\s+([\d,\.]+)', text)
    if disc:
        charges.append({"description": "DISCOUNT", "amount": f"-{disc}"})

    return json.dumps(charges) if charges else ""


def extract_charges_dayton(text):
    """Extract ALL individual charge line items from Dayton invoice."""
    charges = []

    # Base freight: class WGT rate Amount — "55 1342 44.17 592.76"
    base_match = re.search(r'\d{2,3}\s+(\d{2,5})\s+([\d\.]+)\s+([\d,\.]+)\s*\n.*?NMFC', text, re.DOTALL)
    if base_match:
        charges.append({"description": "BASE FREIGHT", "amount": base_match.group(3),
                        "weight": base_match.group(1), "rate": base_match.group(2)})

    # All accessorial charges: "DESCRIPTION percentage% \n CODE \n amount"
    # FUEL SURCHARGE 7.000% \n FS \n 5.81
    # CALL FOR DELIVERY APPOINTMENT \n CDA \n 0.00
    dayton_charges = re.findall(
        r'([A-Z][A-Z &/\-\.]+?(?:\s+[\d\.]+%)?)\s*\n([A-Z]{2,5})\s*\n([\d,\.]+)',
        text
    )
    for desc, code, amt in dayton_charges:
        desc = desc.strip()
        # Only keep known Dayton charge codes
        valid_codes = ('FS', 'CDA', 'NOT', 'LG', 'RES', 'IG')
        if code not in valid_codes:
            continue
        charges.append({"description": desc, "amount": amt, "code": code})

    # Charges subject to discount
    subj = find(r'Charges\s+([\d,\.]+)\s*\nSubject', text)
    if subj:
        charges.append({"description": "CHARGES SUBJECT TO DISCOUNT", "amount": subj})

    # Discount: "Discount .8600 509.77"
    disc_match = re.search(r'Discount\s+([\.\d]+)\s+([\d,\.]+)', text)
    if disc_match:
        charges.append({"description": f"DISCOUNT ({disc_match.group(1)})", "amount": f"-{disc_match.group(2)}"})

    # Other charges
    other = find(r'Other\s*\nCharges\s+([\d,\.]+)', text)
    if other:
        charges.append({"description": "OTHER CHARGES (NON-DISCOUNTABLE)", "amount": other})

    return json.dumps(charges) if charges else ""


def extract_charges_aaa(text):
    """Extract ALL individual charge line items from AAA Cooper invoice.
    
    AAA Cooper OCR renders charges in this pattern:
      - Base charge: "$628.21" (first large $ amount in charges section)
      - Discount: "DISCOUNT 78.60%" followed by "-$493.77" (or vice versa)
      - Fuel: "FUEL SURCHARGE @ 7.25%" followed by "$9.75" (or vice versa)
      - State: "STATE COMPLIANCE CA" followed by "$17.95"
    
    Amounts can appear BEFORE or AFTER descriptions due to OCR column reading.
    """
    charges = []

    # --- Base freight charge ---
    # The base charge is the first $XXX.XX in the Charges column (after "Charges" header)
    # Pattern: "Rate \n Charges \n ... \n $XXX.XX" or just the first large dollar amount
    # Look for the charges section: after "Charges" header, first $amount
    charges_section = re.search(
        r'(?:Rate\s*[i|:;]?\s*Charges|Charges)\s*\n(.*?)(?:PCS TTL|\*Prepaid\*)',
        text, re.DOTALL | re.IGNORECASE
    )
    if charges_section:
        section = charges_section.group(1)
    else:
        # Fallback: use full text
        section = text

    # Base freight: first positive $amount > $50 in the section (not the $.00 entries)
    base_match = re.search(r'\$([\d,]+\.\d{2})\s', section)
    if base_match:
        amt = base_match.group(1).replace(',', '')
        try:
            if float(amt) > 50:
                charges.append({"description": "BASE FREIGHT", "amount": base_match.group(1)})
        except ValueError:
            pass

    # --- Discount ---
    # Pattern: "DISCOUNT XX.XX%" followed by "-$amount" within a few lines
    # OCR noise lines (., :, ', etc.) may appear between description and amount
    disc_match = re.search(
        r'DISCOUNT\s+([\d\.]+%)\s*\n(?:[^\-\$\n]{0,5}\n){0,4}-\$([\d,\.]+)',
        section
    )
    if disc_match:
        charges.append({"description": f"DISCOUNT {disc_match.group(1)}", "amount": f"-{disc_match.group(2)}"})
    else:
        # Pattern B: "-$amount \n ... \n DISCOUNT XX.XX%"
        disc_match2 = re.search(r'-\$([\d,\.]+)\s*\n(?:[^\n]*\n){0,2}?DISCOUNT\s+([\d\.]+%)', section)
        if disc_match2:
            charges.append({"description": f"DISCOUNT {disc_match2.group(2)}", "amount": f"-{disc_match2.group(1)}"})
        else:
            # Pattern C: "-$amount" and "DISCOUNT XX%" both present in section
            disc_amt = re.search(r'-\$([\d,]+\.\d{2})', section)
            disc_pct = re.search(r'DISCOUNT\s+([\d\.]+%)', section)
            if disc_amt and disc_pct:
                charges.append({"description": f"DISCOUNT {disc_pct.group(1)}", "amount": f"-{disc_amt.group(1)}"})
            elif disc_amt:
                charges.append({"description": "DISCOUNT", "amount": f"-{disc_amt.group(1)}"})

    # --- Fuel surcharge ---
    # Pattern: "FUEL SURCHARGE @ X.XX%" followed by "$amount" within a few lines
    # OCR noise lines (., :, ', etc.) may appear between description and amount
    fuel_match = re.search(
        r'FUEL SURCHARGE\s*@\s*([\d\.]+%)\s*\n(?:[^\$\n]{0,5}\n){0,3}\s*\$([\d,\.]+)',
        section
    )
    if fuel_match:
        charges.append({"description": f"FUEL SURCHARGE {fuel_match.group(1)}", "amount": fuel_match.group(2)})
    else:
        # Pattern B: "$amount \n ... \n FUEL SURCHARGE @ X.XX%" (amount before desc)
        fuel_match2 = re.search(r'\$([\d,]+\.\d{2})\s*\n(?:[^\n]*\n){0,2}?FUEL SURCHARGE\s*@\s*([\d\.]+%)', section)
        if fuel_match2:
            charges.append({"description": f"FUEL SURCHARGE {fuel_match2.group(2)}", "amount": fuel_match2.group(1)})
        else:
            # Pattern C: "FUEL SURCHARGE @ X.XX% $amount" (same line)
            fuel_match3 = re.search(r'FUEL SURCHARGE\s*@\s*([\d\.]+%)\s*\$?([\d,]+\.\d{2})', text)
            if fuel_match3:
                charges.append({"description": f"FUEL SURCHARGE {fuel_match3.group(1)}", "amount": fuel_match3.group(2)})

    # --- State compliance ---
    # Pattern: "STATE COMPLIANCE XX" followed by "$amount" within a few lines
    # OCR noise lines (., :, ', etc.) may appear between description and amount
    state_matches = list(re.finditer(
        r'STATE COMPLIANCE\s+([A-Z]+)\s*\n(?:[^\$\n]{0,5}\n){0,3}\s*\$([\d,\. ]+)',
        section
    ))
    for sm in state_matches:
        amt = sm.group(2).replace(' ', '')
        if re.match(r'^\d+\.\d{2}$', amt):
            charges.append({"description": f"STATE COMPLIANCE {sm.group(1)}", "amount": amt})

    if not any("STATE COMPLIANCE" in c["description"] for c in charges):
        # Pattern B: "$amount \n ... \n STATE COMPLIANCE XX"
        state_matches2 = list(re.finditer(r'\$([\d,\. ]+)\s*\n(?:[^\n]*\n){0,2}?STATE COMPLIANCE\s+([A-Z]+)', section))
        for sm in state_matches2:
            amt = sm.group(1).replace(' ', '')
            if re.match(r'^\d+\.\d{2}$', amt):
                charges.append({"description": f"STATE COMPLIANCE {sm.group(2)}", "amount": amt})

    if not any("STATE COMPLIANCE" in c["description"] for c in charges):
        # Pattern C: "STATE COMPLIANCE XX $amount" (same line, possibly with OCR spaces)
        state_matches3 = re.findall(r'STATE COMPLIANCE\s+([A-Z]+)\s+\$?([\d,\. ]+)', text)
        for state, amt in state_matches3:
            amt = amt.replace(' ', '').strip()
            if amt and re.match(r'^\d+\.\d{2}$', amt):
                charges.append({"description": f"STATE COMPLIANCE {state}", "amount": amt})

    # --- Other accessorials ---
    accessorial_patterns = [
        (r'(LIFTGATE[^\n]*?)\s+\$([\d,\.]+)', None),
        (r'(RESIDENTIAL[^\n]*?)\s+\$([\d,\.]+)', None),
        (r'(NOTIFY[^\n]*?)\s+\$([\d,\.]+)', None),
        (r'(INSIDE DELIVERY[^\n]*?)\s+\$([\d,\.]+)', None),
        (r'(LIMITED ACCESS[^\n]*?)\s+\$([\d,\.]+)', None),
    ]
    for pattern, _ in accessorial_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            desc = m.group(1).strip().upper()
            amt = m.group(2)
            # Avoid duplicates
            if not any(desc in c["description"] for c in charges):
                charges.append({"description": desc, "amount": amt})

    return json.dumps(charges) if charges else ""


# ── SAIA ───────────────────────────────────────────────────────────────────────
def extract_saia_layout(doc):
    """
    Extracts SAIA data using x/y coordinates relative to the header.
    Anchors each data value to the nearest header column, not fixed pixels.
    This correctly handles the case where DUE C/L has a value.
    """
    page = doc[0]
    blocks = page.get_text("dict")["blocks"]

    # Step 1: find the header line and extract each column's position
    header_spans = []
    header_y = None
    for block in blocks:
        for line in block.get("lines", []):
            line_text = "".join(span["text"] for span in line["spans"])
            if "DESTINATION" in line_text and "ORIGIN" in line_text:
                header_y = line["bbox"][3]  # bottom of header line
                for span in line["spans"]:
                    txt = span["text"].strip()
                    if txt:
                        header_spans.append({"text": txt, "x": span["bbox"][0]})
                break
        if header_y:
            break

    if not header_y or not header_spans:
        return "", "", "", "", "", ""

    # Build column map: name -> x position
    col_map = {}
    for span in header_spans:
        col_map[span["text"]] = span["x"]

    # Step 2: extract spans from the data row (just below the header)
    data_spans = []
    for block in blocks:
        for line in block.get("lines", []):
            line_top = line["bbox"][1]
            if header_y < line_top < header_y + 30:
                for span in line["spans"]:
                    txt = span["text"].strip()
                    if txt:
                        data_spans.append({"text": txt, "x": span["bbox"][0]})

    data_spans = sorted(data_spans, key=lambda s: s["x"])

    # Step 3: assign each data_span to the nearest header by x position
    def nearest_col(x):
        return min(col_map, key=lambda c: abs(col_map[c] - x))

    result = {col: "" for col in col_map}
    for span in data_spans:
        col = nearest_col(span["x"])
        if not result[col]:  # first value wins (avoid overwriting)
            result[col] = span["text"]

    # Map column names to variables (tolerant to variants)
    date = ""
    biller = ""
    accts_rec = ""
    due_saia = ""
    destination = ""
    origin = ""

    for col_name, val in result.items():
        name_upper = col_name.upper()
        if "DATE" in name_upper:
            date = val
        elif "BILLER" in name_upper:
            biller = val
        elif "ACCTS" in name_upper or "REC" in name_upper:
            accts_rec = val
        elif "DUE" in name_upper and "C/L" not in name_upper:
            due_saia = val
        elif "DESTINATION" in name_upper or "DEST" in name_upper:
            destination = val
        elif "ORIGIN" in name_upper:
            origin = val

    return date, biller, accts_rec, due_saia, destination, origin


def extract_saia(text, doc=None):
    # Try coordinate-based extraction if we have the doc
    date = biller = accts_rec = due_saia = destination = origin = ""

    if doc:
        try:
            date, biller, accts_rec, due_saia, destination, origin = extract_saia_layout(doc)
        except Exception:
            pass  # fallback to original text-based method

    # Fallback: original text-based method
    if not due_saia:
        data_block = find(
            r'DESTINATION\s+ORIGIN\s*\n(.*?)(?:PURCHASE ORDER|BL#|$)', text,
            flags=re.IGNORECASE | re.DOTALL
        )
        parts = re.split(r'\s+', data_block.strip()) if data_block else []

        date      = parts[0] if len(parts) > 0 else ""
        biller    = parts[1] if len(parts) > 1 else ""
        accts_rec = parts[2] if len(parts) > 2 else ""
        due_saia  = parts[3] if len(parts) > 3 else ""

        codes = re.findall(r'\b([A-Z]{2,5})\b', data_block) if data_block else []
        loc_codes = [c for c in codes if c not in (biller, 'PAGE', 'ORIGI') and len(c) >= 2]
        destination = loc_codes[0] if len(loc_codes) >= 1 else ""
        origin      = loc_codes[1] if len(loc_codes) >= 2 else ""

    invoice_no = find(r'\b(\d{10,12})\b', text)
    total      = find(r'([\d,\.]+)PPD', text)
    fuel       = find(r'FUEL SURCHARGE\s+FS\s+([\d,\.]+)', text)
    if not fuel:
        fuel = find(r'FS\s*\n([\d,\.]+)', text)
    discount = find(r'DISCOUNT\s+DISCN\s+CNT\s+([\d,\.]+)', text)
    if not discount:
        discount = find(r'DISCN\s+CNT\s+([\d,\.]+)', text)
    weight = find(r'(\d{2,5})\s+[\d,\.]+PPD', text)

    # Zip codes: "CITY \n ST ZIPCODE"
    # SAIA layout: shipper address has "CITY\nST ZIPCODE\n" before DRIVER'S No
    # Consignee: after shipper, "CITY\nST ZIPCODE\nO WAUSAU"
    # The shipper is between "E" markers, consignee between "P" and "O" markers
    shipper_zip = find(r'E\s+[A-Z]+\s*\n[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\s*\n', text)
    # Consignee zip: city/state before "O WAUSAU" (the bill-to line)
    consignee_zip = find(r'[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\s*\nO\s+WAUSAU', text)
    if not consignee_zip:
        # Alternative: look for zip after the shipper section, before "O" marker
        consignee_zip = find(r't\s*\n[A-Z]+\s*\n[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\s*\nO\s+', text)

    # Freight class: after NMFC "150560" the class is on the next line
    # SAIA text: "PT IT 150560 \n55\n337" (NMFC \n class \n weight)
    fc_match = re.search(r'(?:IT|PT)\s+(?:IT\s+)?\d{5,6}\s*\n(\d{2,3})\s*\n', text)
    freight_class = fc_match.group(1) if fc_match else ""
    if not freight_class:
        freight_class = find(r'150560\s*\n(\d{2,3})\b', text)
    if not freight_class:
        freight_class = extract_freight_class(text)

    return {
        "date":           date,
        "invoice_no":     invoice_no,
        "pro_number":     invoice_no,
        "po_number":      find(r'PO#\s*([^\n]+)', text),
        "bl_number":      find(r'BL#\s*(\d+)', text),
        "biller":         biller,
        "accts_rec":      accts_rec,
        "due_amount":     due_saia,
        "total_charges":  total,
        "fuel_surcharge": fuel,
        "discount":       discount,
        "origin":         origin,
        "destination":    destination,
        "weight":         weight,
        "shipper_name":   find(r'((?:EMPOWER LABEL|WAUSAU COATED PRODUCTS)[^\n]*)', text),
        "shipper_zip":    shipper_zip,
        "consignee_name": find(r'(?:CONS:|CONSIGNEE)\s*([A-Z][A-Z &]+?)(?:\n|PLANT)', text),
        "consignee_zip":  consignee_zip,
        "freight_class":  freight_class,
        "payment_terms":  "PREPAID" if "PPD" in text else "",
        "payment_due_date": "",
        "charges_detail": extract_charges_saia(text),
    }


# ── DAYTON ─────────────────────────────────────────────────────────────────────
def extract_dayton(text):
    # Layout Dayton:
    # Phone (937) 264-4060  10/21/2025  684234661
    # Orig. Term.  Dest. Term  Orig. Partner  Dest. Partner  Shipper BL Number  PO Number  Shipper Ref
    # MSP          MSP                                       409828             324707     NS
    date_inv = re.search(r'Phone[^\n]+\n(\d{1,2}/\d{1,2}/\d{4})\s+(\d{6,})', text)
    date       = date_inv.group(1) if date_inv else find(r'(\d{1,2}/\d{1,2}/\d{4})\s+\d{6,}', text)
    invoice_no = date_inv.group(2) if date_inv else find(r'\d{1,2}/\d{1,2}/\d{4}\s+(\d{6,})', text)

    hdr = re.search(
        r'Orig\.\s*Term\.\s*\nDest\.\s*Term\s*\n'
        r'(?:Orig\.\s*Partner\s*\n)?(?:Dest\.\s*Partner\s*\n)?'
        r'Shipper BL Number\s*\nPO Number\s*\n(?:Shipper Reference Number\s*\n)?'
        r'([A-Z0-9]+)\s*\n([A-Z0-9]+)\s*\n([A-Z0-9]+)\s*\n([A-Z0-9]+)',
        text
    )
    if hdr:
        origin      = hdr.group(1)
        destination = hdr.group(2)
        bl_number   = hdr.group(3)
        po_number   = hdr.group(4)
        # "NS" means "Not Specified" — not a real BL/PO
        if bl_number == "NS":
            bl_number = ""
        if po_number == "NS":
            po_number = ""
    else:
        origin      = find(r'Orig\.\s*Term\.\s*\n(?:[^\n]+\n){0,5}([A-Z]{2,5})\b', text)
        destination = ""
        bl_number   = find(r'Shipper BL Number\s*\n(?:PO Number\s*\n)?(?:Shipper Reference Number\s*\n)?[A-Z]{2,5}\s*\n[A-Z]{2,5}\s*\n(\d+)', text)
        po_number   = find(r'PO Number\s*\n(?:Shipper Reference Number\s*\n)?[A-Z]{2,5}\s*\n[A-Z]{2,5}\s*\n\d+\s*\n(\d+)', text)

    pay_amount    = find(r'Pay This Amount\s+([\d,\.]+)', text)
    total_charges = find(r'Charges\s+([\d,\.]+)\s*\nSubject', text)
    fuel          = find(r'FUEL SURCHARGE\s+[\d\.]+%\s*\nFS\s+([\d,\.]+)', text)
    if not fuel:
        fuel = find(r'FS\s+([\d,\.]+)', text)
    discount = find(r'Discount\s+([\d,\.]+)', text)
    weight   = find(r'Total Charges\s+(\d{2,5})\s+[\d,\.]+', text)

    # Zip codes: "CITY, ST ZIPCODE" format in Dayton
    # Shipper and consignee are in the header block
    # "WAUSAU, WI 54401 \nDES MOINES, IA 50321"
    shipper_zip = ""
    consignee_zip = ""
    # Find both addresses in the header (shipper first, then consignee)
    addr_zips = re.findall(r'[A-Z][A-Za-z ]+,\s*[A-Z]{2}\s+(\d{5}(?:-\d{4})?)', text)
    if len(addr_zips) >= 2:
        shipper_zip = addr_zips[0]
        consignee_zip = addr_zips[1]
    elif len(addr_zips) == 1:
        shipper_zip = addr_zips[0]

    # Freight class
    freight_class = extract_freight_class(text)

    return {
        "date":           date,
        "invoice_no":     invoice_no,
        "pro_number":     find(r'PRO Number\s+(\d{6,})', text) or invoice_no,
        "po_number":      po_number,
        "bl_number":      bl_number,
        "biller":         "",
        "accts_rec":      find(r'Billed to[\.]*\s*(\d+)', text),
        "due_amount":     pay_amount,
        "total_charges":  total_charges,
        "fuel_surcharge": fuel,
        "discount":       discount,
        "origin":         origin,
        "destination":    destination,
        "weight":         weight,
        "shipper_name":   find(r'Shipper\s*\n([A-Z][A-Z &]+)\n', text),
        "shipper_zip":    shipper_zip,
        "consignee_name": find(r'Consignee\s*\n([A-Z][A-Z &]+)\n', text),
        "consignee_zip":  consignee_zip,
        "freight_class":  freight_class,
        "payment_terms":  find(r'Terms:\s*([A-Z ]+\d*\s*DAYS?)', text),
        "payment_due_date": find(r'Invoice Date:\s*\n(\d{1,2}/\d{1,2}/\d{4})', text),
        "charges_detail": extract_charges_dayton(text),
    }


# ── AAA COOPER ─────────────────────────────────────────────────────────────────
def extract_aaa_cooper(text):
    # Save original text for PO/BL (before normalizing)
    text_original = text
    # FIX 3: normalize OCR-fragmented numbers for amounts
    text = normalizar_ocr_numeros(text)

    cust_pro = re.search(r'PRO NUMBER\s*\n[^\n]*\n\s*(\d{5,})\s+(\d{6,})', text)
    customer_no = cust_pro.group(1) if cust_pro else ""
    pro_number  = cust_pro.group(2) if cust_pro else ""
    if not pro_number:
        # Pattern: 6-digit customer number on one line, 8-digit pro on next line
        # After "CUSTOMER NUMBER\nPRO NUMBER\n[garbage]\n...797191\n68500827"
        cust_pro2 = re.search(r'(\d{5,7})\s*\n(\d{7,9})\nSHIPPER', text)
        if cust_pro2:
            customer_no = cust_pro2.group(1)
            pro_number = cust_pro2.group(2)
    if not pro_number:
        # Payment section: "date\nPRO_NUM\n$amount" — but OCR may join date+pro
        # "11/08/25 \n68500827 \n$114.48" or "11/08/2568500827\n$114.48"
        pro_pay = re.search(r'PAYMENT DUE\s*\n.*?(\d{7,9})\s*\n\s*\$', text, re.DOTALL)
        if pro_pay:
            pro_number = pro_pay.group(1)
    if not pro_number:
        # Fallback: look in Stmt Id line "2466-0000797191-68500827 -VGS-"
        stmt_pro = re.search(r'\d{4}-\d{10}-(\d{7,9})\s*-[A-Z]{3}', text)
        if stmt_pro:
            pro_number = stmt_pro.group(1)
    if not pro_number:
        # From Bill of Lading: "Pro Number: 685008054" (may have extra digit)
        bol_pro = find(r'Pro Number[:\s]+(\d{7,10})', text)
        if bol_pro:
            # Remove trailing check digit if > 9 digits
            pro_number = bol_pro[:8] if len(bol_pro) > 9 else bol_pro
    if not customer_no:
        customer_no = find(r'CUSTOMER NUMBER[:\s]*(\d{5,7})', text_original)
        if not customer_no:
            # From the header: "797191 \n68500827"
            customer_no = find(r'(\d{5,7})\s*\n' + re.escape(pro_number) if pro_number else r'(\d{5,7})\s*\n\d{7,9}', text)

    date = find(r'(?:WAUSAU COATED PRODUCTS[^\n]*\n[^\n]*\n)(\d{1,2}/\d{1,2}/\d{2,4})', text)
    if not date:
        date = find(r'(\d{1,2}/\d{1,2}/\d{2,4})', text)

    origin = find(r'ORIGIN\s*\n[^\n]+\n[^\n]+\n([A-Z]{2,4})\s*\n', text)
    if not origin:
        origin = find(r'ORIGIN\s*\n([A-Z]{2,4})\s*\n', text)
    if not origin:
        # From Stmt Id line: "2466-0000797191-68500827 -VGS-44-"
        origin = find(r'\d{4}-\d{10}-\d{7,9}\s*-([A-Z]{2,4})-', text)
    destination = find(r'\bDEST\s*\n([A-Z]{2,4})\s*\n', text)

    # FIX 4: tolerate up to 3 intermediate lines between "B.L.NUMBER" and the number
    bl_number = find(
        r'B\.L\.NUMBER\s*\n(?:[^\n]*\n){0,3}?(\d{5,})',
        text_original, flags=re.IGNORECASE
    )

    # FIX 3: after normalizing, fuel should be on a single line
    fuel = find(r'FUEL SURCHARGE\s*@\s*[\d\.]+%\s*\$?([\d,\.]+)', text)
    if not fuel:
        fuel = find(r'FUEL SURCHARGE\s*@\s*[\d\.]+%\s*\n\$?([\d,\.]+)', text)

    total  = find(r'\$([\d,\.]+)\s*PPD', text)
    due    = find(r'AMOUNT DUE\s*\n\s*\$([\d,\.]+)', text)
    weight = find(r'(\d{3,5})\s+\$[\d,\.]+\s*PPD', text)

    # PO: between P.0.NUMBER and B.L.NUMBER, can be free text
    # Use original text (not normalized) to avoid collapsing lines
    po_raw = find(r'P\.?0?\.?\s*NUMBER\s*\n(?:B\.L\.NUMBER\s*\n)?(?:ADV\s*\n)?([^\n]+)\n', text_original)
    po_number = ""
    if po_raw:
        po_clean = po_raw.strip()
        # If it's the consignee name (all caps, >2 words), look at the next line
        if re.match(r'^[A-Z][A-Z &\-\.]{5,}$', po_clean):
            # It's the consignee name, PO is on the next line
            po_raw2 = find(r'P\.?0?\.?\s*NUMBER\s*\n(?:B\.L\.NUMBER\s*\n)?(?:ADV\s*\n)?[^\n]+\n([^\n]+)\n', text_original)
            if po_raw2 and po_raw2.strip() and len(po_raw2.strip()) < 40:
                po_number = po_raw2.strip()
        elif po_clean.upper() not in ("B.L.NUMBER", "ADV", "BEYOND", "") and len(po_clean) < 40:
            po_number = po_clean

    # Consignee: OCR corrupts "CONSIGNEE" in many ways
    # Look for the consignee number (7 digits) followed by the name on the next line
    consignee_name = ""
    cons_match = re.search(r'CON[^\n]{0,15}\s+(\d{7})\s*\nP\.?0?\.?\s*NUMBER', text, re.IGNORECASE)
    if cons_match:
        # The name is between the number and "P.O.NUMBER", search intermediate lines
        cons_id = cons_match.group(1)
        # Search name after the consignee ID
        name_match = re.search(cons_id + r'\s*\n(?:P\.?0?\.?\s*NUMBER\s*\n)?(?:B\.L\.NUMBER\s*\n)?(?:ADV\s*\n)?([A-Z][A-Z &\-\.]+)\n', text)
        if name_match:
            consignee_name = name_match.group(1).strip()
    if not consignee_name:
        # Fallback: search between ADV and the address
        consignee_name = find(r'ADV\s*\n([A-Z][A-Z &\-\.]{3,})\s*\n', text)

    # Shipper: "SHIPPER 3648320 \n WAUSAU COATED PRODUCTS"
    shipper_name = find(r'SHIPPER\s+\d+\s*\n([A-Z][A-Z &]+)', text)

    # Payment due date
    payment_due = find(r'PAYMENT DUE\s*\n(\d{1,2}/\d{1,2}/\d{2,4})', text)

    # Zip codes: "CITY, ST ZIPCODE-XXXX" in AAA Cooper
    shipper_zip = find(r'(?:NORTH LAS VEGAS|WAUSAU|[A-Z ]+),\s*[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\s*\n.*?ORIGIN', text_original, flags=re.DOTALL)
    if not shipper_zip:
        shipper_zip = find(r'[A-Z]{2}\s+(\d{5}-\d{4})\s*\n.*?(?:ORIGIN|VGS)', text_original, flags=re.DOTALL)
    consignee_zip = find(r'(?:CONSIGNEE|CON[^\n]{0,10})\s+\d+.*?[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\s*\n', text_original, flags=re.DOTALL)
    if not consignee_zip:
        # After consignee address, before $.00 or ADVANCE
        consignee_zip = find(r',\s*[A-Z]{2}\s+(\d{5}-\d{4})\s*\n\s*\$', text_original)

    # Freight class
    freight_class = extract_freight_class(text)

    return {
        "date":           date,
        "invoice_no":     pro_number,
        "pro_number":     pro_number,
        "po_number":      po_number,
        "bl_number":      bl_number,
        "biller":         "",
        "accts_rec":      customer_no,
        "due_amount":     due or total,
        "total_charges":  total,
        "fuel_surcharge": fuel,
        "discount":       "",
        "origin":         origin,
        "destination":    destination,
        "weight":         weight,
        "shipper_name":   shipper_name,
        "shipper_zip":    shipper_zip,
        "consignee_name": consignee_name,
        "consignee_zip":  consignee_zip,
        "freight_class":  freight_class,
        "payment_terms":  "PREPAID" if "Prepaid" in text or "*Prepaid*" in text else "",
        "payment_due_date": payment_due,
        "charges_detail": extract_charges_aaa(text),
    }


# ── FEDEX ──────────────────────────────────────────────────────────────────────
def extract_fedex(text):
    invoice_no = find(r'Freight Bi[ln]{1,2}\s*Number\s+(\d{6,})', text)

    # FedEx has "Ship Date/Invoice Date 10/22/2025 10/24/2025"
    # We want the INVOICE DATE (second date), not the ship date
    date = ""
    ship_date = ""

    # Try to find both dates on the Ship Date/Invoice Date line
    # OCR renders it in many ways - try patterns from most specific to least
    ship_date_line = re.search(r'Ship Date[^\n]+', text, re.IGNORECASE)
    sdl = ship_date_line.group() if ship_date_line else ""

    # Pattern 1: Two clean dates "10/22/2025 10/24/2025"
    two_clean = re.search(r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})', sdl)
    if two_clean:
        ship_date = two_clean.group(1)
        date = two_clean.group(2)
    else:
        # Pattern 2: Pure digits first + clean second "09129120251 10/07/2025"
        p2 = re.search(r'(\d{8,11})\s+(\d{1,2}/\d{1,2}/\d{4})', sdl)
        if p2:
            ship_date = parsear_fecha(p2.group(1))
            date = p2.group(2)
        else:
            # Pattern 3: Clean first + corrupted second "10/10/2025 10/1512025"
            p3 = re.search(r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{5,8})', sdl)
            if p3:
                ship_date = p3.group(1)
                date = parsear_fecha(p3.group(2))
            else:
                # Pattern 4: Corrupted first (with /) + clean second "10/17120251 10/27/2025"
                p4 = re.search(r'(\d{1,2}/\d{5,8})\s+(\d{1,2}/\d{1,2}/\d{4})', sdl)
                if p4:
                    ship_date = parsear_fecha(p4.group(1))
                    date = p4.group(2)
                else:
                    # Pattern 5: Both corrupted (with /) "10/2212025 10/2412025"
                    p5 = re.search(r'(\d{1,2}/\d{5,8})\s+(\d{1,2}/\d{5,8})', sdl)
                    if p5:
                        ship_date = parsear_fecha(p5.group(1))
                        date = parsear_fecha(p5.group(2))
                    else:
                        # Pattern 6: Pure digits first + corrupted second
                        p6 = re.search(r'(\d{8,11})\s+(\d{1,2}/\d{5,8})', sdl)
                        if p6:
                            ship_date = parsear_fecha(p6.group(1))
                            date = parsear_fecha(p6.group(2))

    # Fallback if no date found from Ship Date line
    if not date:
        raw = find(r'Invoice Date\s+(\d{1,2}/\d{1,2}/\d{4})', text, flags=re.IGNORECASE)
        if raw:
            date = raw
        else:
            raw = find(r'INVOICE DATE\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})', text, flags=re.IGNORECASE)
            if raw:
                date = raw
            else:
                # Remittance section
                rem = re.search(r'SHIP DATE\s*[I|/]\s*INVOICE DATE\s*\n\s*(\d{1,2}/\d{1,2}.\d{4})\s*[I|/]\s*(\d{1,2}/\d{1,2}.\d{4})', text, re.IGNORECASE)
                if rem:
                    date = parsear_fecha(rem.group(2))
                else:
                    raw = find(r'Ship Date[^\n]*?(\d{1,2}/\d{2,4}\d{4})', text, flags=re.IGNORECASE)
                    if raw:
                        date = parsear_fecha(raw)

    bl_number = find(r'Bill of Lading Number\s+(\d+)', text)
    account   = find(r'Account#\s+(\d+)', text)

    po_match = re.search(r'P\.O\.\s*Number\s+([^\n]+)', text, re.IGNORECASE)
    po_number = ""
    if po_match:
        po_raw = po_match.group(1).strip().split()[0]
        if not re.match(r'^(EMAIL|HTTP|WWW|PHONE|SEE)', po_raw.upper()):
            po_number = po_raw

    if not po_number:
        sref = find(r'Shipper Reference Number\s*\n([^\n]+)', text)
        if sref:
            sr = sref.strip().split()[0]
            if sr and not re.match(r'^(PHONE|EMAIL|HTTP|I/L)', sr.upper()):
                po_number = sr

    od = re.search(
        r'Origin\s*[1I/f!]\s*Destination\s+([A-Z]{2,5})\s*[1I/f!&( \\.]\s*([A-Z0-9]{2,5})',
        text, re.IGNORECASE
    )
    origin      = od.group(1) if od else ""
    destination = od.group(2) if od else ""
    # Fix OCR: M0N -> MON (zero -> O)
    if destination and re.match(r'^[A-Z0-9]{2,5}$', destination):
        destination = destination.replace('0', 'O')

    total = find(r'Total Amount Due\s+([\d,\.]+)', text)
    if not total:
        total = find(r'PLEASE PAY THIS AMOUNT\s+([\d,\.]+)', text)

    fuel = find(r'FUEL SURCHG\s+LTL\s+SHPT\s+[\d\.]+%\s+([\d,\.]+)', text)

    # FIX 2: search "Earned Discount" WITHOUT re.DOTALL to avoid capturing
    # the ".571" (multiplier factor) that appears earlier in the text
    discount = find(r'Earned Discount\s*\n([\d,\.]+)', text, flags=re.IGNORECASE)
    if not discount:
        discount = find(r'Earned Discount\s+([\d,\.]+)', text, flags=re.IGNORECASE)
    discount = discount.rstrip('.')  # OCR adds a trailing dot: "48.01."

    wt_match = re.search(
        r'Totals?\s*[I|/]?\s*Amount\s+(?:Due|We)\s+by[^\n]*\n\s*([\d,]+)\s*\n\s*([\d,\.]+)',
        text, re.IGNORECASE
    )
    weight = wt_match.group(1).replace(',', '') if wt_match else find(r'(?:WGT|WT\(LBS\))\s*\n(\d{3,5})\b', text)

    shipper = find(r'Terms\s+(?:PREPAID|COLLECT)\s*\n([A-Z][A-Z &]+(?:INC|PRODUCTS?|CORP)[^\n]*)', text)
    if not shipper:
        shipper = find(r'Shipper\s*\n[^\n]*\n([A-Z][A-Z &]+)\n', text)

    # Consignee: "Consignee \n Bill To I Payment Due From \n Account# ... \n FLEXTEC CORP"
    consignee = find(r'Consignee\s*\n(?:Bill To[^\n]*\n)?(?:Account#[^\n]*\n)?([A-Z][A-Z &\-\.]+)\n', text)
    if not consignee:
        consignee = find(r'Consignee\s*\n([A-Z][A-Z &,\.]+)\n', text)

    terms = find(r'Terms\s+(PREPAID|COLLECT)', text)
    if not terms:
        terms = find(r'TERMS\s*\n(PREPAID|COLLECT)', text)

    # Payment due date: "Payment Due Date 11/15/2025" or "1111512025"
    payment_due = find(r'Payment Due Date\s+(\d{1,2}/\d{1,2}/\d{4})', text)
    if not payment_due:
        # Corrupt OCR: "1111512025" -> try to parse
        raw_due = find(r'Payment Due Date\s+(\d{7,10})', text)
        if raw_due:
            payment_due = parsear_fecha(raw_due)
    if not payment_due:
        payment_due = find(r'PAYMENT DUE DATE\s*\n(\d{1,2}/\d{1,2}/\d{4})', text)

    pro_number = find(r'I/L PRO Number\s*\n([^\n]+)', text)
    if pro_number:
        pro_number = pro_number.strip()
        if re.match(r'^(PHONE|EMAIL|SHIPPER)', pro_number.upper()):
            pro_number = ""

    # Zip codes: FedEx layout has shipper address before "Origin" line
    # "PLYMOUTH MN 55447-1907 \nTotal Amount Due"
    shipper_zip = find(r'[A-Z]+\s+[A-Z]{2}\s+(\d{5}[\.\-]\d{4})\s*\nTotal Amount', text)
    if not shipper_zip:
        shipper_zip = find(r'[A-Z]+\s+[A-Z]{2}\s+(\d{5}[\.\-]\d{4})\s*\n.*?Origin', text, flags=re.DOTALL)
    if not shipper_zip:
        shipper_zip = find(r'[A-Z]{2}\s+(\d{5})\s*\nTotal Amount', text)
    if shipper_zip:
        shipper_zip = shipper_zip.replace('.', '-')

    # Consignee zip: after "Consignee" keyword, the address with zip
    # Look for the LAST zip before "CHA" or "PIECES" (charges section)
    cons_section = re.search(r'Consignee\s*\n(.*?)(?:CHA|PIECES|PALLETS)', text, re.DOTALL)
    consignee_zip = ""
    if cons_section:
        # Find all zips in consignee section, take the last one (closest to charges)
        zips = re.findall(r'[A-Z]{2}\s+(\d{5}[\.\-]\d{4})', cons_section.group(1))
        if zips:
            consignee_zip = zips[-1].replace('.', '-')
        else:
            zips = re.findall(r'[A-Z]{2}\s+(\d{5})\b', cons_section.group(1))
            if zips:
                consignee_zip = zips[-1]

    # Freight class
    freight_class = extract_freight_class(text)

    return {
        "date":           date,
        "invoice_no":     invoice_no,
        "pro_number":     pro_number,
        "po_number":      po_number,
        "bl_number":      bl_number,
        "biller":         "",
        "accts_rec":      account,
        "due_amount":     total,
        "total_charges":  total,
        "fuel_surcharge": fuel,
        "discount":       discount,
        "origin":         origin,
        "destination":    destination,
        "weight":         weight,
        "shipper_name":   shipper,
        "shipper_zip":    shipper_zip,
        "consignee_name": consignee,
        "consignee_zip":  consignee_zip,
        "freight_class":  freight_class,
        "payment_terms":  terms,
        "payment_due_date": payment_due,
        "charges_detail": extract_charges_fedex(text),
    }


# ── OTHER ──────────────────────────────────────────────────────────────────────
def extract_other(text):
    return {
        "date":           find(r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b', text),
        "invoice_no":     find(r'(?:invoice|freight bill|pro)\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9\-]+)', text),
        "pro_number":     find(r'PRO\s*(?:Number|#)?\s*[:\-]?\s*(\d{6,})', text),
        "po_number":      find(r'P\.?O\.?\s*(?:Number|#)?\s*[:\-]?\s*([A-Z0-9\-]+)', text),
        "bl_number":      find(r'B\.?(?:O\.?)?L\.?\s*(?:Number|#)?\s*[:\-]?\s*([A-Z0-9\-]+)', text),
        "biller":         "",
        "accts_rec":      "",
        "due_amount":     find(r'(?:amount due|pay this amount|total due)\s*\$?\s*([\d,\.]+)', text),
        "total_charges":  find(r'(?:total charges?|amount due)\s*\$?\s*([\d,\.]+)', text),
        "fuel_surcharge": find(r'fuel\s*sur(?:charge)?\s*\$?\s*([\d,\.]+)', text),
        "discount":       find(r'discount\s*\$?\s*([\d,\.]+)', text),
        "origin":         "",
        "destination":    "",
        "weight":         find(r'\b(\d{3,5})\s*(?:lbs?|LBS?)\b', text),
        "shipper_name":   "",
        "shipper_zip":    "",
        "consignee_name": "",
        "consignee_zip":  "",
        "freight_class":  "",
        "payment_terms":  "",
        "payment_due_date": "",
    }


# ── Pipeline ───────────────────────────────────────────────────────────────────
EXTRACTORS = {
    "SAIA":       extract_saia,
    "DAYTON":     extract_dayton,
    "AAA_COOPER": extract_aaa_cooper,
    "FEDEX":      extract_fedex,
    "OTHER":      extract_other,
}

FIELDS = ALL_FIELDS


def calc_confidence(carrier, extracted):
    """Calcula confianza basada en cuantos campos criticos se extrajeron."""
    fields = CRITICAL_FIELDS.get(carrier, CRITICAL_FIELDS["OTHER"])
    filled = sum(1 for f in fields if extracted.get(f))
    ratio = filled / len(fields)
    if ratio >= 0.8:
        return "HIGH"
    elif ratio >= 0.5:
        return "MEDIUM"
    return "LOW"


def process_pdf(pdf_path):
    row = {f: "" for f in FIELDS}
    row["filename"] = pdf_path.name
    try:
        doc = fitz.open(str(pdf_path))
        row["pages"] = str(len(doc))
        # Mejora 4: leer TODAS las paginas, no solo la primera
        text = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        row["error"] = str(e)
        row["extraction_confidence"] = "LOW"
        return row

    carrier = detect_carrier(text)
    row["carrier"] = carrier
    # Mejora 3: pasar doc a SAIA para extraccion por coordenadas
    if carrier == "SAIA":
        extracted = extract_saia(text, doc=doc)
    else:
        extracted = EXTRACTORS[carrier](text)
    doc.close()
    # Validar rangos de montos y peso
    extracted = sanitize_extracted(extracted)
    row.update(extracted)
    # Mejora 2: calcular confianza
    row["extraction_confidence"] = calc_confidence(carrier, extracted)

    # ── Manual overrides for known OCR errors ──────────────────────────────────
    MANUAL_OVERRIDES = {
        "WC_AP006_001_of_003_951_20251022111505.pdf": {"date": "10/16/25"},
        "WC_AP003_001_of_001_595_20251003122658.pdf": {"date": "10/03/2025"},
    }
    if pdf_path.name in MANUAL_OVERRIDES:
        row.update(MANUAL_OVERRIDES[pdf_path.name])

    return row


def save_db(rows, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=10000")

    # ── Main invoices table ────────────────────────────────────────────────────
    conn.execute("DROP TABLE IF EXISTS invoices")
    cols = ", ".join(f'"{f}" TEXT' for f in FIELDS)
    conn.execute(f"CREATE TABLE invoices ({cols})")
    for idx in ["carrier", "date", "po_number", "bl_number", "pro_number", "invoice_no"]:
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{idx} ON invoices("{idx}")')
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_invoice
        ON invoices(carrier, invoice_no)
        WHERE invoice_no != ''
    """)
    ph = ", ".join("?" for _ in FIELDS)
    data = [[row.get(f, "") for f in FIELDS] for row in rows]
    conn.executemany(f"INSERT OR REPLACE INTO invoices VALUES ({ph})", data)

    # ── Charges detail table (one row per charge line item) ────────────────────
    conn.execute("DROP TABLE IF EXISTS invoice_charges")
    conn.execute("""
        CREATE TABLE invoice_charges (
            filename TEXT,
            carrier TEXT,
            invoice_no TEXT,
            date TEXT,
            description TEXT,
            amount REAL,
            code TEXT,
            weight TEXT,
            rate TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_filename ON invoice_charges(filename)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_carrier ON invoice_charges(carrier)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_charges_desc ON invoice_charges(description)")

    charges_data = []
    for row in rows:
        charges_str = row.get("charges_detail", "")
        if not charges_str:
            continue
        try:
            charges = json.loads(charges_str)
            for c in charges:
                amt_str = c.get("amount", "0").replace(",", "")
                try:
                    amt = float(amt_str)
                except ValueError:
                    amt = 0.0
                charges_data.append((
                    row.get("filename", ""),
                    row.get("carrier", ""),
                    row.get("invoice_no", ""),
                    row.get("date", ""),
                    c.get("description", ""),
                    amt,
                    c.get("code", ""),
                    c.get("weight", ""),
                    c.get("rate", ""),
                ))
        except (json.JSONDecodeError, TypeError):
            pass

    conn.executemany(
        "INSERT INTO invoice_charges VALUES (?,?,?,?,?,?,?,?,?)",
        charges_data
    )

    conn.commit()
    conn.close()


def save_charges_csv(rows, csv_path):
    """Save a flat CSV with one row per charge line item."""
    charge_fields = ["filename", "carrier", "invoice_no", "date", "description", "amount", "code", "weight", "rate"]
    charge_rows = []
    for row in rows:
        charges_str = row.get("charges_detail", "")
        if not charges_str:
            continue
        try:
            charges = json.loads(charges_str)
            for c in charges:
                charge_rows.append({
                    "filename": row.get("filename", ""),
                    "carrier": row.get("carrier", ""),
                    "invoice_no": row.get("invoice_no", ""),
                    "date": row.get("date", ""),
                    "description": c.get("description", ""),
                    "amount": c.get("amount", ""),
                    "code": c.get("code", ""),
                    "weight": c.get("weight", ""),
                    "rate": c.get("rate", ""),
                })
        except (json.JSONDecodeError, TypeError):
            pass

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=charge_fields)
        writer.writeheader()
        writer.writerows(charge_rows)
    return len(charge_rows)


def save_json(rows, json_path):
    """Save all invoice data as JSON, with charges_detail parsed as objects."""
    output = []
    for row in rows:
        obj = {k: v for k, v in row.items() if k != "charges_detail"}
        # Parse charges_detail from JSON string to actual list
        charges_str = row.get("charges_detail", "")
        if charges_str:
            try:
                obj["charges"] = json.loads(charges_str)
            except (json.JSONDecodeError, TypeError):
                obj["charges"] = []
        else:
            obj["charges"] = []
        output.append(obj)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    return len(output)


def get_already_processed(db_path):
    """Returns set of filenames already processed in the DB."""
    if not Path(db_path).exists():
        return set()
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT filename FROM invoices").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def main():
    parser = argparse.ArgumentParser(description="Extract data from invoice PDFs")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess all PDFs (ignore cache)")
    args = parser.parse_args()

    pdfs_all = sorted(Path(PDF_DIR).glob("*.pdf"))
    if not pdfs_all:
        print(f"No PDFs found in {PDF_DIR}")
        return

    # Incremental processing: only new PDFs
    if args.force:
        pdfs = pdfs_all
        print(f"--force mode: reprocessing {len(pdfs)} PDFs...")
    else:
        already_done = get_already_processed(DB_OUT)
        pdfs = [p for p in pdfs_all if p.name not in already_done]
        if not pdfs:
            print(f"All {len(pdfs_all)} PDFs are already processed.")
            print("Use --force to reprocess all.")
            return
        print(f"Total PDFs: {len(pdfs_all)} | Already in DB: {len(already_done)} | New: {len(pdfs)}")

    print(f"Processing {len(pdfs)} PDFs...")
    rows = [process_pdf(p) for p in tqdm(pdfs, unit="pdf")]

    # If incremental, load existing rows from CSV for the full export
    all_rows = rows
    if not args.force and Path(CSV_OUT).exists():
        existing = {}
        with open(CSV_OUT, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing[r["filename"]] = r
        # Update with new ones
        for r in rows:
            existing[r["filename"]] = r
        all_rows = list(existing.values())

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    save_db(all_rows, DB_OUT)
    n_charges = save_charges_csv(all_rows, CSV_CHARGES)
    save_json(all_rows, JSON_OUT)

    from collections import Counter
    carriers = Counter(r["carrier"] for r in all_rows)
    errors   = [r for r in rows if r["error"]]
    confidence = Counter(r["extraction_confidence"] for r in all_rows)

    print(f"\nDone:")
    print(f"  CSV      : {CSV_OUT}")
    print(f"  Charges  : {CSV_CHARGES} ({n_charges} line items)")
    print(f"  JSON     : {JSON_OUT}")
    print(f"  DB       : {DB_OUT} (tables: invoices, invoice_charges)")
    print(f"  Total    : {len(all_rows)} invoices")
    if len(rows) != len(all_rows):
        print(f"  New: {len(rows)}")
    print(f"\nCarriers detected:")
    for c, n in carriers.most_common():
        print(f"  {c}: {n}")
    print(f"\nExtraction confidence:")
    for level in ("HIGH", "MEDIUM", "LOW"):
        print(f"  {level}: {confidence.get(level, 0)}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for r in errors[:5]:
            print(f"  {r['filename']}: {r['error']}")

    print("\n--- Sample per carrier ---")
    seen = set()
    for row in rows:
        c = row["carrier"]
        if c not in seen:
            seen.add(c)
            print(f"\n[{c}] {row['filename']}")
            for k in FIELDS:
                if k not in ("filename", "carrier", "pages", "error", "shipper_name", "consignee_name") and row.get(k):
                    print(f"  {k:16}: {row[k]}")


if __name__ == "__main__":
    main()
