#!/usr/bin/env python3
"""Importa las guías de precios de Revista Motor a la base de AutoValor Pro.

Uso típico (con los CSV ya extraídos incluidos en data/revista_motor/):
    python scripts/import_revista_motor.py --dry-run
    python scripts/import_revista_motor.py

Con el PDF original (requiere pdfplumber; el PDF NO se distribuye con la aplicación):
    python scripts/import_revista_motor.py --pdf /ruta/REVISTA_MOTOR.pdf

El proceso es idempotente: ejecutarlo de nuevo no duplica vehículos ni precios.
Antes de escribir se crea una copia de seguridad en database/backups/.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "importers"))

from db import DEFAULT_DB, connect_db, init_db  # noqa: E402
from revista_import import EDITION_DATE, run_import  # noqa: E402

DATA = ROOT / "data" / "revista_motor"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_from_pdf(pdf: Path, out_dir: Path, edition: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    used = out_dir / f"revista_motor_{edition}_usados_extraido.csv"
    new = out_dir / f"revista_motor_{edition}_nuevos_extraido.csv"
    imp = ROOT / "importers"
    subprocess.run([sys.executable, str(imp / "revista_extract_usados.py"), str(pdf), str(used)], check=True)
    anchors = imp / "revista_anchors_nuevos.py"
    subprocess.run([sys.executable, str(imp / "revista_extract_nuevos.py"), str(pdf), str(new), str(anchors)], check=True)
    return used, new


def write_reports(summary: dict, report_dir: Path, tag: str) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    cols = ["severity", "issue_type", "page", "brand", "raw_description", "model_year", "raw_value", "source_ref", "detail"]
    groups = {"pendientes": "pendiente", "descartados": "descartado", "advertencias": "advertencia", "conflictos": "conflicto"}
    for name, sev in groups.items():
        rows = [r for r in summary["issues"] if r["severity"] == sev]
        with open(report_dir / f"{name}_{tag}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--pdf", help="PDF original de Revista Motor (opcional; se extrae primero)")
    ap.add_argument("--used-csv", default=str(DATA / f"revista_motor_{EDITION_DATE}_usados_extraido.csv"))
    ap.add_argument("--new-csv", default=str(DATA / f"revista_motor_{EDITION_DATE}_nuevos_extraido.csv"))
    ap.add_argument("--edition", default=EDITION_DATE, help="Fecha de la edición (AAAA-MM-DD)")
    ap.add_argument("--report-dir", default=str(ROOT / "reports" / "importaciones"))
    ap.add_argument("--dry-run", action="store_true", help="Calcula todo y deshace los cambios")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    source_file, source_hash = "", ""
    used, new = Path(args.used_csv), Path(args.new_csv)
    if args.pdf:
        pdf = Path(args.pdf)
        used, new = extract_from_pdf(pdf, DATA, args.edition)
        source_file, source_hash = pdf.name, sha256(pdf)
    else:
        source_file = f"{used.name} + {new.name}"
        source_hash = sha256(used) + "+" + sha256(new)
    for p in (used, new):
        if not p.exists():
            print(f"No existe {p}", file=sys.stderr)
            return 2

    db_path = Path(args.db)
    init_db(db_path)  # aplica migraciones y crea la base si hace falta
    if not args.dry_run and not args.no_backup and db_path.exists():
        backups = db_path.parent / "backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = backups / f"autovalor_pre_revista_motor_{stamp}.db"
        shutil.copy2(db_path, dest)
        print(f"Copia de seguridad: {dest}")

    conn = connect_db(db_path)
    try:
        summary = run_import(conn, used, new, edition=args.edition, source_file_name=source_file,
                             source_sha256=source_hash, dry_run=args.dry_run)
        if not args.dry_run:
            # Las versiones nuevas llegan con la caja «Automática» o «Sin confirmar»; la auditoría
            # aplica las mismas reglas y fichas técnicas que al resto del catálogo (idempotente).
            from transmission_audit import run_audit
            audit = run_audit(conn, apply=True, with_integrity=False)
            print("Auditoría de transmisiones:", audit["summary"]["statuses"])
    finally:
        conn.close()
    tag = ("simulacion_" if args.dry_run else "") + datetime.now().strftime("%Y%m%d_%H%M%S")
    write_reports(summary, Path(args.report_dir), tag)
    print("Antes :", summary["antes"])
    print("Después:", summary["despues"])
    for k, v in summary["contadores"].items():
        print(f"  {k}: {v}")
    print("Vehículos distintos (marca+modelo):", summary["vehiculos_distintos_marca_modelo"])
    print("Vehículos distintos sin año:", summary["vehiculos_distintos_sin_anio"])
    print("Reportes en", args.report_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
