"""Acceso a SQLite, inicialización, migraciones y carga trazable de datos."""
from __future__ import annotations

import csv
import math
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from migrations import apply_migrations
from catalog_utils import (
    find_version_duplicate,
    get_or_create_brand,
    get_or_create_model,
    populate_canonical_keys,
    vehicle_canonical_key,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "database" / "autovalor.db"
SCHEMA = ROOT / "database" / "schema.sql"
DATA_DIR = ROOT / "data"
VALID_VEHICLE_STATUSES = {"pendiente", "parcial", "verificado", "rechazado"}
VALID_PRICE_STATUSES = {"observado_no_verificado", "parcial", "verificado", "expirado", "rechazado"}
VALID_PRICE_TYPES = {
    "precio_solicitado", "transaccion_documentada", "guia_comercial",
    "valor_fiscal", "precio_nuevo", "otro",
}


def connect_db(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Migraciones aditivas; no elimina ni sobrescribe datos existentes."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(vehicle_versions)")}
    for column, definition in (
        ("technical_source_name", "TEXT"),
        ("technical_source_checked_at", "TEXT"),
        ("canonical_key", "TEXT"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE vehicle_versions ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_engine_config "
        "ON vehicle_versions(model_id, model_year, engine_label, fuel, displacement_cc, transmission)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_verification "
        "ON vehicle_versions(verification_status, model_year)"
    )
    collisions = populate_canonical_keys(conn)
    if not collisions:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_versions_canonical_key "
            "ON vehicle_versions(canonical_key) WHERE canonical_key IS NOT NULL"
        )
    else:
        # No bloquear la apertura de una base heredada: el informe de calidad
        # mostrará las colisiones para resolverlas antes de crear la restricción.
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, details) VALUES('migration_warning','vehicle_versions',?)",
            (f"Se detectaron {len(collisions)} colisiones de clave lógica; no se creó índice único.",),
        )
    conn.commit()
    apply_migrations(conn)


def _backup_before_auto_audit(path: Path) -> None:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.parent / "backups" / f"autovalor_pre_auditoria_transmisiones_auto_{stamp}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(path))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def _unzip_database(path: Path) -> None:
    """En el repositorio la base va comprimida (autovalor.db.zip) porque GitHub web no acepta
    archivos de más de 25 MB. Si la base no existe y está el .zip, se descomprime aquí."""
    if str(path) == ":memory:" or path.exists():
        return
    packed = path.with_name(path.name + ".zip")
    if packed.exists():
        import zipfile
        with zipfile.ZipFile(packed) as zf:
            zf.extract(path.name, path.parent)
        print(f"Base de datos descomprimida: {path}")


