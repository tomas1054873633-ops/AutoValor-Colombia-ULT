"""Migraciones versionadas e idempotentes de AutoValor Pro.

Cada migración queda registrada en ``schema_migrations``. Son aditivas: no borran
ni sobrescriben datos existentes. Se ejecutan al abrir la base (``init_db``) y
también pueden ejecutarse manualmente con ``python scripts/migrate.py``.
"""
from __future__ import annotations

import sqlite3
from typing import Callable


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def m001_import_tracking(conn: sqlite3.Connection) -> None:
    """Historial de importaciones, trazabilidad por fila y procedencia del catálogo."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            importer TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_file_name TEXT,
            source_sha256 TEXT,
            source_edition_date TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'en_curso'
                CHECK(status IN ('en_curso','completada','fallida','simulacion')),
            parameters TEXT,
            counts TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS import_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
            severity TEXT NOT NULL CHECK(severity IN ('pendiente','descartado','advertencia','conflicto')),
            issue_type TEXT NOT NULL,
            page INTEGER,
            brand TEXT,
            raw_description TEXT,
            model_year INTEGER,
            raw_value TEXT,
            source_ref TEXT,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_import_issues_run ON import_issues(run_id, severity, issue_type);
        """
    )
    _add_columns(conn, "vehicle_versions", {
        "data_origin": "TEXT",          # p. ej. revista_motor_usados, revista_motor_nuevos (NULL = carga original)
        "year_basis": "TEXT",           # cómo se determinó el año modelo
        "raw_description": "TEXT",      # descripción tal como la publica la fuente
        "source_category": "TEXT",      # categoría de la fuente (AUTOMÓVILES, CAMIONETAS, ...)
        "origin_run_id": "INTEGER",
    })
    _add_columns(conn, "price_references", {
        "import_run_id": "INTEGER",
        "source_ref": "TEXT",           # localizador de fila: edición/página/coordenada/año de columna
        "source_page": "INTEGER",
        "column_year": "INTEGER",       # año del encabezado de la columna de donde sale el precio
        "raw_value": "TEXT",            # valor tal como aparece (p. ej. "61,5" o "170.000.000")
        "price_unit_note": "TEXT",
    })
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_prices_version_type_year ON price_references(version_id, price_type, model_year);
        CREATE INDEX IF NOT EXISTS idx_prices_import_run ON price_references(import_run_id);
        CREATE INDEX IF NOT EXISTS idx_versions_origin ON vehicle_versions(data_origin, model_year);
        CREATE INDEX IF NOT EXISTS idx_versions_origin_run ON vehicle_versions(origin_run_id);
        """
    )


def m002_valuation_config_v2(conn: sqlite3.Connection) -> None:
    """Parámetros del motor de valoración basado en kilometraje esperado (configurables)."""
    rows = [
        ("km_expected_per_year", "14000",
         "Kilometraje anual esperado, km/año. Valor documentado por Revista Motor para modelos 2007 en adelante; parámetro configurable."),
        ("km_deadband_pct", "10",
         "Zona sin ajuste: diferencia relativa (%) respecto al kilometraje esperado dentro de la cual no se ajusta el valor."),
        ("km_deadband_floor_km", "5000",
         "Zona sin ajuste mínima (km): diferencias menores a este valor frente al esperado no modifican el valor."),
        ("valuation_rounding_cop", "100000",
         "Redondeo (COP) de la valoración final, para no sugerir una exactitud que el método no tiene."),
        ("km_scale_floor", "10000",
         "Kilometraje mínimo (km) usado como escala para vehículos recientes, evitando divisiones por un esperado cercano a cero."),
        ("km_penalty_cap_pct", "15",
         "Tope del descuento por kilometraje superior al esperado (% del valor de referencia). Parámetro inicial configurable, no calibrado con datos de mercado."),
        ("km_bonus_cap_pct", "5",
         "Tope del incremento por kilometraje inferior al esperado (% del valor de referencia). Parámetro inicial configurable, no calibrado con datos de mercado."),
        ("km_curve_steepness", "1.0",
         "Pendiente de la curva suave (tanh) del ajuste por kilometraje. Parámetro inicial configurable, no calibrado con datos de mercado."),
        ("km_atypical_high_factor", "2.5",
         "Se advierte kilometraje atípico si supera este múltiplo del kilometraje esperado."),
        ("km_atypical_low_factor", "0.2",
         "Se advierte kilometraje atípico si es inferior a esta fracción del esperado (con vehículo de al menos 2 años)."),
        ("reference_year_offset", "0",
         "Desplazamiento (años) sobre el año calendario para calcular la edad del vehículo."),
        ("guide_vs_ads_warning_pct", "20",
         "Se advierte si la mediana de anuncios difiere de la guía en más de este porcentaje."),
        ("guide_max_age_days", "120",
         "Antigüedad máxima (días) de una edición de guía para tratarla como vigente sin advertencia."),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO valuation_config(key, value, description) VALUES(?,?,?)", rows
    )
    # El ajuste fijo por 10.000 km del motor anterior queda retirado del cálculo.
    conn.execute(
        "UPDATE valuation_config SET description = ? WHERE key = 'provisional_cop_per_10000km'",
        ("OBSOLETO: ya no se usa. El ajuste por kilometraje es porcentual y relativo al kilometraje esperado.",),
    )
    conn.execute(
        "UPDATE valuation_config SET description = ? WHERE key IN ('annual_km_low','annual_km_central','annual_km_high')",
        ("Referencia anual solo para advertencias; ya no define un ajuste monetario.",),
    )


def m003_transmission_audit(conn: sqlite3.Connection) -> None:
    """Transmisión normalizada (categoría + subtipo + estado + evidencia) y registro de auditoría.

    Solo añade columnas y tablas. Los valores los calcula ``transmission_audit.run_audit``,
    que conserva el valor original en ``transmission_original`` y registra cada cambio.
    """
    _add_columns(conn, "vehicle_versions", {
        "transmission_subtype": "TEXT",       # vocabulario cerrado (transmissions.SUBTYPES)
        "transmission_gears": "INTEGER",      # marchas, solo si están documentadas
        "transmission_detail": "TEXT",        # p. ej. SKYACTIV-DRIVE, S tronic, con levas
        "transmission_status": "TEXT",        # verificada/documentada/inferida/regla_tecnica/pendiente/conflicto
        "transmission_rule": "TEXT",          # regla o ficha que fijó el valor
        "transmission_evidence": "TEXT",      # justificación legible
        "transmission_source_ids": "TEXT",    # ids de technical_sources separados por ';'
        "transmission_original": "TEXT",      # valor previo a la primera normalización
        "transmission_checked_at": "TEXT",    # fecha de la auditoría que fijó el valor
    })
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS technical_sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            publisher TEXT,
            url TEXT NOT NULL,
            published_date TEXT,
            accessed_date TEXT NOT NULL,
            market TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transmission_specs (
            spec_id TEXT PRIMARY KEY,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            year_from INTEGER NOT NULL,
            year_to INTEGER NOT NULL,
            desc_regex TEXT,
            exclude_regex TEXT,
            engine_regex TEXT,
            origin TEXT,
            offering TEXT NOT NULL CHECK(offering IN ('MT','AT','MT+AT')),
            level TEXT NOT NULL DEFAULT 'verificada' CHECK(level IN ('verificada','inferida')),
            mt_gears INTEGER,
            at_subtype TEXT,
            at_gears INTEGER,
            at_detail TEXT,
            source_ids TEXT NOT NULL,
            evidence TEXT NOT NULL,
            checked_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transmission_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            mode TEXT NOT NULL CHECK(mode IN ('simulacion','aplicada')),
            rules_version TEXT,
            specs_sha256 TEXT,
            sources_sha256 TEXT,
            versions_examined INTEGER,
            versions_changed INTEGER,
            summary TEXT
        );
        CREATE TABLE IF NOT EXISTS transmission_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES transmission_audit_runs(id) ON DELETE CASCADE,
            version_id INTEGER NOT NULL REFERENCES vehicle_versions(id) ON DELETE CASCADE,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            rule TEXT,
            evidence TEXT,
            source_ids TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_transmission_changes_version ON transmission_changes(version_id, run_id);
        CREATE INDEX IF NOT EXISTS idx_versions_transmission_status ON vehicle_versions(transmission_status, model_year);
        """
    )


