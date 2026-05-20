"""
validate_invoices.py
Validacion de coherencia interna de los datos extraidos.
NO depende de buscar texto en el PDF — valida formatos y aritmetica.

Validaciones:
  1. Formato: cada campo cumple su patron esperado
  2. Aritmetica: los montos cuadran entre si
  3. Rangos: valores dentro de limites razonables

Uso:
    py -3 validate_invoices.py
"""

import csv
import re
from pathlib import Path
from collections import Counter, defaultdict

CSV_IN = r"C:\Users\alexf\OneDrive\Escritorio\invoices.csv"


def to_float(val):
    """Convierte string a float, retorna None si no es posible."""
    if not val:
        return None
    try:
        return float(val.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def load_csv():
    with open(CSV_IN, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── VALIDACION 1: Formato ─────────────────────────────────────────────────────

FORMAT_RULES = {
    "date": {
        "pattern": r'^\d{1,2}/\d{1,2}/\d{2,4}$',
        "desc": "MM/DD/YY o MM/DD/YYYY",
    },
    "invoice_no": {
        "pattern": r'^\d{6,12}$',
        "desc": "6-12 digitos",
    },
    "pro_number": {
        "pattern": r'^\d{6,12}$',
        "desc": "6-12 digitos",
    },
    "bl_number": {
        "pattern": r'^\d{4,15}$',
        "desc": "4-15 digitos (incluye numeros de cuenta collect)",
    },
    "accts_rec": {
        "pattern": r'^\d{4,10}$',
        "desc": "4-10 digitos",
    },
    "due_amount": {
        "pattern": r'^[\d,]+\.?\d*$',
        "desc": "numero decimal",
    },
    "total_charges": {
        "pattern": r'^[\d,]+\.?\d*$',
        "desc": "numero decimal",
    },
    "fuel_surcharge": {
        "pattern": r'^[\d,]+\.?\d*$',
        "desc": "numero decimal",
    },
    "origin": {
        "pattern": r'^[A-Z]{2,6}$',
        "desc": "2-6 letras mayusculas (codigo terminal)",
    },
    "destination": {
        "pattern": r'^[A-Z]{2,6}$',
        "desc": "2-6 letras mayusculas (codigo terminal)",
    },
    "weight": {
        "pattern": r'^\d{2,5}$',
        "desc": "2-5 digitos",
    },
}


def validate_format(row):
    """Retorna lista de (campo, valor, problema) para campos con formato invalido."""
    issues = []
    for field, rule in FORMAT_RULES.items():
        val = row.get(field, "")
        if not val:
            continue  # campo vacio no es error de formato
        if not re.match(rule["pattern"], val):
            issues.append((field, val, f"No cumple formato: {rule['desc']}"))
    return issues


# ── VALIDACION 2: Aritmetica ──────────────────────────────────────────────────

def validate_arithmetic(row):
    """Verifica que los montos cuadren entre si."""
    issues = []
    carrier = row.get("carrier", "")

    due     = to_float(row.get("due_amount"))
    total   = to_float(row.get("total_charges"))
    fuel    = to_float(row.get("fuel_surcharge"))
    disc    = to_float(row.get("discount"))

    # SAIA: due_amount deberia = total_charges (son el mismo campo en SAIA)
    if carrier == "SAIA" and due is not None and total is not None:
        if abs(due - total) > 0.02:
            issues.append(("due vs total", f"due={due} total={total}", "due_amount != total_charges"))

    # DAYTON: due_amount = total_charges - discount + fuel
    # Solo aplica si discount es un monto real (>$1), no un rate como ".8180"
    if carrier == "DAYTON" and due is not None and total is not None and fuel is not None:
        if disc is not None and disc > 1:
            expected = total - disc + fuel
            if abs(due - expected) > 0.50:
                issues.append(("aritmetica", f"due={due} expected={expected:.2f} (total={total} - disc={disc} + fuel={fuel})",
                              "due != total - discount + fuel"))

    # Fuel no puede ser mayor que el total
    if fuel is not None and total is not None and fuel > total and carrier != "FEDEX":
        issues.append(("fuel > total", f"fuel={fuel} total={total}", "fuel_surcharge > total_charges"))

    # Discount no puede ser mayor que el total (si es un monto, no un rate)
    # Excluir SAIA: su discount es sobre el rate bruto, no sobre total_charges
    if disc is not None and disc > 1 and total is not None and disc > total * 2:
        if carrier not in ("SAIA",):
            issues.append(("disc > 2x total", f"disc={disc} total={total}", "discount > 2x total_charges"))

    return issues


# ── VALIDACION 3: Rangos razonables ───────────────────────────────────────────

def validate_ranges(row):
    """Verifica que los valores esten en rangos razonables para freight."""
    issues = []

    # Peso: 10 - 20,000 lbs es razonable para LTL freight
    weight = to_float(row.get("weight"))
    if weight is not None:
        if weight < 10:
            issues.append(("weight", str(weight), "Peso < 10 lbs (sospechoso)"))
        elif weight > 20000:
            issues.append(("weight", str(weight), "Peso > 20,000 lbs (sospechoso)"))

    # Monto: $1 - $50,000 es razonable
    due = to_float(row.get("due_amount"))
    if due is not None:
        if due < 1:
            issues.append(("due_amount", str(due), "Monto < $1 (sospechoso)"))
        elif due > 50000:
            issues.append(("due_amount", str(due), "Monto > $50,000 (sospechoso)"))

    # Fecha: verificar que mes y dia sean validos
    date = row.get("date", "")
    if date:
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', date)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            if month < 1 or month > 12:
                issues.append(("date", date, f"Mes invalido: {month}"))
            if day < 1 or day > 31:
                issues.append(("date", date, f"Dia invalido: {day}"))

    # Fuel surcharge: tipicamente $1 - $500 para LTL
    fuel = to_float(row.get("fuel_surcharge"))
    if fuel is not None:
        if fuel > 500:
            issues.append(("fuel_surcharge", str(fuel), "Fuel > $500 (sospechoso)"))

    return issues


# ── VALIDACION 4: Campos obligatorios por carrier ─────────────────────────────

REQUIRED_FIELDS = {
    "SAIA":       ["date", "invoice_no", "biller", "accts_rec", "due_amount"],
    "DAYTON":     ["date", "invoice_no", "due_amount", "origin"],
    "FEDEX":      ["due_amount"],
    "AAA_COOPER": ["date", "due_amount", "origin"],
}


def validate_required(row):
    """Verifica que los campos obligatorios para el carrier esten presentes."""
    issues = []
    carrier = row.get("carrier", "")
    required = REQUIRED_FIELDS.get(carrier, [])
    for field in required:
        if not row.get(field, ""):
            issues.append((field, "", f"Campo obligatorio vacio para {carrier}"))
    return issues


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rows = load_csv()
    print(f"Validando {len(rows)} facturas...\n")

    all_issues = defaultdict(list)  # filename -> [(tipo, campo, valor, problema)]
    format_fails = Counter()
    arith_fails = Counter()
    range_fails = Counter()
    required_fails = Counter()

    for row in rows:
        fn = row["filename"]
        carrier = row.get("carrier", "")

        for field, val, prob in validate_format(row):
            all_issues[fn].append(("FORMATO", field, val, prob))
            format_fails[(carrier, field)] += 1

        for field, val, prob in validate_arithmetic(row):
            all_issues[fn].append(("ARITMETICA", field, val, prob))
            arith_fails[(carrier, field)] += 1

        for field, val, prob in validate_ranges(row):
            all_issues[fn].append(("RANGO", field, val, prob))
            range_fails[(carrier, field)] += 1

        for field, val, prob in validate_required(row):
            all_issues[fn].append(("REQUERIDO", field, val, prob))
            required_fails[(carrier, field)] += 1

    # Resumen
    total_issues = sum(len(v) for v in all_issues.values())
    files_with_issues = len(all_issues)
    files_clean = len(rows) - files_with_issues

    print("=" * 70)
    print(f"  RESUMEN DE VALIDACION")
    print("=" * 70)
    print(f"  Total facturas:       {len(rows)}")
    print(f"  Sin problemas:        {files_clean} ({100*files_clean/len(rows):.1f}%)")
    print(f"  Con algun problema:   {files_with_issues}")
    print(f"  Total issues:         {total_issues}")
    print()

    # Desglose por tipo
    print("--- Por tipo de validacion ---")
    print(f"  Formato invalido:     {sum(format_fails.values())}")
    print(f"  Aritmetica no cuadra: {sum(arith_fails.values())}")
    print(f"  Fuera de rango:       {sum(range_fails.values())}")
    print(f"  Campo requerido vacio:{sum(required_fails.values())}")
    print()

    # Top problemas de formato
    if format_fails:
        print("--- Top problemas de FORMATO ---")
        for (carrier, field), count in format_fails.most_common(10):
            print(f"  [{carrier}] {field}: {count} casos")
        print()

    # Problemas de aritmetica
    if arith_fails:
        print("--- Problemas de ARITMETICA ---")
        for (carrier, field), count in arith_fails.most_common(10):
            print(f"  [{carrier}] {field}: {count} casos")
        print()

    # Problemas de rango
    if range_fails:
        print("--- Problemas de RANGO ---")
        for (carrier, field), count in range_fails.most_common(10):
            print(f"  [{carrier}] {field}: {count} casos")
        print()

    # Campos requeridos vacios
    if required_fails:
        print("--- Campos REQUERIDOS vacios ---")
        for (carrier, field), count in required_fails.most_common(10):
            print(f"  [{carrier}] {field}: {count} casos")
        print()

    # Ejemplos de problemas
    if all_issues:
        print("--- Ejemplos (primeros 15) ---")
        shown = 0
        for fn, issues in sorted(all_issues.items()):
            if shown >= 15:
                break
            carrier = next((r["carrier"] for r in rows if r["filename"] == fn), "?")
            for tipo, field, val, prob in issues:
                if shown >= 15:
                    break
                print(f"  [{carrier}] {fn}")
                print(f"    {tipo} | {field} = {val!r} | {prob}")
                shown += 1
        print()

    # Veredicto final
    print("=" * 70)
    if total_issues == 0:
        print("  VEREDICTO: Todos los datos pasan validacion.")
    elif files_with_issues < len(rows) * 0.05:
        print(f"  VEREDICTO: {100*files_clean/len(rows):.1f}% de facturas sin problemas.")
        print(f"  Los {files_with_issues} con issues son probablemente PDFs con formato atipico.")
    else:
        print(f"  VEREDICTO: {files_with_issues} facturas con problemas. Revisar patrones.")
    print("=" * 70)


if __name__ == "__main__":
    main()
