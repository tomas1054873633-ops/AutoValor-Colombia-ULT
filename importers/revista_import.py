"""Importador idempotente de las guías de precios de Revista Motor.

Entrada: CSV extraídos del PDF con ``revista_extract_usados.py`` y
``revista_extract_nuevos.py`` (una fila por observación de precio con su página,
coordenadas y año de encabezado). Salida: catálogo + referencias de precio en
SQLite, historial de importación y reportes de pendientes.

Reglas de oro (ver reports/METODO_VALORACION.md):
  * Usados: el valor de la guía está en MILLONES de COP ("61,5" = $61.500.000).
  * Nuevos: el valor ya está en pesos COP; nunca se multiplica.
  * Cada precio queda asociado al año del encabezado de su columna.
  * Celdas ambiguas, ilegibles o fuera de patrón NO se importan: pasan a pendientes.
  * Reimportar el mismo archivo no duplica nada (clave: version + localizador + valor).
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from catalog_utils import get_or_create_brand, get_or_create_model, normalize_token, vehicle_canonical_key  # noqa: E402
from revista_parser import (  # noqa: E402
    ParsedVehicle, new_scope_decision, parse_description, used_scope_decision,
)

IMPORTER_NAME = "revista_motor v1"
EDITION_DATE = "2026-10-14"
SOURCE_USED = "Revista Motor — Guía de precios de usados (edición 14 oct 2026)"
SOURCE_NEW = "Revista Motor — Precios de vehículos nuevos (edición 14 oct 2026)"
ORIGIN_USED = "revista_motor_usados"
ORIGIN_NEW = "revista_motor_nuevos"
NEW_ASSUMED_YEAR = 2026

SOURCE_USED_NOTES = (
    "Tablas elaboradas por Revista Motor como referencia de precios promedio de vehículos usados en "
    "Bogotá hasta la fecha de impresión (edición 14 oct 2026). Asumen buenas condiciones de uso y "
    "kilometraje proporcional a la edad (20.000 km/año; 14.000 km/año desde modelos 2007 por "
    "restricciones de Pico y Placa). No son vinculantes y la revista no garantiza su exactitud. "
    "Valores publicados en millones de COP. Uso: referencia interna; confirmar autorización antes de redistribuir."
)
SOURCE_NEW_NOTES = (
    "Listado de precios de vehículos nuevos de la edición 14 oct 2026 de Revista Motor, publicado en pesos "
    "colombianos. No publica año modelo ni kilometraje; no es un precio de usado."
)

HYBRID_LEGEND = {"HL": "Híbrido ligero", "HC": "Híbrido completo", "HE": "Híbrido enchufable", "HR": "Híbrido de rango extendido"}

USED_PRICE_MIN, USED_PRICE_MAX = 3_000_000, 3_000_000_000
NEW_PRICE_MIN, NEW_PRICE_MAX = 20_000_000, 5_000_000_000
# Un valor se separa de la serie si es menos de la mitad o más del doble de sus vecinos de año.
OUTLIER_LOW, OUTLIER_HIGH = 0.5, 2.0
# Inversión de la serie (el año más viejo vale más que el más nuevo): se importa con advertencia.
INVERSION_LIMIT = 1.0


class Report:
    """Acumula contadores y filas de pendientes/descartes."""

    def __init__(self):
        self.counts: Counter = Counter()
        self.issues: list[dict] = []
        self.versions_created: list[int] = []
        self.versions_touched: set[int] = set()
        self.model_pairs: set[tuple[str, str]] = set()
        self.vehicle_keys: set[tuple] = set()

    def issue(self, severity: str, issue_type: str, **kw):
        row = {"severity": severity, "issue_type": issue_type}
        row.update(kw)
        self.issues.append(row)
        self.counts[f"{severity}:{issue_type}"] += 1


def _ensure_source(conn, name: str, source_type: str, notes: str, edition: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO sources(name, domain, source_type, base_url, usage_notes, last_checked_at) VALUES(?,?,?,?,?,?)",
        (name, "revistamotor", source_type, None, notes, edition),
    )
    conn.execute("UPDATE sources SET usage_notes=?, source_type=?, last_checked_at=? WHERE name=?",
                 (notes, source_type, edition, name))
    return int(conn.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()[0])


class CatalogWriter:
    def __init__(self, conn: sqlite3.Connection, run_id: int, report: Report):
        self.conn, self.run_id, self.report = conn, run_id, report
        self.brand_cache: dict[str, int] = {}
        self.model_cache: dict[tuple[int, str], int] = {}
        self.version_cache: dict[str, int] = {}

    def version_for(self, p: ParsedVehicle, year: int, origin: str, year_basis: str, extra_notes: str = "",
                    source_ref: str | None = None) -> int:
        brand_id = self.brand_cache.get(p.brand)
        if brand_id is None:
            brand_id = get_or_create_brand(self.conn, p.brand, "Internacional")
            self.brand_cache[p.brand] = brand_id
        mk = (brand_id, p.model)
        model_id = self.model_cache.get(mk)
        if model_id is None:
            model_id = get_or_create_model(self.conn, brand_id, p.model, p.category.title() if p.category else None)
            self.model_cache[mk] = model_id
        key = vehicle_canonical_key(p.brand, p.model, year, p.generation, p.version_name,
                                    p.engine_label, p.fuel, p.transmission)
        vid = self.version_cache.get(key) if source_ref is None else None
        if vid is None and source_ref:
            # Un precio ya importado identifica su versión sin ambigüedad: se reutiliza esa versión
            # aunque la auditoría de transmisiones haya cambiado su caja o su clave lógica.
            row = self.conn.execute(
                "SELECT version_id FROM price_references WHERE source_ref=? ORDER BY id LIMIT 1", (source_ref,)).fetchone()
            if row:
                vid = int(row[0])
                self.report.counts["versiones_existentes_reutilizadas"] += 1
        if vid is None:
            row = self.conn.execute("SELECT id FROM vehicle_versions WHERE canonical_key=?", (key,)).fetchone()
            if row is None:
                # La auditoría de transmisiones puede haber confirmado la caja de una versión que la guía
                # publica sin marca («Sin confirmar» → «Mecánica»): se reconoce por su descripción literal
                # para no crear un duplicado al volver a importar.
                row = self.conn.execute(
                    """SELECT id FROM vehicle_versions WHERE model_id=? AND model_year=? AND raw_description=?
                       AND data_origin=? ORDER BY id LIMIT 1""",
                    (model_id, year, p.raw_description, origin)).fetchone()
            if row:
                vid = int(row[0])
                self.report.counts["versiones_existentes_reutilizadas"] += 1
            else:
                notes = (f"Nomenclatura publicada por Revista Motor: «{p.raw_description}» (categoría {p.category}). "
                         "Motor, combustible y transmisión solo se registran si la guía los publica; "
                         "sin ficha técnica verificada." + (" " + extra_notes if extra_notes else ""))
                if p.flags:
                    notes += " Observaciones del importador: " + "; ".join(p.flags) + "."
                cur = self.conn.execute(
                    """INSERT INTO vehicle_versions
                       (model_id, model_year, generation, version_name, engine_label, displacement_cc, fuel,
                        transmission, canonical_key, verification_status, notes, data_origin, year_basis,
                        raw_description, source_category, origin_run_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (model_id, year, p.generation, p.version_name, p.engine_label, None, p.fuel,
                     p.transmission, key, "parcial", notes, origin, year_basis,
                     p.raw_description, p.category, self.run_id),
                )
                vid = int(cur.lastrowid)
                self.report.versions_created.append(vid)
                self.report.counts["versiones_nuevas"] += 1
            self.version_cache[key] = vid
        self.report.versions_touched.add(vid)
        self.report.model_pairs.add((p.brand, p.model))
        self.report.vehicle_keys.add((p.brand, p.model, p.generation, p.version_name, p.engine_label, p.fuel, p.transmission))
        return vid


