"""Migra el schema y (opcionalmente) los datos de la SQLite local a Turso.

Uso:
    python migrate_to_turso.py \\
        --url libsql://cavalletto-admin-<user>.turso.io \\
        --token eyJhbG... \\
        [--copy-from ../agente-administrativo/db/cavalletto_admin.db]

Aplica:
1. Schema base (schema.sql del skill).
2. ALTER TABLE items_inventario ADD COLUMN foto_blob BLOB / foto_mime TEXT.
3. CREATE TABLE facturas.
4. Si --copy-from, vuelca filas de tablas core (sin BLOBs) a Turso.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Importamos el adapter HTTP del modulo db.py para reutilizar logica
sys.path.insert(0, str(Path(__file__).parent))
from db import _TursoHTTPConnection, _normalize_turso_url  # noqa: E402


SCHEMA_PATH = Path(__file__).parent.parent / "agente-administrativo" / "db" / "schema.sql"

EXTRA_DDL = [
    # Columnas nuevas para fotos (ALTER sin IF NOT EXISTS en SQLite; tolerar error duplicado)
    "ALTER TABLE items_inventario ADD COLUMN foto_blob BLOB",
    "ALTER TABLE items_inventario ADD COLUMN foto_mime TEXT",
    # Tabla facturas
    """CREATE TABLE IF NOT EXISTS facturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_archivo TEXT NOT NULL,
        mime TEXT NOT NULL,
        archivo_blob BLOB NOT NULL,
        fecha_subida TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        estatus TEXT DEFAULT 'pendiente' CHECK(estatus IN ('pendiente', 'procesada', 'rechazada')),
        gasto_id INTEGER REFERENCES gastos(id),
        monto_extraido REAL,
        fecha_extraida DATE,
        rfc_emisor TEXT,
        uuid_fiscal TEXT,
        notas TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_facturas_estatus ON facturas(estatus)",
]

# Tablas a copiar en orden (respeta FKs). Sin movimientos/pagos — decision
# explicita: solo llevamos catalogos y estructura. Si quieres historial, pasa
# --with-history.
CORE_TABLES = [
    "familias", "alumnos",
    "conceptos_cobro",
    "categorias_gasto", "proveedores",
    "items_inventario",
    "empleados",
    "presupuesto_mensual",
]

HISTORY_TABLES = [
    "cargos", "pagos",
    "gastos",
    "movimientos_inventario",
    "pagos_nomina",
]


def _split_statements(sql_text):
    """Divide un archivo SQL en statements individuales respetando ;."""
    stmts = []
    buf = []
    for line in sql_text.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmts.append("\n".join(buf).strip().rstrip(";"))
            buf = []
    if buf:
        tail = "\n".join(buf).strip().rstrip(";")
        if tail:
            stmts.append(tail)
    return stmts


def apply_schema(turso):
    print("→ Aplicando schema base...")
    sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
    for stmt in _split_statements(sql_text):
        try:
            turso.execute(stmt)
        except Exception as e:
            print(f"  ⚠️ stmt fallo (continuo): {e}")
    print("→ Aplicando DDL extra (fotos + facturas)...")
    for stmt in EXTRA_DDL:
        try:
            turso.execute(stmt)
            print(f"  ✓ {stmt[:60]}...")
        except Exception as e:
            msg = str(e).lower()
            if "duplicate" in msg or "already exists" in msg:
                print(f"  = ya existe: {stmt[:60]}...")
            else:
                print(f"  ⚠️ {e}")
    turso.commit()
    print("✓ Schema listo")


def copy_table(src, turso, table):
    """Copia filas de src (sqlite3.Connection) a turso (libsql)."""
    src.row_factory = sqlite3.Row
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  = {table}: vacia")
        return 0
    cols = rows[0].keys()
    placeholders = ",".join("?" * len(cols))
    collist = ",".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})"
    n = 0
    for r in rows:
        values = tuple(r[c] for c in cols)
        try:
            turso.execute(sql, values)
            n += 1
        except Exception as e:
            print(f"  ⚠️ {table} id={r['id'] if 'id' in cols else '?'}: {e}")
    turso.commit()
    print(f"  ✓ {table}: {n}/{len(rows)} filas")
    return n


def copy_data(src_path, turso, with_history):
    print(f"→ Copiando datos desde {src_path}...")
    src = sqlite3.connect(src_path)
    try:
        tables = CORE_TABLES + (HISTORY_TABLES if with_history else [])
        for t in tables:
            try:
                copy_table(src, turso, t)
            except Exception as e:
                print(f"  ⚠️ {t}: {e}")
    finally:
        src.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="libsql://<db>-<user>.turso.io")
    p.add_argument("--token", required=True)
    p.add_argument("--copy-from", help="Ruta a SQLite local a volcar")
    p.add_argument("--with-history", action="store_true",
                   help="Copiar tambien cargos/pagos/gastos/movimientos/nomina")
    args = p.parse_args()

    print(f"→ Conectando a Turso: {args.url}")
    turso = _TursoHTTPConnection(_normalize_turso_url(args.url), args.token)

    apply_schema(turso)

    if args.copy_from:
        copy_data(args.copy_from, turso, args.with_history)

    print("\n✅ Migracion completa")


if __name__ == "__main__":
    sys.exit(main() or 0)
