"""Importa referencias observadas de CX-5 desde CSV; no las marca como verificadas."""
from __future__ import annotations
import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db import DEFAULT_DB, connect_db, init_db

CSV_PATH = ROOT / "data" / "referencias_cx5_2024_observadas.csv"

def main():
    init_db(DEFAULT_DB, seed=False)
    conn = connect_db(DEFAULT_DB)
    added = skipped = 0
    try:
        with conn:
            with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    version = conn.execute("""
                        SELECT v.id FROM vehicle_versions v
                        JOIN models m ON m.id=v.model_id
                        JOIN brands b ON b.id=m.brand_id
                        WHERE b.name=? AND m.name=? AND v.model_year=?
                          AND v.version_name=?
                    """, (row["brand"], row["model"], int(row["model_year"]), row["version_name"])).fetchone()
                    if not version:
                        raise RuntimeError(f"No se encontró versión de catálogo para {row['brand']} {row['model']} {row['model_year']} {row['version_name']}")
                    source = conn.execute("SELECT id FROM sources WHERE name=?", (row["source_name"],)).fetchone()
                    if source:
                        source_id = source["id"]
                    else:
                        from urllib.parse import urlparse
                        domain = row["source_domain"]
                        conn.execute("""INSERT INTO sources(name,domain,source_type,base_url,usage_notes,last_checked_at)
                            VALUES(?,?,?,?,?,?)""", (
                            row["source_name"], domain, "anuncios_publicos",
                            "https://" + domain + "/",
                            "Precio solicitado de anuncio público; no es transacción efectiva. La referencia se conserva como no verificada o expirada hasta comprobar disponibilidad y condiciones de uso.",
                            row["observed_at"]))
                        source_id = conn.execute("SELECT id FROM sources WHERE name=?", (row["source_name"],)).fetchone()["id"]
                    exists = conn.execute("""SELECT id FROM price_references
                        WHERE version_id=? AND source_url=? AND price_cop=? AND observed_at=?""",
                        (version["id"], row["source_url"], int(row["price_cop"]), row["observed_at"])).fetchone()
                    if exists:
                        skipped += 1
                        continue
                    conn.execute("""INSERT INTO price_references
                        (version_id,source_id,price_cop,price_type,model_year,mileage_km,location,source_url,observed_at,verification_status,notes)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                        version["id"], source_id, int(row["price_cop"]), row["price_type"],
                        int(row["model_year"]), int(row["mileage_km"]), row["location"],
                        row["source_url"], row["observed_at"], row["verification_status"], row["notes"]))
                    price_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute("""INSERT INTO audit_log(action,entity_type,entity_id,details)
                        VALUES('import_price_reference','price_reference',?,?)""",
                        (price_id, f"Importado desde CSV; estado={row['verification_status']}; fuente={row['source_url']}"))
                    added += 1
        print(f"Referencias añadidas: {added}; duplicadas omitidas: {skipped}")
    finally:
        conn.close()

if __name__ == "__main__":
    import csv
    main()
