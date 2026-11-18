"""Importa la ampliación documental con backup, deduplicación y auditoría."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog_utils import (
    find_potential_duplicate_pairs, find_version_duplicate, get_or_create_brand,
    get_or_create_model, normalize_token, vehicle_broad_key, vehicle_canonical_key,
)
from db import (
    DEFAULT_DB, DATA_DIR, _find_price_vehicle, _load_price_file, _load_vehicle_file,
    connect_db, init_db,
)


def backup_database(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.stem}_pre_catalog_expansion_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def find_brand_model(conn, brand, model):
    brand_id = None
    for row in conn.execute("SELECT id,name FROM brands"):
        if normalize_token(row["name"]) == normalize_token(brand):
            brand_id = int(row["id"])
            break
    if brand_id is None:
        return None, None
    model_id = None
    for row in conn.execute("SELECT id,name FROM models WHERE brand_id=?", (brand_id,)):
        if normalize_token(row["name"]) == normalize_token(model):
            model_id = int(row["id"])
            break
    return brand_id, model_id


def read_csv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def vehicle_candidate_report(conn, rows):
    report = []
    seen_canonical, seen_broad = set(), set()
    for index, row in enumerate(rows, start=2):
        brand, model = (row.get("brand") or "").strip(), (row.get("model") or "").strip()
        try:
            year = int(row.get("model_year", ""))
            generation = (row.get("generation") or "").strip() or None
            version = (row.get("version_name") or "").strip()
            engine = (row.get("engine_label") or "").strip()
            fuel = (row.get("fuel") or "").strip() or None
            transmission = (row.get("transmission") or "").strip()
            if not all([brand, model, version, engine, transmission]) or not 1950 <= year <= 2100:
                raise ValueError("Campos obligatorios incompletos o año fuera de rango.")
            canonical = vehicle_canonical_key(brand, model, year, generation, version, engine, fuel, transmission)
            broad = vehicle_broad_key(brand, model, year, version, engine, fuel, transmission)
            if canonical in seen_canonical:
                status, detail = "duplicado_exacto", "Duplicado exacto dentro del archivo de ampliación."
            elif broad in seen_broad:
                status, detail = "posible_duplicado", "Coincidencia sin generación; requiere revisión."
            else:
                _, model_id = find_brand_model(conn, brand, model)
                values = {
                    "model_year": year, "generation": generation, "version_name": version,
                    "engine_label": engine, "fuel": fuel, "transmission": transmission,
                }
                duplicate_type, duplicate = find_version_duplicate(conn, brand, model, model_id, values) if model_id else (None, None)
                if duplicate_type == "exacto":
                    status, detail = "duplicado_exacto", f"Ya existe la versión ID {duplicate['id']}."
                elif duplicate_type == "posible":
                    status, detail = "posible_duplicado", f"Posible duplicado del ID {duplicate['id']}."
                else:
                    status, detail = "candidato_nuevo", "No se encontró coincidencia en los registros existentes."
            seen_canonical.add(canonical)
            seen_broad.add(broad)
        except Exception as exc:
            status, detail = "descartado_error", str(exc)[:200]
            canonical = broad = ""
        report.append({
            "tipo": "vehiculo", "fila_csv": index, "marca": brand, "modelo": model,
            "año_modelo": row.get("model_year", ""), "versión": row.get("version_name", ""),
            "motor": row.get("engine_label", ""), "transmisión": row.get("transmission", ""),
            "estado": status, "detalle": detail, "url_fuente": row.get("technical_source_url", ""),
        })
    return report


def price_candidate_report(conn, rows):
    report = []
    seen = set()
    for index, row in enumerate(rows, start=2):
        brand, model = (row.get("brand") or "").strip(), (row.get("model") or "").strip()
        try:
            match = _find_price_vehicle(conn, row)
            if not match:
                status, detail, key = "sin_vehiculo_exacta", "No hay una única versión compatible en el catálogo.", None
            else:
                key = (int(match["id"]), (row.get("source_url") or "").strip(), int(row.get("price_cop", 0)), (row.get("observed_at") or "").strip())
                exists = conn.execute(
                    "SELECT 1 FROM price_references WHERE version_id=? AND source_url=? AND price_cop=? AND observed_at=?",
                    key,
                ).fetchone()
                if key in seen or exists:
                    status, detail = "duplicado", "La referencia ya existe o se repite dentro del archivo."
                else:
                    status, detail = "candidato_nuevo", "Precio oficial sugerido; separado de precios de usados."
                seen.add(key)
        except Exception as exc:
            status, detail, key = "descartado_error", str(exc)[:200], None
        report.append({
            "tipo": "precio", "fila_csv": index, "marca": brand, "modelo": model,
            "año_modelo": row.get("model_year", ""), "versión": row.get("version_name", ""),
            "motor": row.get("engine_label", ""), "transmisión": row.get("transmission", ""),
            "estado": status, "detalle": detail, "url_fuente": row.get("source_url", ""),
            "precio_cop": row.get("price_cop", ""), "tipo_precio": row.get("price_type", ""),
        })
    return report


def display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def counts(conn):
    return {
        "brands": conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
        "models": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0],
        "versions": conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0],
        "price_references": conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0],
        "used_price_references": conn.execute("SELECT COUNT(*) FROM price_references WHERE price_type IN ('precio_solicitado','transaccion_documentada','guia_comercial')").fetchone()[0],
        "new_price_references": conn.execute("SELECT COUNT(*) FROM price_references WHERE price_type='precio_nuevo'").fetchone()[0],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Ruta de la base SQLite.")
    args = parser.parse_args()
    db_path = args.db.resolve()
    backup = backup_database(db_path)
    init_db(db_path, seed=False)
    conn = connect_db(db_path)
    try:
        before = counts(conn)
        vehicle_files = [
            DATA_DIR / "ampliacion_catalogo_oficial.csv",
            DATA_DIR / "ampliacion_catalogo_mercado.csv",
        ]
        price_files = [
            DATA_DIR / "ampliacion_precios_oficiales.csv",
            DATA_DIR / "ampliacion_precios_mercado.csv",
        ]
        vehicle_rows = [row for file_path in vehicle_files for row in read_csv(file_path)]
        price_rows = [row for file_path in price_files for row in read_csv(file_path)]
        vehicle_report = vehicle_candidate_report(conn, vehicle_rows)
        # Los precios dependen de versiones nuevas, por eso se clasifican después de importar vehículos.
        added_vehicles = skipped_vehicles = 0
        for file_path in vehicle_files:
            added, skipped = _load_vehicle_file(conn, file_path, "2026-10-26")
            added_vehicles += added
            skipped_vehicles += skipped
        conn.commit()
        price_report = price_candidate_report(conn, price_rows)
        added_prices = skipped_prices = 0
        for file_path in price_files:
            added, skipped = _load_price_file(conn, file_path, "2026-10-26")
            added_prices += added
            skipped_prices += skipped
        conn.commit()
        after = counts(conn)
        # Marcar qué candidatos nuevos quedaron efectivamente incorporados.
        remaining_added = added_vehicles
        for item in vehicle_report:
            if item["estado"] == "candidato_nuevo":
                if remaining_added > 0:
                    item["estado"] = "añadido"
                    remaining_added -= 1
                else:
                    item["estado"] = "descartado_error"
                    item["detalle"] = "No se pudo confirmar la inserción; revisar logs y restricciones."
        remaining_prices = added_prices
        for item in price_report:
            if item["estado"] == "candidato_nuevo":
                if remaining_prices > 0:
                    item["estado"] = "añadido"
                    remaining_prices -= 1
                else:
                    item["estado"] = "duplicado_o_error"
                    item["detalle"] = "No se confirmó la inserción; revisar el historial."
        outdir = ROOT / "reports"
        outdir.mkdir(parents=True, exist_ok=True)
        pairs = find_potential_duplicate_pairs(conn)
        baseline_vehicle_rows = read_csv(DATA_DIR / "seed_vehicles.csv")
        baseline_price_rows = read_csv(DATA_DIR / "seed_prices.csv")
        baseline = {
            "brands": len({normalize_token(r.get("brand")) for r in baseline_vehicle_rows}),
            "models": len({(normalize_token(r.get("brand")), normalize_token(r.get("model"))) for r in baseline_vehicle_rows}),
            "versions": len(baseline_vehicle_rows),
            "price_references": len(baseline_price_rows),
            "used_price_references": len(baseline_price_rows),
            "new_price_references": 0,
        }
        baseline_vehicle_keys = {
            vehicle_canonical_key(
                r.get("brand"), r.get("model"), r.get("model_year"), r.get("generation"),
                r.get("version_name"), r.get("engine_label"), r.get("fuel"), r.get("transmission")
            ) for r in baseline_vehicle_rows
        }
        def price_identity(r):
            return (
                normalize_token(r.get("brand")), normalize_token(r.get("model")), str(r.get("model_year") or ""),
                normalize_token(r.get("version_name")), normalize_token(r.get("engine_label")),
                normalize_token(r.get("transmission")), str(r.get("price_cop") or ""),
                (r.get("source_url") or "").strip(), (r.get("observed_at") or "").strip()
            )
        baseline_price_keys = {price_identity(r) for r in baseline_price_rows}

        cumulative_vehicle_report = []
        vehicle_row_number = 2
        for file_path in vehicle_files:
            for row in read_csv(file_path):
                key = vehicle_canonical_key(
                    row.get("brand"), row.get("model"), row.get("model_year"), row.get("generation"),
                    row.get("version_name"), row.get("engine_label"), row.get("fuel"), row.get("transmission")
                )
                exists = conn.execute("SELECT id FROM vehicle_versions WHERE canonical_key=?", (key,)).fetchone()
                if exists and key not in baseline_vehicle_keys:
                    state, detail = "añadido", f"Versión presente en SQLite, ID {exists['id']}; fuente y estado registrados en el catálogo."
                elif exists:
                    state, detail = "ya_existía_en_semilla", f"Versión de la base original, ID {exists['id']}."
                else:
                    state, detail = "no_importado_o_rechazado", "No se encontró la versión en SQLite; revisar validación/importación."
                cumulative_vehicle_report.append({
                    "tipo":"vehiculo","archivo_origen":file_path.name,"fila_csv":vehicle_row_number,
                    "marca":row.get("brand",""),"modelo":row.get("model",""),"año_modelo":row.get("model_year",""),
                    "versión":row.get("version_name",""),"motor":row.get("engine_label",""),
                    "transmisión":row.get("transmission",""),"estado":state,"detalle":detail,
                    "url_fuente":row.get("technical_source_url",""),"precio_cop":"","tipo_precio":"",
                })
                vehicle_row_number += 1

        cumulative_price_report = []
        price_row_number = 2
        for file_path in price_files:
            for row in read_csv(file_path):
                match = _find_price_vehicle(conn, row)
                exists = None
                if match:
                    exists = conn.execute(
                        "SELECT 1 FROM price_references WHERE version_id=? AND source_url=? AND price_cop=? AND observed_at=?",
                        (int(match["id"]), (row.get("source_url") or "").strip(), int(row.get("price_cop") or 0), (row.get("observed_at") or "").strip())
                    ).fetchone()
                identity = price_identity(row)
                if exists and identity not in baseline_price_keys:
                    state, detail = "añadido", "Referencia presente en SQLite; tipo y estado se conservan por separado."
                elif exists:
                    state, detail = "ya_existía_en_semilla", "Referencia incluida en la base original."
                else:
                    state, detail = "no_importado_o_rechazado", "Referencia no encontrada en SQLite o sin versión exacta."
                cumulative_price_report.append({
                    "tipo":"precio","archivo_origen":file_path.name,"fila_csv":price_row_number,
                    "marca":row.get("brand",""),"modelo":row.get("model",""),"año_modelo":row.get("model_year",""),
                    "versión":row.get("version_name",""),"motor":row.get("engine_label",""),
                    "transmisión":row.get("transmission",""),"estado":state,"detalle":detail,
                    "url_fuente":row.get("source_url",""),"precio_cop":row.get("price_cop",""),
                    "tipo_precio":row.get("price_type",""),
                })
                price_row_number += 1

        with (outdir / "INFORME_IMPORTACION_CATALOGO.csv").open("w", encoding="utf-8-sig", newline="") as f:
            fields = ["tipo","archivo_origen","fila_csv","marca","modelo","año_modelo","versión","motor","transmisión","estado","detalle","url_fuente","precio_cop","tipo_precio"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(cumulative_vehicle_report + cumulative_price_report)
        with (outdir / "INFORME_POSIBLES_DUPLICADOS.csv").open("w", encoding="utf-8-sig", newline="") as f:
            fields = ["id_a","vehicle_a","generation_a","id_b","vehicle_b","generation_b","reason"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(pairs)

        summary = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "database": display_path(db_path),
            "backup": display_path(backup),
            "baseline_seed": baseline,
            "before_this_run": before,
            "after": after,
            "added_this_run": {
                "vehicle_versions": added_vehicles,
                "price_references": added_prices,
                "used_price_references": sum(1 for r in price_report if r["estado"] == "añadido" and r.get("tipo_precio") == "precio_solicitado"),
                "new_vehicle_prices": sum(1 for r in price_report if r["estado"] == "añadido" and r.get("tipo_precio") == "precio_nuevo"),
            },
            "cumulative_added_since_seed": {
                "vehicle_versions": after["versions"] - baseline["versions"],
                "price_references": after["price_references"] - baseline["price_references"],
                "used_price_references": after["used_price_references"] - baseline["used_price_references"],
                "new_vehicle_prices": after["new_price_references"] - baseline["new_price_references"],
            },
            "skipped_vehicle_rows_this_run": skipped_vehicles,
            "skipped_price_rows_this_run": skipped_prices,
            "possible_duplicate_pairs": len(pairs),
        }
        summary_text = json.dumps(summary, ensure_ascii=False, indent=2)
        (outdir / "INFORME_IMPORTACION_CATALOGO.json").write_text(summary_text, encoding="utf-8")
        with (outdir / "INFORME_IMPORTACION_HISTORIAL.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        baseline_lines = [
            "# Informe acumulado de importación del catálogo", "",
            f"Última ejecución registrada: {summary['generated_at']}", "",
            "## Base de comparación", "",
            f"- Base de datos inicial del proyecto: {baseline['brands']} marcas, {baseline['models']} líneas/modelos, {baseline['versions']} versiones y {baseline['price_references']} referencias de precio.",
            f"- Base de datos actual: {after['brands']} marcas, {after['models']} líneas/modelos, {after['versions']} versiones y {after['price_references']} referencias económicas.",
            "", "## Incorporaciones acumuladas frente a la semilla", "",
            f"- Versiones añadidas: **{summary['cumulative_added_since_seed']['vehicle_versions']}**.",
            f"- Referencias económicas añadidas: **{summary['cumulative_added_since_seed']['price_references']}**.",
            f"- Anuncios/precios de mercado usado añadidos: **{summary['cumulative_added_since_seed']['used_price_references']}**.",
            f"- Precios sugeridos de vehículos nuevos añadidos: **{summary['cumulative_added_since_seed']['new_vehicle_prices']}**.",
            f"- Filas de vehículos omitidas en la última ejecución por duplicado/validación: {skipped_vehicles}.",
            f"- Filas de precios omitidas en la última ejecución por duplicado/validación: {skipped_prices}.",
            f"- Pares de posibles duplicados que requieren revisión: {len(pairs)}.",
            f"- Copia de seguridad de la última ejecución: `{backup.name if backup else 'no se creó (la base no existía antes de la ejecución)'}`.",
            "", "La ampliación conserva los registros iniciales, no genera especificaciones ni precios ficticios y mantiene separados los precios de vehículos nuevos y usados. El CSV de importación es acumulativo frente a los archivos de semilla.", "",
        ]
        (outdir / "INFORME_IMPORTACION_CATALOGO.md").write_text("\n".join(baseline_lines), encoding="utf-8")
        print(summary_text)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