def _fuel_short(fuel):
    if not fuel:
        return None
    return "Híbrido" if fuel.startswith("Híbrido (") else fuel


def _new_fuel(p: ParsedVehicle, category: str, hybrid: str) -> str | None:
    if hybrid:
        return HYBRID_LEGEND.get(hybrid, p.fuel)
    cat = category.upper()
    if "ELÉCTRICO" in cat and not p.fuel:
        return "Eléctrico"
    if "HÍBRIDO" in cat and not p.fuel:
        return "Híbrido (según categoría de la guía)"
    return p.fuel


def build_parsed(used_rows: list[dict], new_rows: list[dict]) -> dict:
    """Interpreta cada descripción distinta (una vez) y desambigua nombres que chocarían.

    La tabla ``vehicle_versions`` exige unicidad por (modelo, año, versión, motor, transmisión).
    Cuando la propia guía publica dos vehículos que solo difieren por generación o combustible,
    se añade ese dato entre paréntesis a la versión de forma determinista (independiente del orden).
    """
    parsed: dict[tuple, ParsedVehicle | ValueError] = {}
    for kind, rows in (("u", used_rows), ("n", new_rows)):
        for r in rows:
            cat, brand, desc = r["category"], r["brand"], r["description"]
            scope = (used_scope_decision if kind == "u" else new_scope_decision)(cat, brand, desc)
            if not scope[0]:
                continue
            hybrid = (r.get("hybrid_type") or "").strip() if kind == "n" else ""
            key = (kind, brand, desc, cat, hybrid)
            if key in parsed:
                continue
            try:
                p = parse_description(brand, desc, cat)
            except ValueError as exc:
                parsed[key] = exc
                continue
            if kind == "n":
                p.fuel = _new_fuel(p, cat, hybrid)
            parsed[key] = p
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for key, p in parsed.items():
        if isinstance(p, ParsedVehicle):
            groups[(p.brand, p.model, p.version_name, p.engine_label, p.transmission)].append(key)
    for gkey, keys in groups.items():
        variants = {(parsed[k].generation, parsed[k].fuel) for k in keys}
        if len(variants) <= 1:
            continue
        for k in keys:
            p = parsed[k]
            parts = [x for x in (p.generation, _fuel_short(p.fuel)) if x]
            if parts:
                p.version_name = f"{p.version_name} ({', '.join(parts)})"
                p.flags.append("nombre de versión calificado porque la guía publica otra descripción idéntica que difiere en generación o combustible")
    return parsed


