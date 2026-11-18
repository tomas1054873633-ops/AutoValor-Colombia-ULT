from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db import DEFAULT_DB, connect_db, init_db

init_db(DEFAULT_DB, seed=True)
conn = connect_db(DEFAULT_DB)
try:
    print(f"Base de datos inicializada: {DEFAULT_DB}")
    for label, sql in [
        ("Marcas", "SELECT COUNT(*) FROM brands"),
        ("Líneas/modelos", "SELECT COUNT(*) FROM models"),
        ("Versiones", "SELECT COUNT(*) FROM vehicle_versions"),
        ("Referencias de precio", "SELECT COUNT(*) FROM price_references"),
        ("Versiones sin referencias", "SELECT COUNT(*) FROM vehicle_versions v WHERE NOT EXISTS (SELECT 1 FROM price_references p WHERE p.version_id=v.id)"),
    ]:
        print(f"{label}: {conn.execute(sql).fetchone()[0]}")
finally:
    conn.close()
