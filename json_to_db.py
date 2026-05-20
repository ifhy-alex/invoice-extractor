"""
json_to_db.py
Convierte los JSONs generados por marker a filas en una base de datos SQLite.
Adapta la conexión a PostgreSQL/MySQL cambiando la sección de conexión.

Uso:
    py -3 json_to_db.py

Requisitos:
    pip install tqdm
"""

import json
import os
import sqlite3
from pathlib import Path
from tqdm import tqdm


# ── Configuración ──────────────────────────────────────────────────────────────
JSON_DIR = r"C:\Users\alexf\OneDrive\Escritorio\invoices_json"
DB_PATH  = r"C:\Users\alexf\OneDrive\Escritorio\invoices.db"
# ──────────────────────────────────────────────────────────────────────────────


def get_text_from_html(html: str) -> str:
    """Extrae texto plano básico del HTML de un bloque."""
    import re
    # Elimina content-ref y tags HTML
    text = re.sub(r"<content-ref[^>]*>.*?</content-ref>", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()


def flatten_blocks(page: dict, doc_name: str) -> list[dict]:
    """Aplana recursivamente los bloques de una página."""
    rows = []
    page_id = page.get("id", "")

    def recurse(block: dict, depth: int = 0):
        block_type = block.get("block_type", "")
        html = block.get("html", "") or ""
        text = get_text_from_html(html)
        polygon = block.get("polygon") or block.get("bbox")

        rows.append({
            "doc_name":   doc_name,
            "page_id":    page_id,
            "block_id":   block.get("id", ""),
            "block_type": block_type,
            "text":       text,
            "html":       html,
            "polygon":    json.dumps(polygon) if polygon else None,
            "depth":      depth,
        })

        for child in block.get("children") or []:
            recurse(child, depth + 1)

    for child in page.get("children") or []:
        recurse(child)

    return rows


def create_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_name   TEXT UNIQUE NOT NULL,
            page_count INTEGER,
            imported_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_name   TEXT NOT NULL,
            page_id    TEXT,
            block_id   TEXT,
            block_type TEXT,
            text       TEXT,
            html       TEXT,
            polygon    TEXT,
            depth      INTEGER,
            FOREIGN KEY (doc_name) REFERENCES documents(doc_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_doc ON blocks(doc_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_type ON blocks(block_type)")
    conn.commit()


def import_json(json_path: Path, conn: sqlite3.Connection):
    doc_name = json_path.stem  # nombre del archivo sin extensión

    # Saltar si ya fue importado
    existing = conn.execute(
        "SELECT 1 FROM documents WHERE doc_name = ?", (doc_name,)
    ).fetchone()
    if existing:
        return 0

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    pages = data.get("children", [])
    all_rows = []
    for page in pages:
        all_rows.extend(flatten_blocks(page, doc_name))

    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_name, page_count) VALUES (?, ?)",
        (doc_name, len(pages))
    )
    conn.executemany(
        """INSERT INTO blocks
           (doc_name, page_id, block_id, block_type, text, html, polygon, depth)
           VALUES (:doc_name, :page_id, :block_id, :block_type, :text, :html, :polygon, :depth)
        """,
        all_rows
    )
    conn.commit()
    return len(all_rows)


def main():
    json_dir = Path(JSON_DIR)
    if not json_dir.exists():
        print(f"ERROR: No existe la carpeta {JSON_DIR}")
        print("Asegurate de haber ejecutado marker primero.")
        return

    # marker guarda cada JSON en una subcarpeta con el mismo nombre que el PDF
    json_files = list(json_dir.rglob("*.json"))
    # Excluir archivos _meta.json que genera marker
    json_files = [f for f in json_files if not f.name.endswith("_meta.json")]

    if not json_files:
        print(f"No se encontraron archivos JSON en {JSON_DIR}")
        return

    print(f"Encontrados {len(json_files)} JSONs. Importando a {DB_PATH}...")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)

    total_blocks = 0
    errors = []

    for json_path in tqdm(json_files, unit="doc"):
        try:
            total_blocks += import_json(json_path, conn)
        except Exception as e:
            errors.append((json_path.name, str(e)))

    conn.close()

    print(f"\nImportados {total_blocks:,} bloques de {len(json_files)} documentos.")
    print(f"  Base de datos: {DB_PATH}")

    if errors:
        print(f"\n{len(errors)} errores:")
        for name, err in errors[:10]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