def init_db(db_path: str | Path = DEFAULT_DB, seed: bool = True, auto_audit: bool = True) -> None:
    """Crea o actualiza la base. Si hay versiones sin transmisión auditada (base nueva o
    anterior a la migración 003), ejecuta la auditoría de transmisiones; en una base con
    datos previos crea antes una copia de seguridad."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _unzip_database(path)
    conn = connect_db(path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        _migrate_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
        seeded_now = False
        if seed and count == 0:
            seed_database(conn)
            seeded_now = True
        if auto_audit:
            from transmission_audit import needs_audit, run_audit

            if needs_audit(conn):
                if not seeded_now and str(path) != ":memory:":
                    conn.commit()
                    _backup_before_auto_audit(path)
                run_audit(conn, apply=True, with_integrity=False)
        # Se repuebla después de seed para mantener compatibles bases antiguas
        # y cualquier registro que se haya añadido durante la carga inicial.
        collisions = populate_canonical_keys(conn)
        if not collisions:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_versions_canonical_key "
                "ON vehicle_versions(canonical_key) WHERE canonical_key IS NOT NULL"
            )
        conn.commit()
    finally:
        conn.close()


def _source_id(conn: sqlite3.Connection, name: str, source_url: str = "") -> int:
    parsed = urlparse(source_url) if source_url else None
    domain = parsed.netloc if parsed else ""
    normalized_name = name.lower().strip()
    if normalized_name in {"mercado libre", "mercadolibre"}:
        conn.execute(
            """INSERT OR IGNORE INTO sources(name, domain, source_type, base_url, usage_notes)
               VALUES(?, ?, 'anuncios_publicos', ?, ?)""",
            ("Mercado Libre", "mercadolibre.com.co", "https://www.mercadolibre.com.co/",
             "Precios solicitados publicados por usuarios o comercios; no son transacciones confirmadas. Las referencias existentes se capturaron manualmente para una muestra local. Revisar términos vigentes y obtener autorización/API antes de un uso comercial o extracción masiva."),
        )
        name = "Mercado Libre"
    elif normalized_name in {"fasecolda", "guía de valores fasecolda", "guia de valores fasecolda"}:
        conn.execute(
            """INSERT OR IGNORE INTO sources(name, domain, source_type, base_url, terms_url, usage_notes)
               VALUES(?, ?, 'guia_comercial', ?, ?, ?)""",
            ("Guía de Valores Fasecolda", "fasecolda.com",
             "https://www.fasecolda.com/fasecolda-guia-de-valores/centro-de-ayuda/preguntas-frecuentes/",
             "https://www.guiadevalores.com/terminos/",
             "Fuente candidata para futuras consultas autorizadas. No se copiaron precios de Fasecolda a esta base; revisar condiciones de acceso, licencia y reproducción."),
        )
        name = "Guía de Valores Fasecolda"
    else:
        conn.execute(
            """INSERT OR IGNORE INTO sources(name, domain, source_type, base_url, usage_notes)
               VALUES(?, ?, 'otra', ?, ?)""",
            (name, domain, f"https://{domain}/" if domain else "",
             "Fuente importada por administración; revisar condiciones de uso."),
        )
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return int(row[0])


def _ensure_source(
    conn: sqlite3.Connection,
    name: str,
    source_url: str,
    source_type: str,
    usage_notes: str = "",
    checked_at: str | None = None,
) -> int:
    parsed = urlparse(source_url or "")
    domain = parsed.netloc or ""
    conn.execute(
        """INSERT OR IGNORE INTO sources(name, domain, source_type, base_url, usage_notes, last_checked_at)
           VALUES(?,?,?,?,?,?)""",
        (name, domain, source_type, source_url or None, usage_notes or None, checked_at),
    )
    if checked_at:
        conn.execute("UPDATE sources SET last_checked_at=? WHERE name=?", (checked_at, name))
    row = conn.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
    return int(row["id"])


def _int_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError("Se esperaba un entero no negativo.")
    return int(text)


def _float_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Se esperaba un número finito no negativo.")
    return number


def _vehicle_values(row: dict) -> dict:
    year = int(row.get("model_year", ""))
    values = {
        "model_year": year,
        "generation": (row.get("generation") or "").strip() or None,
        "version_name": (row.get("version_name") or "").strip(),
        "engine_label": (row.get("engine_label") or "").strip(),
        "displacement_cc": _int_or_none(row.get("displacement_cc")),
        "fuel": (row.get("fuel") or "").strip() or None,
        "transmission": (row.get("transmission") or "").strip(),
        "body_type": (row.get("body_type") or "").strip() or None,
        "traction": (row.get("traction") or "").strip() or None,
        "horsepower": _float_or_none(row.get("horsepower")),
        "doors": _int_or_none(row.get("doors")),
        "passengers": _int_or_none(row.get("passengers")),
        "image_url": (row.get("image_url") or "").strip() or None,
        "image_source_url": (row.get("image_source_url") or "").strip() or None,
        "technical_source_url": (row.get("technical_source_url") or "").strip() or None,
        "technical_source_name": (row.get("technical_source_name") or "").strip() or None,
        "technical_source_checked_at": (row.get("technical_source_checked_at") or "").strip() or None,
        "verification_status": (row.get("verification_status") or "pendiente").strip().lower(),
        "notes": (row.get("notes") or "").strip() or None,
    }
    if not (1950 <= year <= 2100):
        raise ValueError("Año modelo fuera de rango")
    if not all([values["version_name"], values["engine_label"], values["transmission"]]):
        raise ValueError("Faltan versión, motor/energía o transmisión")
    if values["verification_status"] not in VALID_VEHICLE_STATUSES:
        raise ValueError("Estado de verificación inválido")
    if values["verification_status"] == "verificado":
        if not values["technical_source_url"] or not values["technical_source_url"].startswith("https://"):
            raise ValueError("Una versión verificada requiere una URL técnica HTTPS.")
        if not values["technical_source_name"] or not values["technical_source_checked_at"]:
            raise ValueError("Una versión verificada requiere nombre de fuente y fecha de consulta.")
    if values["technical_source_checked_at"]:
        from datetime import date
        checked = date.fromisoformat(values["technical_source_checked_at"])
        if checked.isoformat() != values["technical_source_checked_at"] or checked > date.today():
            raise ValueError("Fecha de consulta técnica inválida o futura.")
    return values


def _find_price_vehicle(conn, row: dict):
    """Encuentra una única versión compatible; no enlaza precios por similitud ambigua."""
    from catalog_utils import normalize_token

    try:
        year = int(row.get("model_year", ""))
    except (TypeError, ValueError):
        return None
    brand_key = normalize_token(row.get("brand"))
    model_key = normalize_token(row.get("model"))
    version_key = normalize_token(row.get("version_name"))
    engine_key = normalize_token(row.get("engine_label"))
    # La caja se compara por categoría («Mecánica 5 velocidades» y «Mecánica» son la misma caja).
    from transmissions import category_for_key
    transmission_key = category_for_key(row.get("transmission"))
    candidates = conn.execute(
        """SELECT v.id, v.model_year, b.name AS brand, m.name AS model, v.version_name,
                  v.engine_label, v.transmission
           FROM vehicle_versions v JOIN models m ON m.id=v.model_id
           JOIN brands b ON b.id=m.brand_id WHERE v.model_year=?""",
        (year,),
    ).fetchall()
    matches = [
        r for r in candidates
        if normalize_token(r["brand"]) == brand_key
        and normalize_token(r["model"]) == model_key
        and normalize_token(r["version_name"]) == version_key
        and normalize_token(r["engine_label"]) == engine_key
        and category_for_key(r["transmission"]) == transmission_key
    ]
    return matches[0] if len(matches) == 1 else None


def _load_vehicle_file(conn: sqlite3.Connection, path: Path, source_date: str | None = None) -> tuple[int, int]:
    added = skipped = 0
    if not path.exists():
        return added, skipped
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand_name = (row.get("brand") or "").strip()
            model_name = (row.get("model") or "").strip()
            if not brand_name or not model_name:
                skipped += 1
                continue
            try:
                values = _vehicle_values(row)
                brand_id = get_or_create_brand(conn, brand_name, "Internacional")
                model_id = get_or_create_model(
                    conn, brand_id, model_name, (row.get("category") or "Automóvil").strip() or "Automóvil"
                )
                duplicate_type, duplicate = find_version_duplicate(
                    conn, brand_name, model_name, model_id, values
                )
                if duplicate_type:
                    skipped += 1
                    continue
                canonical_key = vehicle_canonical_key(
                    brand_name, model_name, values["model_year"], values["generation"],
                    values["version_name"], values["engine_label"], values["fuel"], values["transmission"],
                )
                conn.execute(
                    """INSERT INTO vehicle_versions
                       (model_id, model_year, generation, version_name, engine_label, displacement_cc,
                        fuel, transmission, body_type, traction, horsepower, doors, passengers,
                        image_url, image_source_url, technical_source_url, technical_source_name,
                        technical_source_checked_at, canonical_key, verification_status, notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (model_id, values["model_year"], values["generation"], values["version_name"],
                     values["engine_label"], values["displacement_cc"], values["fuel"], values["transmission"],
                     values["body_type"], values["traction"], values["horsepower"], values["doors"],
                     values["passengers"], values["image_url"], values["image_source_url"],
                     values["technical_source_url"], values["technical_source_name"],
                     values["technical_source_checked_at"], canonical_key,
                     values["verification_status"], values["notes"]),
                )
                added += 1
                if values["technical_source_name"] and values["technical_source_url"]:
                    source_type = "anuncios_publicos" if "usados.kia.com.co" in values["technical_source_url"] else "ficha_tecnica"
                    _ensure_source(
                        conn, values["technical_source_name"], values["technical_source_url"],
                        source_type, values["notes"] or "", values["technical_source_checked_at"],
                    )
            except (ValueError, sqlite3.IntegrityError, TypeError):
                skipped += 1
    return added, skipped