def m004_engine_audit(conn: sqlite3.Connection) -> None:
    """Motor documentado por ficha técnica para versiones publicadas sin cilindrada.

    Solo añade columnas. Los valores los calcula ``engine_audit.run_engine_audit``, que guarda el
    motor original (etiqueta, cilindrada y combustible) en ``engine_original`` antes de cambiarlo.
    """
    _add_columns(conn, "vehicle_versions", {
        "engine_original": "TEXT",       # JSON con etiqueta, cilindrada y combustible previos
        "engine_status": "TEXT",         # verificada/inferida/regla_tecnica
        "engine_rule": "TEXT",           # ficha o regla que fijó el motor
        "engine_evidence": "TEXT",       # justificación legible
        "engine_source_ids": "TEXT",     # ids de fuentes técnicas separados por ';'
        "engine_checked_at": "TEXT",     # fecha de la auditoría que fijó el motor
    })


MIGRATIONS: list[tuple[str, str, Callable[[sqlite3.Connection], None]]] = [
    ("001", "seguimiento de importaciones y trazabilidad por fila", m001_import_tracking),
    ("002", "parámetros de valoración por kilometraje esperado", m002_valuation_config_v2),
    ("003", "transmisiones normalizadas y auditoría de cajas", m003_transmission_audit),
    ("004", "motores documentados para versiones sin cilindrada", m004_engine_audit),
]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Aplica las migraciones pendientes y devuelve las versiones aplicadas en esta llamada."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, fn in MIGRATIONS:
        if version in done:
            continue
        fn(conn)
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES(?,?)", (version, name))
        applied.append(version)
    conn.commit()
    return applied
