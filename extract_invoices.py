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

from config import PDF_DIR, CSV_OUT, DB_OUT, ALL_FIELDS, CRITICAL_FIELDS


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
    # Short format without second /: "10/2825"
    m = re.match(r'^(\d{1,2})/(\d{2})(\d{2})$', raw)
    if m:
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
        "consignee_name": find(r'(?:CONS:|CONSIGNEE)\s*([A-Z][A-Z &]+?)(?:\n|PLANT)', text),
        "payment_terms":  "PREPAID" if "PPD" in text else "",
        "payment_due_date": "",
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
        "consignee_name": find(r'Consignee\s*\n([A-Z][A-Z &]+)\n', text),
        "payment_terms":  find(r'Terms:\s*([A-Z ]+\d*\s*DAYS?)', text),
        "payment_due_date": find(r'Invoice Date:\s*\n(\d{1,2}/\d{1,2}/\d{4})', text),
    }


# ── AAA COOPER ─────────────────────────────────────────────────────────────────
def extract_aaa_cooper(text):
    # Save original text for PO/BL (before normalizing)
    text_original = text
    # FIX 3: normalize OCR-fragmented numbers for amounts
    text = normalizar_ocr_numeros(text)

    cust_pro = re.search(r'PRO NUMBER\s*\n[^\n]*\n\s*(\d{5,})\s+(\d{6,})', text)
    customer_no = cust_pro.group(1) if cust_pro else find(r'CUSTOMER NUMBER\s*\n\s*(\d+)', text)
    pro_number  = cust_pro.group(2) if cust_pro else ""
    if not pro_number:
        pro_number = find(r'AMOUNT DUE\s*\n[^\n]*\n\s*(\d{6,})\s+\$', text)

    date = find(r'(?:WAUSAU COATED PRODUCTS[^\n]*\n[^\n]*\n)(\d{1,2}/\d{1,2}/\d{2,4})', text)
    if not date:
        date = find(r'(\d{1,2}/\d{1,2}/\d{2,4})', text)

    origin = find(r'ORIGIN\s*\n[^\n]+\n[^\n]+\n([A-Z]{2,5})\s*\n', text)
    if not origin:
        origin = find(r'ORIGIN\s*\n([A-Z]{2,5})\b', text)
    destination = find(r'\bDEST\s*\n([A-Z]{2,5})\b', text)

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
        "consignee_name": consignee_name,
        "payment_terms":  "PREPAID" if "Prepaid" in text or "*Prepaid*" in text else "",
        "payment_due_date": payment_due,
    }


# ── FEDEX ──────────────────────────────────────────────────────────────────────
def extract_fedex(text):
    invoice_no = find(r'Freight Bill Number\s+(\d{6,})', text)

    # FIX 1: parse date with OCR that omits separators
    # OCR can read "10/23/2025" as "10123120251" or "10/2312025"
    # Strategy: search for the most specific pattern first
    date = ""
    # Intento 1: well-formed date "10/23/2025"
    raw = find(r'Ship Date[^\n]*?(\d{1,2}/\d{1,2}/\d{4})\b', text, flags=re.IGNORECASE)
    if raw:
        date = raw
    else:
        # Intento 2: date with missing second / "10/2312025"
        raw = find(r'Ship Date[^\n]*?(\d{1,2}/\d{2,4}\d{4})\b', text, flags=re.IGNORECASE)
        if raw:
            date = parsear_fecha(raw)
        else:
            # Intento 3: search in the remittance section
            raw = find(r'SHIP DATE[^\n]*?(\d{1,2}/\d{1,2}/\d{4})\b', text, flags=re.IGNORECASE)
            if raw:
                date = raw
            else:
                raw = find(r'SHIP DATE[^\n]*?(\d{1,2}/\d{2,4}\d{4})\b', text, flags=re.IGNORECASE)
                if raw:
                    date = parsear_fecha(raw)

    bl_number = find(r'Bill of Lading Number\s+(\d+)', text)
    account   = find(r'Account#\s+(\d+)', text)

    po_match = re.search(r'P\.O\.\s*Number\s+([^\n]+)', text, re.IGNORECASE)
    po_number = ""
    if po_match:
        po_raw = po_match.group(1).strip().split()[0]
        if not re.match(r'^(EMAIL|HTTP|WWW|PHONE)', po_raw.upper()):
            po_number = po_raw

    if not po_number:
        sref = find(r'Shipper Reference Number\s*\n([^\n]+)', text)
        if sref:
            sr = sref.strip().split()[0]
            if sr and not re.match(r'^(PHONE|EMAIL|HTTP|I/L)', sr.upper()):
                po_number = sr

    od = re.search(
        r'Origin\s*[1I/]\s*Destination\s+([A-Z]{2,5})\s*[1I/]\s*([A-Z]{2,5})',
        text, re.IGNORECASE
    )
    origin      = od.group(1) if od else ""
    destination = od.group(2) if od else ""

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
        "consignee_name": consignee,
        "payment_terms":  terms,
        "payment_due_date": payment_due,
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
        "consignee_name": "",
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
    return row


def save_db(rows, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=10000")
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
    # executemany: ~5x mas rapido que loop de execute
    ph = ", ".join("?" for _ in FIELDS)
    data = [[row.get(f, "") for f in FIELDS] for row in rows]
    conn.executemany(f"INSERT OR REPLACE INTO invoices VALUES ({ph})", data)
    conn.commit()
    conn.close()


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

    from collections import Counter
    carriers = Counter(r["carrier"] for r in all_rows)
    errors   = [r for r in rows if r["error"]]
    confidence = Counter(r["extraction_confidence"] for r in all_rows)

    print(f"\nDone:")
    print(f"  CSV : {CSV_OUT}")
    print(f"  DB  : {DB_OUT}")
    print(f"  Total: {len(all_rows)} invoices")
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