def _load_price_file(conn: sqlite3.Connection, path: Path, source_date: str | None = None) -> tuple[int, int]:
    added = skipped = 0
    if not path.exists():
        return added, skipped
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                match = _find_price_vehicle(conn, row)
                if not match:
                    skipped += 1
                    continue
                price = int(row.get("price_cop", ""))
                year = int(row.get("model_year", ""))
                price_type = (row.get("price_type") or "precio_solicitado").strip().lower()
                verification_status = (row.get("verification_status") or "observado_no_verificado").strip().lower()
                source_url = (row.get("source_url") or "").strip()
                source_name = (row.get("source_name") or "").strip()
                observed_at = (row.get("observed_at") or "").strip()
                if price <= 0 or not source_name or not source_url.startswith("https://") or not observed_at:
                    skipped += 1
                    continue
                if price_type not in VALID_PRICE_TYPES or verification_status not in VALID_PRICE_STATUSES:
                    skipped += 1
                    continue
                source_type = (row.get("source_type") or "").strip() or (
                    "precio_nuevo" if price_type == "precio_nuevo" else "anuncios_publicos"
                )
                source_notes = (row.get("source_notes") or row.get("notes") or "").strip()
                source_id = _ensure_source(
                    conn, source_name, source_url, source_type, source_notes, observed_at
                )
                before = conn.total_changes
                conn.execute(
                    """INSERT OR IGNORE INTO price_references
                       (version_id, source_id, price_cop, price_type, model_year, mileage_km,
                        location, source_url, observed_at, verification_status, notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(match["id"]), source_id, price, price_type, year,
                     _int_or_none(row.get("mileage_km")), (row.get("location") or "").strip() or None,
                     source_url, observed_at, verification_status, (row.get("notes") or "").strip() or None),
                )
                if conn.total_changes > before:
                    added += 1
                else:
                    skipped += 1
            except (ValueError, sqlite3.IntegrityError, TypeError):
                skipped += 1
    return added, skipped


def seed_database(conn: sqlite3.Connection) -> None:
    """Carga datos iniciales y expansión oficial reproducible; no crea precios ficticios."""
    _ensure_source(
        conn, "Chevrolet Colombia — fichas técnicas", "https://www.chevrolet.com.co/fichas-tecnicas",
        "fichas_tecnicas", "Fichas oficiales colombianas por modelo y año; cada registro conserva la URL específica del documento.",
        "2026-10-26",
    )
    _ensure_source(
        conn, "Mazda Colombia — fichas técnicas", "https://www.mazda.com.co/",
        "fichas_tecnicas", "Las fichas incluidas en la expansión tienen fecha de publicación y año aplicable indicados en el propio documento.",
        "2026-10-26",
    )
    _ensure_source(
        conn, "Toyota Colombia — fichas técnicas", "https://www.toyota.com.co/",
        "fichas_tecnicas", "Fichas oficiales de modelos 2026; se conservan solo las configuraciones descritas en el documento.",
        "2026-10-26",
    )
    _ensure_source(
        conn, "Ford Colombia — fichas de producto", "https://www.ford.com.co/",
        "fichas_tecnicas", "Fichas oficiales de versiones; el año modelo 2027 se confirma en los avisos legales de precio.",
        "2026-10-26",
    )
    _source_id(conn, "Mercado Libre")
    _source_id(conn, "Guía de Valores Fasecolda")
    _ensure_source(
        conn, "Chevrolet Colombia — ficha técnica Spark GT",
        "https://www.chevrolet.com.co/content/dam/chevrolet/south-america/colombia/espanol/index/cars/01-pdfs/ficha-tecnica-spark-gt.pdf",
        "ficha_tecnica", "Ficha general; no confirma automáticamente cada versión y año modelo.", "2026-10-26",
    )
    _ensure_source(
        conn, "Renault Colombia — manuales y catálogos", "https://www.renault.com.co/servicios/manuales.html",
        "catalogo_tecnico", "Portal general; debe localizarse el documento exacto por modelo y año.", "2026-10-26",
    )

    _load_vehicle_file(conn, DATA_DIR / "seed_vehicles.csv", "2026-10-26")
    _load_vehicle_file(conn, DATA_DIR / "ampliacion_catalogo_oficial.csv", "2026-10-26")
    _load_vehicle_file(conn, DATA_DIR / "ampliacion_catalogo_mercado.csv", "2026-10-26")
    _load_price_file(conn, DATA_DIR / "seed_prices.csv", "2026-10-26")
    _load_price_file(conn, DATA_DIR / "ampliacion_precios_oficiales.csv", "2026-10-26")
    _load_price_file(conn, DATA_DIR / "ampliacion_precios_mercado.csv", "2026-10-26")
    conn.execute(
        "INSERT INTO audit_log(action, entity_type, details) VALUES('seed', 'database', ?)",
        ("Carga de muestra inicial y ampliación oficial trazable; precios nuevos separados de precios de usados; sin valores inventados.",),
    )
    conn.commit()