def _to_int(value, default=None):
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _insert_price(conn, run_id, report, *, version_id, source_id, price, price_type, year, source_ref, page,
                  observed_at, raw_value, status, notes, unit_note) -> bool:
    before = conn.total_changes
    conn.execute(
        """INSERT OR IGNORE INTO price_references
           (version_id, source_id, price_cop, price_type, model_year, mileage_km, location, source_url,
            observed_at, verification_status, notes, import_run_id, source_ref, source_page, column_year,
            raw_value, price_unit_note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (version_id, source_id, price, price_type, year, None, "Bogotá" if price_type == "guia_comercial" else None,
         source_ref, observed_at, status, notes, run_id, source_ref, page, year, raw_value, unit_note),
    )
    inserted = conn.total_changes > before
    report.counts["precios_insertados" if inserted else "precios_ya_existentes"] += 1
    return inserted


# ---------------------------------------------------------------------------
# Usados
# ---------------------------------------------------------------------------
def _series_screen(rows: list[dict]) -> dict[int, str]:
    """Clasifica cada observación: '' ok, 'inversion' (advertencia) o 'atipico' (pendiente)."""
    by_row: dict[tuple, dict[int, int]] = defaultdict(dict)
    for i, r in enumerate(rows):
        by_row[(r["page"], r["category"], r["brand"], r["description"], r["col_x"], r["row_top"])][int(r["model_year"])] = i
    verdict: dict[int, str] = {}
    for key, years in by_row.items():
        price = {y: int(rows[idx]["price_cop"]) for y, idx in years.items()}
        ordered = sorted(price)
        # Pasada 1: puntos interiores comparados con sus dos vecinos de año.
        flagged: set[int] = set()
        for y in ordered:
            if y - 1 in price and y + 1 in price:
                lo, hi = min(price[y - 1], price[y + 1]), max(price[y - 1], price[y + 1])
                if price[y] < OUTLIER_LOW * lo or price[y] > OUTLIER_HIGH * hi:
                    flagged.add(y)
        # Pasada 2: extremos de la serie frente al vecino contiguo no marcado.
        for y in (ordered[0], ordered[-1]):
            if y in flagged:
                continue
            step = 1 if y == ordered[0] else -1
            n = y + step
            if n in price and n not in flagged:
                if price[y] < OUTLIER_LOW * price[n] or price[y] > OUTLIER_HIGH * price[n]:
                    flagged.add(y)
        for y in flagged:
            verdict[years[y]] = "atipico"
        for y in ordered:
            if y in flagged:
                continue
            older = y - 1
            if older in price and older not in flagged and price[older] > INVERSION_LIMIT * price[y]:
                verdict[years[older]] = "inversion"
    return verdict


def import_used(conn, run_id, report: Report, rows: list[dict], parsed: dict, edition: str, source_id: int, writer: CatalogWriter):
    report.counts["usados_observaciones_en_archivo"] = len(rows)
    screen = _series_screen(rows)
    for i, r in enumerate(rows):
        page = _to_int(r["page"])
        desc, brand, cat = r["description"], r["brand"], r["category"]
        year = _to_int(r["model_year"])
        raw_value = r["raw_value"]
        ref = f"revista-motor:{edition}:p{page}:x{r['col_x']}:y{r['row_top']}:a{year}:usado"
        base = dict(page=page, brand=brand, raw_description=desc, model_year=year, raw_value=raw_value, source_ref=ref)
        report.counts["usados_observaciones_evaluadas"] += 1
        in_scope, reason = used_scope_decision(cat, brand, desc)
        if not in_scope:
            report.issue("descartado", "fuera_de_alcance", detail=reason, **base)
            continue
        if r.get("flag"):
            report.issue("pendiente", "valor_ambiguo", detail=f"{r['flag']}. Interpretación propuesta: {r['price_cop']} COP; requiere confirmación humana.", **base)
            continue
        price = _to_int(r["price_cop"])
        if price is None or not (USED_PRICE_MIN <= price <= USED_PRICE_MAX):
            report.issue("pendiente", "precio_fuera_de_rango", detail=f"Valor convertido {r['price_cop']} COP fuera de {USED_PRICE_MIN}–{USED_PRICE_MAX}.", **base)
            continue
        if year is None or not (1990 <= year <= 2027):
            report.issue("pendiente", "anio_invalido", detail=f"Año de encabezado inválido: {r['model_year']}", **base)
            continue
        verdict = screen.get(i, "")
        if verdict == "atipico":
            report.issue("pendiente", "valor_atipico_en_serie",
                         detail="El valor es menos de la mitad o más del doble que el de los años contiguos de la misma fila; "
                                "probable error de impresión o de lectura. No se corrigió ni se importó.", **base)
            continue
        p = parsed[("u", brand, desc, cat, "")]
        if isinstance(p, Exception):
            report.issue("pendiente", "descripcion_no_interpretable", detail=str(p), **base)
            continue
        vid = writer.version_for(p, year, ORIGIN_USED, "Año tomado del encabezado de la columna de la guía de usados.",
                                 source_ref=ref)
        notes = (f"Transcripción automática de la guía de usados (valor publicado: {raw_value} millones de COP). "
                 "Supone buen estado y kilometraje esperado según la edad.")
        if verdict == "inversion":
            notes += " Advertencia: en la serie de la fila el año anterior figura con un valor mayor."
            report.issue("advertencia", "inversion_de_serie", detail="El año contiguo más viejo tiene un valor mayor; se importa tal como se publica.", **base)
        if p.flags:
            for fl in p.flags:
                report.issue("advertencia", "descripcion_interpretada", detail=fl, **base)
        _insert_price(conn, run_id, report, version_id=vid, source_id=source_id, price=price, price_type="guia_comercial",
                      year=year, source_ref=ref, page=page, observed_at=edition, raw_value=raw_value, status="parcial",
                      notes=notes, unit_note="Valor publicado en millones de COP; convertido ×1.000.000.")
        report.counts["usados_precios_validos"] += 1


# ---------------------------------------------------------------------------
# Nuevos
# ---------------------------------------------------------------------------
def import_new(conn, run_id, report: Report, rows: list[dict], parsed: dict, edition: str, source_id: int, writer: CatalogWriter):
    report.counts["nuevos_observaciones_en_archivo"] = len(rows)
    for r in rows:
        page = _to_int(r["page"])
        desc, brand, cat = r["description"], r["brand"], r["category"]
        ref = f"revista-motor:{edition}:p{page}:c{r['col']}:y{r['y']}:nuevo"
        base = dict(page=page, brand=brand, raw_description=desc, model_year=None, raw_value=r["raw_price"], source_ref=ref)
        report.counts["nuevos_observaciones_evaluadas"] += 1
        in_scope, reason = new_scope_decision(cat, brand, desc)
        if not in_scope:
            report.issue("descartado", "fuera_de_alcance", detail=f"{reason} [categoría {cat}]", **base)
            continue
        price = _to_int(r["price_cop"])
        if price is None or not (NEW_PRICE_MIN <= price <= NEW_PRICE_MAX):
            report.issue("pendiente", "precio_fuera_de_rango", detail=f"Valor {r['price_cop']} COP fuera de {NEW_PRICE_MIN}–{NEW_PRICE_MAX}.", **base)
            continue
        hybrid = (r.get("hybrid_type") or "").strip()
        p = parsed[("n", brand, desc, cat, hybrid)]
        if isinstance(p, Exception):
            report.issue("pendiente", "descripcion_no_interpretable", detail=str(p), **base)
            continue
        vid = writer.version_for(
            p, NEW_ASSUMED_YEAR, ORIGIN_NEW,
            "ASUMIDO: la lista de precios de nuevos no publica año modelo; se agrupa bajo 2026 (año de la edición).",
            extra_notes="Año modelo no publicado por la fuente; 2026 es el año de la edición, no un dato del vehículo.",
            source_ref=ref,
        )
        if r.get("wrapped") == "True":
            report.issue("advertencia", "descripcion_en_varias_lineas", detail="El nombre ocupaba varias líneas en el PDF; se unió por posición.", **base)
        for fl in p.flags:
            report.issue("advertencia", "descripcion_interpretada", detail=fl, **base)
        notes = ("Precio de lista de vehículo nuevo publicado en pesos COP (sin conversión). "
                 "Año modelo no publicado; no es un precio de usado." + (f" Tipo híbrido: {HYBRID_LEGEND.get(hybrid, hybrid)}." if hybrid else ""))
        _insert_price(conn, run_id, report, version_id=vid, source_id=source_id, price=price, price_type="precio_nuevo",
                      year=NEW_ASSUMED_YEAR, source_ref=ref, page=page, observed_at=edition, raw_value=r["raw_price"],
                      status="parcial", notes=notes, unit_note="Publicado en COP; no se multiplica.")
        report.counts["nuevos_precios_validos"] += 1


# ---------------------------------------------------------------------------
# Conflictos
# ---------------------------------------------------------------------------
def detect_conflicts(conn, run_id, report: Report):
    rows = conn.execute(
        """SELECT version_id, price_type, GROUP_CONCAT(DISTINCT price_cop) AS vals, COUNT(DISTINCT price_cop) AS n,
                  COUNT(*) AS obs
           FROM price_references WHERE import_run_id IS NOT NULL AND price_type IN ('guia_comercial','precio_nuevo')
           GROUP BY version_id, price_type HAVING n > 1"""
    ).fetchall()
    for r in rows:
        v = conn.execute(
            """SELECT b.name AS brand, m.name AS model, v.model_year, v.version_name FROM vehicle_versions v
               JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id WHERE v.id=?""", (r["version_id"],)).fetchone()
        report.issue("conflicto", "precios_distintos_misma_version",
                     brand=v["brand"], raw_description=f'{v["model"]} {v["version_name"]}', model_year=v["model_year"],
                     raw_value=r["vals"], detail=f"{r['n']} valores distintos publicados para la misma versión/año ({r['price_type']}); no se elige uno. "
                                                "La valoración no usará esta referencia hasta resolverlo.")


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
def run_import(conn, used_csv: Path | None, new_csv: Path | None, *, edition: str = EDITION_DATE,
               source_file_name: str = "", source_sha256: str = "", dry_run: bool = False) -> dict:
    report = Report()
    params = {"used_csv": str(used_csv) if used_csv else None, "new_csv": str(new_csv) if new_csv else None,
              "edition": edition, "dry_run": dry_run}
    cur = conn.execute(
        "INSERT INTO import_runs(importer, source_name, source_file_name, source_sha256, source_edition_date, status, parameters) "
        "VALUES(?,?,?,?,?,?,?)",
        (IMPORTER_NAME, "Revista Motor", source_file_name, source_sha256, edition,
         "simulacion" if dry_run else "en_curso", json.dumps(params)),
    )
    run_id = int(cur.lastrowid)
    before = {
        "marcas": conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
        "modelos": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0],
        "versiones": conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0],
        "precios": conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0],
    }
    try:
        src_used = _ensure_source(conn, SOURCE_USED, "guia_comercial", SOURCE_USED_NOTES, edition)
        src_new = _ensure_source(conn, SOURCE_NEW, "precio_nuevo", SOURCE_NEW_NOTES, edition)
        writer = CatalogWriter(conn, run_id, report)
        def _read(path):
            with open(path, encoding="utf-8", newline="") as fh:
                return list(csv.DictReader(fh))
        used_rows = _read(used_csv) if used_csv else []
        new_rows = _read(new_csv) if new_csv else []
        parsed = build_parsed(used_rows, new_rows)
        if used_csv:
            import_used(conn, run_id, report, used_rows, parsed, edition, src_used, writer)
        if new_csv:
            import_new(conn, run_id, report, new_rows, parsed, edition, src_new, writer)
        detect_conflicts(conn, run_id, report)
        after = {
            "marcas": conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
            "modelos": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0],
            "versiones": conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0],
            "precios": conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0],
        }
        for rec in report.issues:
            conn.execute(
                """INSERT INTO import_issues(run_id, severity, issue_type, page, brand, raw_description, model_year,
                                              raw_value, source_ref, detail) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, rec["severity"], rec["issue_type"], rec.get("page"), rec.get("brand"),
                 rec.get("raw_description"), rec.get("model_year"), rec.get("raw_value"), rec.get("source_ref"), rec.get("detail")),
            )
        summary = {
            "antes": before, "despues": after,
            "contadores": dict(sorted(report.counts.items())),
            "vehiculos_distintos_marca_modelo": len(report.model_pairs),
            "vehiculos_distintos_sin_anio": len(report.vehicle_keys),
            "versiones_tocadas": len(report.versions_touched),
        }
        conn.execute(
            "UPDATE import_runs SET finished_at=?, status=?, counts=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), "simulacion" if dry_run else "completada",
             json.dumps(summary, ensure_ascii=False), run_id),
        )
        if dry_run:
            conn.rollback()
            # El historial de una simulación no se conserva: se deshace todo.
        else:
            conn.execute(
                "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES('import','import_runs',?,?)",
                (run_id, f"Importación Revista Motor {edition}: {json.dumps(summary['contadores'], ensure_ascii=False)}"[:2000]),
            )
            conn.commit()
        summary["run_id"] = run_id
        summary["issues"] = report.issues
        return summary
    except Exception:
        conn.rollback()
        raise
