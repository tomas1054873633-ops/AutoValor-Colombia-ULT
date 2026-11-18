"""API y aplicación web AutoValor Pro Colombia (FastAPI + SQLite)."""
from __future__ import annotations

import csv
import re
import hmac
import io
import math
import mimetypes
import os
import secrets
import time
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

# Tipografías locales: algunos sistemas (p. ej. Windows) no registran estos tipos MIME.
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from db import DEFAULT_DB, ROOT, _ensure_source, _find_price_vehicle, _source_id, connect_db, init_db
from catalog_utils import (find_potential_duplicate_pairs, find_version_duplicate, get_or_create_brand, get_or_create_model, normalize_token, vehicle_broad_key, vehicle_canonical_key)
from valuation import (body_style, estimate_vehicle, load_config, new_price_context, public_fuel,
                       public_view, valuation_available, value_history)
from catalog_selectors import (available_model_ids, available_years, engine_options, path_for_version, transmission_options, validate_combination,
                               version_options)
from transmissions import public_transmission
from transmission_audit import run_audit as run_transmission_audit

load_dotenv(ROOT / ".env")


def _backup_database(db_path: str | Path) -> Path | None:
    """Crea una copia íntegra antes de una importación masiva."""
    path = Path(db_path)
    if not path.exists():
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"{path.stem}_pre_import_{stamp}{path.suffix or '.sqlite'}"
    shutil.copy2(path, backup_path)
    return backup_path


def _normalized_brand_model_ids(conn: sqlite3.Connection, brand_name: str, model_name: str):
    brand_id = None
    for row in conn.execute("SELECT id, name FROM brands"):
        if normalize_token(row["name"]) == normalize_token(brand_name):
            brand_id = int(row["id"])
            break
    if brand_id is None:
        return None, None
    model_id = None
    for row in conn.execute("SELECT id, name FROM models WHERE brand_id=?", (brand_id,)):
        if normalize_token(row["name"]) == normalize_token(model_name):
            model_id = int(row["id"])
            break
    return brand_id, model_id


def create_app(test_config: dict | None = None) -> FastAPI:
    config = {
        "SECRET_KEY": os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        "DATABASE_PATH": os.environ.get("AUTOVALOR_DB", str(DEFAULT_DB)),
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", ""),
        "SESSION_COOKIE_SECURE": os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        "SEED_DATA": True,
        # La documentación automática de la API queda apagada salvo que se pida explícitamente.
        "API_DOCS": os.environ.get("AUTOVALOR_API_DOCS", "0") == "1",
        "ADMIN_MAX_ATTEMPTS": int(os.environ.get("ADMIN_MAX_ATTEMPTS", "5")),
        "ADMIN_LOCK_MINUTES": int(os.environ.get("ADMIN_LOCK_MINUTES", "15")),
    }
    if test_config:
        config.update(test_config)

    init_db(config["DATABASE_PATH"], seed=config.get("SEED_DATA", True))
    app = FastAPI(
        title="AutoValor Pro Colombia", version="1.0.0", redoc_url=None,
        docs_url="/api/docs" if config.get("API_DOCS") else None,
        openapi_url="/openapi.json" if config.get("API_DOCS") else None,
    )
    app.state.config = config
    app.state.database_path = config["DATABASE_PATH"]
    # Carpeta de informes de importación. En pruebas se usa una carpeta junto a la base
    # temporal para no mezclar informes de prueba con los reales del proyecto.
    if config.get("REPORTS_DIR"):
        app.state.reports_dir = Path(config["REPORTS_DIR"])
    elif config.get("TESTING"):
        app.state.reports_dir = Path(config["DATABASE_PATH"]).resolve().parent / "informes_importacion"
    else:
        app.state.reports_dir = ROOT / "reports" / "importaciones"
    app.add_middleware(
        SessionMiddleware,
        secret_key=config["SECRET_KEY"],
        same_site="lax",
        https_only=bool(config.get("SESSION_COOKIE_SECURE", False)),
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    templates = Jinja2Templates(directory=str(ROOT / "templates"))

    # Encabezados de seguridad para todas las respuestas. La política de contenido solo permite
    # recursos propios (la página no carga nada de terceros).
    content_security_policy = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if not request.url.path.startswith(("/api/docs", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", content_security_policy)
        if config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def friendly_http_errors(request: Request, exc: StarletteHTTPException):
        # Página 404 con el diseño del sitio; la API y los informes siguen respondiendo en JSON.
        if exc.status_code == 404 and not request.url.path.startswith(("/api/", "/admin/report", "/static/")):
            return templates.TemplateResponse(request=request, name="404.html", context={}, status_code=404)
        return await http_exception_handler(request, exc)

    # Límite de intentos fallidos de acceso al panel, por dirección IP (en memoria).
    login_failures: dict[str, list[float]] = {}

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "desconocido"

    def _login_locked(request: Request) -> bool:
        window = 60 * int(config.get("ADMIN_LOCK_MINUTES", 15))
        now = time.monotonic()
        recent = [t for t in login_failures.get(_client_key(request), []) if now - t < window]
        login_failures[_client_key(request)] = recent
        return len(recent) >= int(config.get("ADMIN_MAX_ATTEMPTS", 5))

    def _register_login_failure(request: Request) -> None:
        login_failures.setdefault(_client_key(request), []).append(time.monotonic())

    def format_cop(value):
        if value is None:
            return "—"
        return "$ " + f"{int(round(float(value))):,}".replace(",", ".") + " COP"

    def format_km(value):
        if value is None:
            return "No documentado"
        return f"{int(value):,}".replace(",", ".") + " km"

    templates.env.filters["cop"] = format_cop
    templates.env.filters["km"] = format_km

    def get_db():
        conn = connect_db(app.state.database_path)
        try:
            yield conn
        finally:
            conn.close()

    def flash(request: Request, message: str, category: str = "success") -> None:
        messages = request.session.get("_flashes", [])
        messages.append({"message": message, "category": category})
        request.session["_flashes"] = messages[-8:]

    def pop_flashes(request: Request) -> list[dict]:
        return request.session.pop("_flashes", [])

    def admin_ok(request: Request) -> bool:
        return bool(request.session.get("admin_logged_in"))

    def check_csrf(request: Request, form) -> None:
        supplied = str(form.get("csrf_token", ""))
        expected = str(request.session.get("csrf_token", ""))
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=400, detail="Token de seguridad inválido. Recarga el panel e inténtalo de nuevo.")

    def audit(conn, action: str, entity_type: str, entity_id: int | None, details: str):
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?, ?, ?, ?)",
            (action, entity_type, entity_id, details[:2000]),
        )
        conn.commit()

    def public_stats(conn):
        return {
            "brands": conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0],
            "models": conn.execute("SELECT COUNT(*) FROM models").fetchone()[0],
            "versions": conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0],
            "price_references": conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0],
        }

    MONTHS_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre")

    def _long_date_es(iso_text: str | None) -> str | None:
        try:
            d = datetime.strptime(str(iso_text)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        return f"{d.day} de {MONTHS_ES[d.month - 1]} de {d.year}"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        # Todo lo que la portada afirma sale de la base en cada visita: la fecha de los
        # valores vigentes, el kilometraje anual configurado y el tamaño del catálogo que se
        # puede consultar (marcas y líneas con al menos una versión, y el rango de años).
        # Así la publicidad de la valla nunca queda desactualizada ni exagera la cobertura.
        row = conn.execute(
            "SELECT MAX(observed_at) FROM price_references WHERE price_type='guia_comercial' AND import_run_id IS NOT NULL"
        ).fetchone()
        km_row = conn.execute("SELECT value FROM valuation_config WHERE key='km_expected_per_year'").fetchone()
        km_label = None
        if km_row:
            try:
                km_label = f"{int(float(km_row[0])):,}".replace(",", ".")
            except (TypeError, ValueError):
                km_label = None
        def _cfg_label(key: str) -> str | None:
            cfg_row = conn.execute("SELECT value FROM valuation_config WHERE key=?", (key,)).fetchone()
            try:
                return f"{float(cfg_row[0]):g}".replace(".", ",") if cfg_row else None
            except (TypeError, ValueError):
                return None

        cat = conn.execute(
            "SELECT COUNT(DISTINCT m.brand_id), COUNT(DISTINCT v.model_id), MIN(v.model_year), MAX(v.model_year) "
            "FROM vehicle_versions v JOIN models m ON m.id = v.model_id"
        ).fetchone()
        catalog = None
        if cat and cat[0]:
            year_min, year_max = cat[2], cat[3]
            years_label = (f"{year_min}\u2013{year_max}" if year_min and year_max and year_min != year_max
                           else (str(year_min) if year_min else None))
            catalog = {
                "brands_label": f"{int(cat[0]):,}".replace(",", "."),
                "models_label": f"{int(cat[1]):,}".replace(",", "."),
                "years_label": years_label,
            }

        context = {
            "catalog": catalog,
            "values_date_label": _long_date_es(row[0] if row else None),
            "km_per_year_label": km_label,
            "penalty_cap_label": _cfg_label("km_penalty_cap_pct"),
            "bonus_cap_label": _cfg_label("km_bonus_cap_pct"),
        }
        return templates.TemplateResponse(request=request, name="index.html", context=context)

    @app.get("/api/health")
    def api_health():
        return {"status": "ok", "database": "sqlite", "date": date.today().isoformat()}

    @app.get("/api/stats")
    def api_stats(conn: sqlite3.Connection = Depends(get_db)):
        return public_stats(conn)

    @app.get("/api/brands")
    def api_brands(q: str = "", conn: sqlite3.Connection = Depends(get_db)):
        q = q.strip()
        if q:
            rows = conn.execute(
                "SELECT id, name FROM brands WHERE name LIKE ? ORDER BY name",
                (f"%{q}%",),
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, name FROM brands ORDER BY name").fetchall()
        # Solo marcas con al menos una versión que se puede valorar
        models_ok = available_model_ids(conn)
        brands_ok = {r["brand_id"] for r in conn.execute("SELECT id, brand_id FROM models") if r["id"] in models_ok}
        return [dict(r) for r in rows if r["id"] in brands_ok]

    @app.get("/api/models")
    def api_models(
        brand_id: int | None = None, q: str = "", limit: int = 1000, offset: int = 0,
        conn: sqlite3.Connection = Depends(get_db)
    ):
        if not brand_id:
            return []
        limit = max(1, min(int(limit), 5000))
        offset = max(0, int(offset))
        sql = "SELECT id, name FROM models WHERE brand_id=?"
        args: list = [brand_id]
        if q.strip():
            sql += " AND name LIKE ?"
            args.append(f"%{q.strip()}%")
        sql += " ORDER BY name"
        models_ok = available_model_ids(conn)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall() if r["id"] in models_ok]
        return rows[offset:offset + limit]

    @app.get("/api/years")
    def api_years(model_id: int | None = None, conn: sqlite3.Connection = Depends(get_db)):
        if not model_id:
            return []
        exists = conn.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone()
        if not exists:
            return []
        return available_years(conn, model_id)

    @app.get("/api/engines")
    def api_engines(model_id: int | None = None, year: int | None = None, conn: sqlite3.Connection = Depends(get_db)):
        if not model_id or not year:
            return []
        # Motores equivalentes agrupados: la misma cilindrada no se parte en varias opciones.
        return [{**e, "fuel": public_fuel(e["fuel"])} for e in engine_options(conn, model_id, year)]

    @app.get("/api/transmissions")
    def api_transmissions(
        model_id: int | None = None, year: int | None = None, engine_id: int | None = None,
        conn: sqlite3.Connection = Depends(get_db)
    ):
        if not model_id or not year or not engine_id:
            return []
        # Categorías normalizadas (Mecánica / Automática); las versiones cuya caja no se ha podido
        # confirmar se devuelven aparte («confirmed»: false), nunca como una caja válida.
        return transmission_options(conn, model_id, year, engine_id)

    @app.get("/api/versions")
    def api_versions(
        model_id: int | None = None, year: int | None = None, engine_id: int | None = None,
        transmission_id: int | None = None, conn: sqlite3.Connection = Depends(get_db)
    ):
        if not model_id or not year or not engine_id or not transmission_id:
            return []
        rows = version_options(conn, model_id, year, engine_id, transmission_id)
        if not rows:
            return []
        # Respuesta pública: sin procedencia de los datos; solo si la versión se puede valorar.
        cfg = load_config(conn)
        today = date.today()
        out = []
        for r in rows:
            item = {k: r[k] for k in ("id", "version_name", "engine_label", "fuel", "displacement_cc", "verification_status")}
            item.update(public_transmission(r))
            item["fuel"] = public_fuel(r["fuel"])
            item["valuation_available"] = valuation_available(conn, r["id"], cfg, today)
            out.append(item)
        return out

    @app.get("/api/search")
    def api_search(q: str = "", limit: int = 20, conn: sqlite3.Connection = Depends(get_db)):
        """Búsqueda libre sobre el catálogo real: devuelve la ruta completa para rellenar los selects."""
        terms = [t for t in re.split(r"\s+", q.strip()) if t][:6]
        if not terms or sum(len(t) for t in terms) < 2:
            return []
        limit = max(1, min(int(limit), 40))
        clauses, args = [], []
        for t in terms:
            if re.fullmatch(r"(19|20)\d\d", t):
                clauses.append("v.model_year = ?")
                args.append(int(t))
            else:
                clauses.append("(b.name LIKE ? OR m.name LIKE ? OR v.version_name LIKE ? OR v.engine_label LIKE ?)")
                like = f"%{t}%"
                args.extend([like, like, like, like])
        rows = conn.execute(
            f"""SELECT v.id AS version_id, v.fuel,
                       EXISTS(SELECT 1 FROM price_references p WHERE p.version_id=v.id AND p.price_type='guia_comercial'
                              AND p.verification_status IN ('verificado','parcial')) AS has_guide
                FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
                WHERE {' AND '.join(clauses)}
                ORDER BY has_guide DESC, b.name, m.name, v.model_year DESC, v.version_name LIMIT ?""",
            (*args, limit),
        ).fetchall()
        cfg = load_config(conn)
        today = date.today()
        results = []
        for r in rows:
            item = path_for_version(conn, r["version_id"])
            if not item:
                continue
            item["fuel"] = public_fuel(r["fuel"])
            item["valuation_available"] = valuation_available(conn, item["version_id"], cfg, today)
            results.append(item)
        return results

    @app.get("/api/version/{version_id}")
    def api_version(version_id: int, conn: sqlite3.Connection = Depends(get_db)):
        """Ruta completa de una versión (para abrir enlaces compartidos y consultas recientes)."""
        item = path_for_version(conn, version_id)
        if not item:
            return JSONResponse({"error": "La versión no existe en el catálogo."}, status_code=404)
        item["valuation_available"] = valuation_available(conn, version_id)
        return item

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(str(ROOT / "static" / "img" / "favicon-32.png"), media_type="image/png")

    @app.post("/api/estimate")
    async def api_estimate(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        try:
            data = await request.json()
            version_id = _strict_int(data.get("version_id"))
            mileage = _strict_int(data.get("mileage_km"))
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "Selecciona una versión válida e ingresa el kilometraje como número entero."}, status_code=400)
        if version_id <= 0 or mileage < 0:
            return JSONResponse({"error": "La versión debe ser válida y el kilometraje no puede ser negativo."}, status_code=400)
        # El servidor valida la misma combinación que ofrece el formulario (marca, línea, año,
        # motor, transmisión y versión): una caja o un motor que no corresponden se rechazan.
        try:
            combo = {k: (None if data.get(k) in (None, "") else _strict_int(data.get(k)))
                     for k in ("brand_id", "model_id", "model_year", "engine_id", "transmission_id")}
        except (ValueError, TypeError):
            return JSONResponse({"error": "La combinación enviada no es válida."}, status_code=400)
        problem = validate_combination(conn, version_id, **combo)
        if problem:
            status = 404 if problem.startswith("La versión no existe") else 422
            return JSONResponse({"error": problem + " Vuelve a elegir las opciones del formulario."}, status_code=status)
        result = estimate_vehicle(conn, version_id, mileage)
        if result.get("error"):
            return JSONResponse(result, status_code=404)
        # La página pública recibe la valoración y la explicación del kilometraje, sin la procedencia
        # de los valores de referencia (que sigue registrada en la base y en el panel administrativo).
        result["body_style"] = body_style(conn, result["vehicle"])
        payload = public_view(result)
        payload["history"] = value_history(conn, version_id)
        payload["new_price"] = new_price_context(conn, version_id)
        return JSONResponse(payload)

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login(request: Request):
        password = str(app.state.config.get("ADMIN_PASSWORD", ""))
        configured = bool(password) and not password.startswith("CAMBIE_")
        return templates.TemplateResponse(request=request, name="admin_login.html", context={"configured": configured, "messages": pop_flashes(request)})

    @app.post("/admin/login")
    async def admin_login_post(request: Request):
        form = await request.form()
        password = str(app.state.config.get("ADMIN_PASSWORD", ""))
        if not password or password.startswith("CAMBIE_"):
            flash(request, "El panel está bloqueado hasta configurar ADMIN_PASSWORD en el archivo .env.", "error")
            return RedirectResponse("/admin/login", status_code=303)
        if _login_locked(request):
            flash(request, f"Demasiados intentos fallidos. Espera {int(config.get('ADMIN_LOCK_MINUTES', 15))} minutos e inténtalo de nuevo.", "error")
            return RedirectResponse("/admin/login", status_code=303)
        supplied = str(form.get("password", ""))
        if not hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8")):
            _register_login_failure(request)
            flash(request, "Contraseña incorrecta.", "error")
            return RedirectResponse("/admin/login", status_code=303)
        login_failures.pop(_client_key(request), None)
        request.session.clear()
        request.session["admin_logged_in"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/logout")
    async def admin_logout(request: Request):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        stats = public_stats(conn)
        stats["versions_without_prices"] = conn.execute(
            """SELECT COUNT(*) FROM vehicle_versions v WHERE NOT EXISTS
               (SELECT 1 FROM price_references p WHERE p.version_id=v.id)"""
        ).fetchone()[0]
        stats["stale_prices"] = conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE julianday('now') - julianday(observed_at) > 90"
        ).fetchone()[0]
        stats["incomplete_versions"] = conn.execute(
            """SELECT COUNT(*) FROM vehicle_versions WHERE fuel IS NULL OR body_type IS NULL
               OR technical_source_url IS NULL OR engine_label IS NULL OR transmission IS NULL
               OR technical_source_checked_at IS NULL"""
        ).fetchone()[0]
        stats["pending_verification"] = conn.execute(
            "SELECT COUNT(*) FROM vehicle_versions WHERE verification_status IN ('pendiente','parcial')"
        ).fetchone()[0]
        stats["versions_with_prices"] = conn.execute(
            """SELECT COUNT(*) FROM vehicle_versions v WHERE EXISTS
               (SELECT 1 FROM price_references p WHERE p.version_id=v.id)"""
        ).fetchone()[0]
        stats["new_price_references"] = conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE price_type='precio_nuevo'"
        ).fetchone()[0]
        stats["used_price_references"] = conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE price_type IN ('precio_solicitado','transaccion_documentada','guia_comercial')"
        ).fetchone()[0]
        stats["year_min"] = conn.execute("SELECT MIN(model_year) FROM vehicle_versions").fetchone()[0]
        stats["year_max"] = conn.execute("SELECT MAX(model_year) FROM vehicle_versions").fetchone()[0]
        versions = conn.execute(
            """SELECT v.id, b.name AS brand, m.name AS model, v.model_year, v.generation, v.version_name,
                      v.engine_label, v.transmission, v.transmission_status, v.verification_status, v.technical_source_url,
                      v.technical_source_name, v.technical_source_checked_at,
                      (SELECT COUNT(*) FROM price_references p WHERE p.version_id=v.id) AS price_count
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               ORDER BY b.name,m.name,v.model_year DESC,v.version_name LIMIT 100"""
        ).fetchall()
        sources = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
        config_rows = conn.execute("SELECT key, value, description FROM valuation_config ORDER BY key").fetchall()
        valuation_settings = {r["key"]: r["value"] for r in config_rows}
        prices = conn.execute(
            """SELECT p.id, p.price_cop, p.price_type, p.observed_at, p.mileage_km, p.location,
                      p.verification_status, p.source_url, s.name AS source_name,
                      b.name AS brand, m.name AS model, v.model_year, v.version_name
               FROM price_references p JOIN sources s ON s.id=p.source_id
               JOIN vehicle_versions v ON v.id=p.version_id JOIN models m ON m.id=v.model_id
               JOIN brands b ON b.id=m.brand_id ORDER BY p.observed_at DESC, p.id DESC LIMIT 50"""
        ).fetchall()
        import_runs = conn.execute(
            """SELECT r.id, r.importer, r.source_name, r.source_file_name, r.source_edition_date, r.started_at,
                      r.finished_at, r.status,
                      (SELECT COUNT(*) FROM import_issues i WHERE i.run_id=r.id AND i.severity='pendiente') AS pending,
                      (SELECT COUNT(*) FROM import_issues i WHERE i.run_id=r.id AND i.severity='descartado') AS discarded,
                      (SELECT COUNT(*) FROM import_issues i WHERE i.run_id=r.id AND i.severity='conflicto') AS conflicts,
                      (SELECT COUNT(*) FROM import_issues i WHERE i.run_id=r.id AND i.severity='advertencia') AS warnings,
                      (SELECT COUNT(*) FROM price_references p WHERE p.import_run_id=r.id) AS prices_added,
                      (SELECT COUNT(*) FROM vehicle_versions v WHERE v.origin_run_id=r.id) AS versions_added
               FROM import_runs r ORDER BY r.id DESC LIMIT 20"""
        ).fetchall()
        stats["guide_price_references"] = conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE price_type='guia_comercial'").fetchone()[0]
        stats["ads_price_references"] = conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE price_type='precio_solicitado'").fetchone()[0]
        return templates.TemplateResponse(request=request, name="admin.html", context={
            "stats": stats, "versions": versions, "sources": sources, "prices": prices, "import_runs": import_runs,
            "valuation_settings": valuation_settings,
            "csrf_token": request.session.get("csrf_token"), "today": date.today().isoformat(),
            "messages": pop_flashes(request),
        })

    @app.post("/admin/vehicles")
    async def admin_add_vehicle(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        brand = str(form.get("brand", "")).strip()
        model = str(form.get("model", "")).strip()
        version_name = str(form.get("version_name", "")).strip()
        engine = str(form.get("engine_label", "")).strip()
        transmission = str(form.get("transmission", "")).strip()
        try:
            year = int(form.get("model_year", ""))
        except (ValueError, TypeError):
            year = 0
        source_url = str(form.get("technical_source_url", "")).strip()
        source_checked = str(form.get("technical_source_checked_at", "")).strip() or None
        status = str(form.get("verification_status", "pendiente")).strip().lower()
        if not all([brand, model, version_name, engine, transmission]) or not (1950 <= year <= date.today().year + 2):
            flash(request, "Completa marca, línea, año válido, motor, transmisión y versión.", "error")
            return RedirectResponse("/admin#vehiculos", status_code=303)
        if source_url and not _is_https_url(source_url):
            flash(request, "La URL de la ficha técnica debe usar HTTPS.", "error")
            return RedirectResponse("/admin#vehiculos", status_code=303)
        if source_checked and not _valid_observed_date(source_checked):
            flash(request, "La fecha de consulta técnica debe ser ISO y no futura.", "error")
            return RedirectResponse("/admin#vehiculos", status_code=303)
        if status not in {"pendiente", "parcial", "verificado", "rechazado"}:
            flash(request, "Estado de verificación inválido.", "error")
            return RedirectResponse("/admin#vehiculos", status_code=303)
        source_name_form = str(form.get("technical_source_name", "")).strip()
        if status == "verificado" and (not source_url or not source_name_form or not source_checked):
            flash(request, "Para marcar una versión como verificada debes indicar URL HTTPS, nombre de fuente y fecha de consulta.", "error")
            return RedirectResponse("/admin#vehiculos", status_code=303)
        try:
            brand_id = get_or_create_brand(conn, brand)
            model_id = get_or_create_model(conn, brand_id, model, str(form.get("category", "Automóvil")).strip() or "Automóvil")
            values = {
                "model_year": year,
                "generation": str(form.get("generation", "")).strip() or None,
                "version_name": version_name,
                "engine_label": engine,
                "displacement_cc": _form_int_or_none(form.get("displacement_cc")),
                "fuel": str(form.get("fuel", "")).strip() or None,
                "transmission": transmission,
            }
            duplicate_type, duplicate = find_version_duplicate(conn, brand, model, model_id, values)
            if duplicate_type:
                conn.rollback()
                if duplicate_type == "exacto":
                    flash(request, f"No se agregó: la versión coincide con el registro existente ID {duplicate['id']}.", "error")
                else:
                    flash(request, f"No se agregó: posible duplicado con ID {duplicate['id']}; revisa la generación antes de incorporarlo.", "error")
                return RedirectResponse("/admin#vehiculos", status_code=303)
            canonical_key = vehicle_canonical_key(
                brand, model, year, values["generation"], version_name, engine,
                values["fuel"], transmission,
            )
            conn.execute(
                """INSERT INTO vehicle_versions(model_id,model_year,generation,version_name,engine_label,displacement_cc,
                   fuel,transmission,body_type,traction,horsepower,doors,passengers,technical_source_url,
                   technical_source_name,technical_source_checked_at,canonical_key,verification_status,notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (model_id, year, values["generation"], version_name, engine,
                 values["displacement_cc"], values["fuel"], transmission,
                 str(form.get("body_type", "")).strip() or None, str(form.get("traction", "")).strip() or None,
                 _form_float_or_none(form.get("horsepower")), _form_int_or_none(form.get("doors")),
                 _form_int_or_none(form.get("passengers")), source_url or None,
                 str(form.get("technical_source_name", "")).strip() or None, source_checked,
                 canonical_key, status, str(form.get("notes", "")).strip() or None),
            )
            version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            source_name = str(form.get("technical_source_name", "")).strip()
            if source_name and source_url:
                _ensure_source(conn, source_name, source_url, "ficha_tecnica",
                               str(form.get("notes", "")).strip(), source_checked)
            conn.commit()
            # Normaliza la caja escrita en el formulario («Manual 5 vel.» → «Mecánica», 5 velocidades),
            # conserva el texto original y registra el cambio en la auditoría de transmisiones.
            run_transmission_audit(conn, apply=True, with_integrity=False)
            audit(conn, "create", "vehicle_version", int(version_id), f"Creada versión {brand} {model} {year} {version_name}")
            flash(request, "Versión agregada. No se creó ningún precio automáticamente.", "success")
        except (ValueError, sqlite3.IntegrityError, TypeError) as exc:
            conn.rollback()
            flash(request, f"No se agregó la versión: revisa los datos y los duplicados. Detalle: {exc}", "error")
        return RedirectResponse("/admin#vehiculos", status_code=303)

    @app.post("/admin/prices")
    async def admin_add_price(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        try:
            version_id = int(form.get("version_id", ""))
            price = int(form.get("price_cop", ""))
            year = int(form.get("model_year", ""))
            mileage = _form_int_or_none(form.get("mileage_km"))
        except (ValueError, TypeError):
            flash(request, "Versión, año, precio y kilometraje deben ser números válidos.", "error")
            return RedirectResponse("/admin#precios", status_code=303)
        source_url = str(form.get("source_url", "")).strip()
        observed_at = str(form.get("observed_at", "")).strip()
        source_name = str(form.get("source_name", "")).strip()
        price_type = str(form.get("price_type", "precio_solicitado")).strip()
        verification_status = str(form.get("verification_status", "observado_no_verificado")).strip()
        if (price <= 0 or year < 1950 or mileage is not None and mileage < 0
                or not _is_https_url(source_url) or not observed_at or not source_name
                or not _valid_observed_date(observed_at)
                or price_type not in {"precio_solicitado","transaccion_documentada","guia_comercial","valor_fiscal","precio_nuevo","otro"}
                or verification_status not in {"observado_no_verificado","parcial","verificado","expirado","rechazado"}):
            flash(request, "Precio, año, kilometraje válido, fuente, URL HTTPS, fecha y tipo/estado de precio son obligatorios.", "error")
            return RedirectResponse("/admin#precios", status_code=303)
        version = conn.execute("SELECT model_year FROM vehicle_versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            flash(request, "La versión indicada no existe.", "error")
            return RedirectResponse("/admin#precios", status_code=303)
        if int(version["model_year"]) != year:
            flash(request, "El año del precio no coincide con el año modelo de la versión seleccionada.", "error")
            return RedirectResponse("/admin#precios", status_code=303)
        try:
            source_type = "precio_nuevo" if price_type == "precio_nuevo" else (
                "guia_comercial" if price_type == "guia_comercial" else "anuncios_publicos"
            )
            source_id = _ensure_source(
                conn, source_name, source_url, source_type,
                str(form.get("notes", "")).strip(), observed_at
            )
            conn.execute(
                """INSERT INTO price_references(version_id,source_id,price_cop,price_type,model_year,mileage_km,
                   location,source_url,observed_at,verification_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, source_id, price, price_type, year, mileage,
                 str(form.get("location", "")).strip() or None, source_url, observed_at,
                 verification_status, str(form.get("notes", "")).strip() or None),
            )
            price_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            audit(conn, "append", "price_reference", int(price_id), f"Añadida referencia por {price} COP; {source_url}")
            flash(request, "Referencia de precio añadida al historial. No se sobrescribieron referencias anteriores.", "success")
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            flash(request, f"No se añadió la referencia: puede ser un duplicado o tener datos incompatibles. {exc}", "error")
        return RedirectResponse("/admin#precios", status_code=303)

    CONFIG_SPEC = {
        # clave: (tipo, mínimo, máximo, etiqueta)
        "km_expected_per_year": (int, 1, 60000, "Km esperados por año"),
        "km_deadband_pct": (float, 0, 50, "Zona sin ajuste (% del esperado)"),
        "km_deadband_floor_km": (int, 0, 50000, "Zona sin ajuste mínima (km)"),
        "km_scale_floor": (int, 1000, 100000, "Escala mínima (km)"),
        "km_penalty_cap_pct": (float, 0, 40, "Tope de descuento por km altos (%)"),
        "km_bonus_cap_pct": (float, 0, 20, "Tope de bonificación por km bajos (%)"),
        "km_curve_steepness": (float, 0.1, 5, "Pendiente de la curva"),
        "km_atypical_high_factor": (float, 1.2, 10, "Km atípico alto (× esperado)"),
        "km_atypical_low_factor": (float, 0, 1, "Km atípico bajo (× esperado)"),
        "minimum_prices": (int, 3, 100, "Mínimo de anuncios para estimar"),
        "freshness_days": (int, 1, 3650, "Vigencia de anuncios (días)"),
        "guide_max_age_days": (int, 1, 3650, "Vigencia de la guía (días)"),
        "guide_vs_ads_warning_pct": (float, 1, 100, "Aviso guía vs. anuncios (%)"),
        "valuation_rounding_cop": (int, 0, 10_000_000, "Redondeo de la valoración (COP)"),
    }

    @app.post("/admin/config")
    async def admin_config(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        values = {}
        try:
            for key, (kind, low, high, label) in CONFIG_SPEC.items():
                raw = str(form.get(key, "")).strip().replace(",", ".")
                try:
                    number = float(raw)
                except ValueError:
                    raise ValueError(f"{label}: ingresa un número válido.")
                if not math.isfinite(number) or not (low <= number <= high):
                    raise ValueError(f"{label}: debe estar entre {low} y {high}.")
                if kind is int:
                    if number != int(number):
                        raise ValueError(f"{label}: debe ser un número entero.")
                    number = int(number)
                values[key] = number
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/admin#configuracion", status_code=303)
        for key, value in values.items():
            conn.execute(
                "INSERT INTO valuation_config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        conn.commit()
        audit(conn, "update", "valuation_config", None, "Parámetros de valoración actualizados: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        flash(request, "Parámetros de valoración guardados. Los cambios se aplican a las próximas consultas.", "success")
        return RedirectResponse("/admin#configuracion", status_code=303)

    @app.post("/admin/verify")
    async def admin_verify(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        try:
            version_id = int(form.get("version_id", ""))
        except ValueError:
            flash(request, "Versión inválida.", "error")
            return RedirectResponse("/admin#catalogo", status_code=303)
        status = str(form.get("verification_status", "pendiente"))
        if status not in {"pendiente", "parcial", "verificado", "rechazado"}:
            flash(request, "Estado de verificación inválido.", "error")
            return RedirectResponse("/admin#catalogo", status_code=303)
        cur = conn.execute("UPDATE vehicle_versions SET verification_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, version_id))
        conn.commit()
        if cur.rowcount:
            audit(conn, "update_verification", "vehicle_version", version_id, f"Estado cambiado a {status}")
            flash(request, "Estado de verificación actualizado; las referencias de precio se conservaron.", "success")
        else:
            flash(request, "No se encontró la versión.", "error")
        return RedirectResponse("/admin#catalogo", status_code=303)

    @app.post("/admin/import/vehicles")
    async def admin_import_vehicles(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            flash(request, "Selecciona un archivo CSV.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        if not str(uploaded.filename).lower().endswith(".csv"):
            flash(request, "Solo se aceptan archivos CSV.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        content = await uploaded.read()
        await uploaded.close()
        if len(content) > 2 * 1024 * 1024:
            flash(request, "El CSV supera el límite de 2 MB.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            required = {"brand", "model", "model_year", "version_name", "engine_label", "transmission"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError("Faltan columnas obligatorias: brand, model, model_year, version_name, engine_label, transmission.")
            rows = list(reader)
            if not rows:
                raise ValueError("El CSV no contiene filas para validar.")
            dry_run = str(form.get("dry_run", "")) == "1"
            seen_keys, seen_broad = set(), set()
            prepared = []
            exact_duplicates = possible_duplicates = errors_count = new_candidates = 0
            for line_no, row in enumerate(rows, start=2):
                try:
                    brand = (row.get("brand") or "").strip()
                    model = (row.get("model") or "").strip()
                    version_name = (row.get("version_name") or "").strip()
                    engine = (row.get("engine_label") or "").strip()
                    transmission = (row.get("transmission") or "").strip()
                    year = int(row.get("model_year", ""))
                    generation = (row.get("generation") or "").strip() or None
                    fuel = (row.get("fuel") or "").strip() or None
                    status = (row.get("verification_status") or "pendiente").strip().lower()
                    source_url = (row.get("technical_source_url") or "").strip()
                    source_checked = (row.get("technical_source_checked_at") or "").strip()
                    if not all([brand, model, version_name, engine, transmission]) or not (1950 <= year <= date.today().year + 2):
                        raise ValueError("Campos obligatorios vacíos o año modelo fuera de rango.")
                    if status not in {"pendiente", "parcial", "verificado", "rechazado"}:
                        raise ValueError("Estado de verificación inválido.")
                    if source_url and not _is_https_url(source_url):
                        raise ValueError("La URL de la fuente técnica debe ser HTTPS.")
                    if source_checked and not _valid_observed_date(source_checked):
                        raise ValueError("La fecha de consulta técnica debe ser ISO y no futura.")
                    # Validar los campos opcionales durante la previsualización; no truncar decimales.
                    _form_int_or_none(row.get("displacement_cc"))
                    _form_float_or_none(row.get("horsepower"))
                    _form_int_or_none(row.get("doors"))
                    _form_int_or_none(row.get("passengers"))
                    source_name = (row.get("technical_source_name") or "").strip()
                    if status == "verificado" and (not source_url or not source_name or not source_checked):
                        raise ValueError("Una versión verificada requiere URL HTTPS, nombre de fuente y fecha de consulta.")
                    values = {
                        "model_year": year, "generation": generation, "version_name": version_name,
                        "engine_label": engine, "fuel": fuel, "transmission": transmission,
                    }
                    canonical = vehicle_canonical_key(brand, model, year, generation, version_name, engine, fuel, transmission)
                    broad = vehicle_broad_key(brand, model, year, version_name, engine, fuel, transmission)
                    if canonical in seen_keys:
                        exact_duplicates += 1
                        prepared.append({"line": line_no, "row": row, "values": values, "canonical": canonical, "broad": broad, "status": "duplicado", "detail": "Duplicado exacto dentro del CSV."})
                        continue
                    if broad in seen_broad:
                        possible_duplicates += 1
                        prepared.append({"line": line_no, "row": row, "values": values, "canonical": canonical, "broad": broad, "status": "posible_duplicado", "detail": "Coincide con una fila anterior salvo generación; requiere revisión."})
                        seen_keys.add(canonical)
                        continue
                    brand_id, model_id = _normalized_brand_model_ids(conn, brand, model)
                    if model_id:
                        duplicate_type, duplicate = find_version_duplicate(conn, brand, model, model_id, values)
                        if duplicate_type == "exacto":
                            exact_duplicates += 1
                            prepared.append({"line": line_no, "row": row, "values": values, "canonical": canonical, "broad": broad, "status": "duplicado", "detail": f"Ya existe la versión ID {duplicate['id']}."})
                            seen_keys.add(canonical)
                            seen_broad.add(broad)
                            continue
                        if duplicate_type == "posible":
                            possible_duplicates += 1
                            prepared.append({"line": line_no, "row": row, "values": values, "canonical": canonical, "broad": broad, "status": "posible_duplicado", "detail": f"Posible duplicado del ID {duplicate['id']}; revisar generación."})
                            seen_keys.add(canonical)
                            seen_broad.add(broad)
                            continue
                    prepared.append({"line": line_no, "row": row, "values": values, "canonical": canonical, "broad": broad, "status": "nuevo", "detail": "Cumple validación básica; no reemplaza datos existentes."})
                    new_candidates += 1
                    seen_keys.add(canonical)
                    seen_broad.add(broad)
                except (ValueError, TypeError, OverflowError) as exc:
                    errors_count += 1
                    prepared.append({"line": line_no, "row": row, "values": None, "canonical": "", "broad": "", "status": "error", "detail": str(exc)[:180]})

            if dry_run:
                report_path = Path(app.state.reports_dir)
                report_path.mkdir(parents=True, exist_ok=True)
                report_file = report_path / f"validacion_vehiculos_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.csv"
                with report_file.open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=["fila_csv", "estado", "marca", "modelo", "año", "versión", "detalle"])
                    writer.writeheader()
                    for item in prepared:
                        row = item["row"]
                        writer.writerow({"fila_csv": item["line"], "estado": item["status"], "marca": row.get("brand",""), "modelo": row.get("model",""), "año": row.get("model_year",""), "versión": row.get("version_name",""), "detalle": item["detail"]})
                flash(request, f"Validación terminada sin modificar la base de datos: {new_candidates} candidato(s) nuevo(s), {exact_duplicates} duplicado(s), {possible_duplicates} posible(s) duplicado(s), {errors_count} error(es). Informe: {report_file.name}.", "success" if errors_count == 0 and possible_duplicates == 0 else "error")
                return RedirectResponse("/admin#importar", status_code=303)

            backup_path = _backup_database(app.state.database_path)
            added = skipped = possible_skipped = 0
            errors = []
            report_path = Path(app.state.reports_dir)
            report_path.mkdir(parents=True, exist_ok=True)
            report_file = report_path / f"importacion_vehiculos_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.csv"
            for item in prepared:
                line_no, row = item["line"], item["row"]
                if item["status"] != "nuevo":
                    item["actual_status"] = item["status"]
                    if item["status"] == "duplicado":
                        skipped += 1
                    elif item["status"] == "posible_duplicado":
                        possible_skipped += 1
                    else:
                        errors.append(f"Fila {line_no}: {item['detail']}")
                    continue
                conn.execute("SAVEPOINT import_vehicle_row")
                try:
                    brand = (row.get("brand") or "").strip()
                    model = (row.get("model") or "").strip()
                    values = item["values"]
                    brand_id = get_or_create_brand(conn, brand)
                    model_id = get_or_create_model(conn, brand_id, model, (row.get("category") or "Automóvil").strip() or "Automóvil")
                    duplicate_type, duplicate = find_version_duplicate(conn, brand, model, model_id, values)
                    if duplicate_type:
                        conn.execute("ROLLBACK TO SAVEPOINT import_vehicle_row")
                        conn.execute("RELEASE SAVEPOINT import_vehicle_row")
                        if duplicate_type == "exacto":
                            skipped += 1
                            item["actual_status"] = "duplicado"
                        else:
                            possible_skipped += 1
                            item["actual_status"] = "posible_duplicado"
                        continue
                    canonical = item["canonical"]
                    cursor = conn.execute(
                        """INSERT INTO vehicle_versions(model_id,model_year,generation,version_name,engine_label,displacement_cc,
                           fuel,transmission,body_type,traction,horsepower,doors,passengers,image_url,image_source_url,
                           technical_source_url,technical_source_name,technical_source_checked_at,canonical_key,verification_status,notes)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (model_id, values["model_year"], values["generation"], values["version_name"], values["engine_label"],
                         _form_int_or_none(row.get("displacement_cc")), values["fuel"], values["transmission"],
                         (row.get("body_type") or "").strip() or None, (row.get("traction") or "").strip() or None,
                         _form_float_or_none(row.get("horsepower")), _form_int_or_none(row.get("doors")),
                         _form_int_or_none(row.get("passengers")), (row.get("image_url") or "").strip() or None,
                         (row.get("image_source_url") or "").strip() or None, (row.get("technical_source_url") or "").strip() or None,
                         (row.get("technical_source_name") or "").strip() or None,
                         (row.get("technical_source_checked_at") or "").strip() or None, canonical,
                         (row.get("verification_status") or "pendiente").strip().lower(), (row.get("notes") or "").strip() or None),
                    )
                    version_id = int(cursor.lastrowid)
                    source_name = (row.get("technical_source_name") or "").strip()
                    source_url = (row.get("technical_source_url") or "").strip()
                    if source_name and source_url:
                        source_type = "anuncios_publicos" if "usados.kia.com.co" in source_url else "ficha_tecnica"
                        _ensure_source(conn, source_name, source_url, source_type,
                                       (row.get("notes") or "").strip(), (row.get("technical_source_checked_at") or "").strip() or None)
                    conn.execute("RELEASE SAVEPOINT import_vehicle_row")
                    added += 1
                    item["actual_status"] = "insertado"
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT import_vehicle_row")
                    conn.execute("RELEASE SAVEPOINT import_vehicle_row")
                    item["actual_status"] = "error"
                    item["detail"] = str(exc)[:180]
                    errors.append(f"Fila {line_no}: {str(exc)[:180]}")
            conn.commit()
            if added:
                # Las versiones nuevas reciben la misma normalización de caja que el resto del catálogo:
                # se conserva el texto original y la caja queda verificada, documentada o sin confirmar.
                run_transmission_audit(conn, apply=True, with_integrity=False)
            audit(conn, "import_csv", "vehicle_versions", None, f"Archivo {Path(uploaded.filename).name}: agregados={added}, duplicados={skipped}, posibles={possible_skipped}, errores={len(errors)}")
            with report_file.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["fila_csv", "estado", "marca", "modelo", "año", "versión", "detalle"])
                writer.writeheader()
                for item in prepared:
                    row = item["row"]
                    status = item.get("actual_status", item["status"])
                    writer.writerow({"fila_csv": item["line"], "estado": status, "marca": row.get("brand",""), "modelo": row.get("model",""), "año": row.get("model_year",""), "versión": row.get("version_name",""), "detalle": item["detail"]})
            backup_text = f" Copia de seguridad: {backup_path.name}." if backup_path else ""
            msg = f"Importación de vehículos terminada: {added} agregado(s), {skipped} duplicado(s), {possible_skipped} posible(s) duplicado(s) enviados a revisión, {len(errors)} error(es). Informe: {report_file.name}.{backup_text}"
            flash(request, msg + (" Primeros errores: " + " | ".join(errors[:4]) if errors else ""), "success" if not errors else "error")
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            flash(request, f"No se pudo procesar el CSV: {exc}", "error")
        return RedirectResponse("/admin#importar", status_code=303)

    @app.post("/admin/import/prices")
    async def admin_import_prices(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        check_csrf(request, form)
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            flash(request, "Selecciona un archivo CSV.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        if not str(uploaded.filename).lower().endswith(".csv"):
            flash(request, "Solo se aceptan archivos CSV.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        content = await uploaded.read()
        await uploaded.close()
        if len(content) > 2 * 1024 * 1024:
            flash(request, "El CSV supera el límite de 2 MB.", "error")
            return RedirectResponse("/admin#importar", status_code=303)
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            required = {"brand", "model", "model_year", "version_name", "engine_label", "transmission", "price_cop", "source_name", "source_url", "observed_at"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError("Faltan columnas obligatorias para precios. Usa la plantilla precios.csv.")
            rows = list(reader)
            if not rows:
                raise ValueError("El CSV no contiene filas para validar.")
            dry_run = str(form.get("dry_run", "")) == "1"
            seen = set()
            prepared = []
            exact_duplicates = errors_count = new_candidates = unmatched = 0
            for line_no, row in enumerate(rows, start=2):
                try:
                    price = int(row.get("price_cop", ""))
                    year = int(row.get("model_year", ""))
                    mileage = _form_int_or_none(row.get("mileage_km"))
                    source_url = (row.get("source_url") or "").strip()
                    observed_at = (row.get("observed_at") or "").strip()
                    source_name = (row.get("source_name") or "").strip()
                    price_type = (row.get("price_type") or "precio_solicitado").strip()
                    status = (row.get("verification_status") or "observado_no_verificado").strip()
                    if price <= 0 or year < 1950 or (mileage is not None and mileage < 0):
                        raise ValueError("Precio, año o kilometraje fuera de rango.")
                    if not _is_https_url(source_url) or not source_name or not _valid_observed_date(observed_at):
                        raise ValueError("Fuente, URL HTTPS y fecha ISO no futura son obligatorios.")
                    if price_type not in {"precio_solicitado","transaccion_documentada","guia_comercial","valor_fiscal","precio_nuevo","otro"}:
                        raise ValueError("Tipo de precio inválido.")
                    if status not in {"observado_no_verificado","parcial","verificado","expirado","rechazado"}:
                        raise ValueError("Estado de verificación de precio inválido.")
                    match = _find_price_vehicle(conn, row)
                    if not match:
                        unmatched += 1
                        prepared.append({"line": line_no, "row": row, "status": "sin_vehiculo", "detail": "No hay una única versión que coincida exactamente; importar/ajustar vehículo primero."})
                        continue
                    key = (int(match["id"]), source_url, price, observed_at)
                    if key in seen:
                        exact_duplicates += 1
                        prepared.append({"line": line_no, "row": row, "version_id": int(match["id"]), "key": key, "status": "duplicado", "detail": "Duplicado exacto dentro del CSV."})
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM price_references WHERE version_id=? AND source_url=? AND price_cop=? AND observed_at=?",
                        key,
                    ).fetchone()
                    if exists:
                        exact_duplicates += 1
                        prepared.append({"line": line_no, "row": row, "version_id": int(match["id"]), "key": key, "status": "duplicado", "detail": "La referencia ya existe en SQLite."})
                        seen.add(key)
                        continue
                    seen.add(key)
                    new_candidates += 1
                    prepared.append({"line": line_no, "row": row, "version_id": int(match["id"]), "key": key, "status": "nuevo", "detail": "Coincidencia exacta con versión del catálogo."})
                except (ValueError, TypeError, OverflowError) as exc:
                    errors_count += 1
                    prepared.append({"line": line_no, "row": row, "status": "error", "detail": str(exc)[:180]})

            report_path = Path(app.state.reports_dir)
            report_path.mkdir(parents=True, exist_ok=True)
            report_file = report_path / f"{'validacion' if dry_run else 'importacion'}_precios_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.csv"
            if dry_run:
                with report_file.open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=["fila_csv", "estado", "marca", "modelo", "año", "versión", "precio_cop", "detalle"])
                    writer.writeheader()
                    for item in prepared:
                        row = item["row"]
                        writer.writerow({"fila_csv": item["line"], "estado": item["status"], "marca": row.get("brand",""), "modelo": row.get("model",""), "año": row.get("model_year",""), "versión": row.get("version_name",""), "precio_cop": row.get("price_cop",""), "detalle": item["detail"]})
                flash(request, f"Validación de precios sin cambios en SQLite: {new_candidates} referencia(s) nueva(s), {exact_duplicates} duplicada(s), {unmatched} sin versión exacta, {errors_count} error(es). Informe: {report_file.name}.", "success" if errors_count == 0 and unmatched == 0 else "error")
                return RedirectResponse("/admin#importar", status_code=303)

            backup_path = _backup_database(app.state.database_path)
            added = skipped = unmatched_count = 0
            errors = []
            for item in prepared:
                row = item["row"]
                if item["status"] == "duplicado":
                    skipped += 1
                    item["actual_status"] = "duplicado"
                    continue
                if item["status"] == "sin_vehiculo":
                    unmatched_count += 1
                    item["actual_status"] = "sin_vehiculo"
                    errors.append(f"Fila {item['line']}: {item['detail']}")
                    continue
                if item["status"] == "error":
                    item["actual_status"] = "error"
                    errors.append(f"Fila {item['line']}: {item['detail']}")
                    continue
                conn.execute("SAVEPOINT import_price_row")
                try:
                    price_type = (row.get("price_type") or "precio_solicitado").strip()
                    source_type = (row.get("source_type") or "").strip() or (
                        "precio_nuevo" if price_type == "precio_nuevo" else (
                            "guia_comercial" if price_type == "guia_comercial" else "anuncios_publicos"
                        )
                    )
                    source_id = _ensure_source(
                        conn, (row.get("source_name") or "").strip(), (row.get("source_url") or "").strip(),
                        source_type, (row.get("source_notes") or row.get("notes") or "").strip(),
                        (row.get("observed_at") or "").strip(),
                    )
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO price_references(version_id,source_id,price_cop,price_type,model_year,mileage_km,
                           location,source_url,observed_at,verification_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (item["version_id"], source_id, int(row["price_cop"]), price_type, int(row["model_year"]),
                         _form_int_or_none(row.get("mileage_km")), (row.get("location") or "").strip() or None,
                         (row.get("source_url") or "").strip(), (row.get("observed_at") or "").strip(),
                         (row.get("verification_status") or "observado_no_verificado").strip(),
                         (row.get("notes") or "").strip() or None),
                    )
                    conn.execute("RELEASE SAVEPOINT import_price_row")
                    if cursor.rowcount:
                        added += 1
                        item["actual_status"] = "insertado"
                    else:
                        skipped += 1
                        item["actual_status"] = "duplicado"
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT import_price_row")
                    conn.execute("RELEASE SAVEPOINT import_price_row")
                    item["actual_status"] = "error"
                    item["detail"] = str(exc)[:180]
                    errors.append(f"Fila {item['line']}: {str(exc)[:180]}")
            conn.commit()
            audit(conn, "import_csv", "price_references", None, f"Archivo {Path(uploaded.filename).name}: agregados={added}, duplicados={skipped}, sin_vehiculo={unmatched_count}, errores={len(errors)}")
            with report_file.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["fila_csv", "estado", "marca", "modelo", "año", "versión", "precio_cop", "detalle"])
                writer.writeheader()
                for item in prepared:
                    row = item["row"]
                    state = item.get("actual_status", item["status"])
                    writer.writerow({"fila_csv": item["line"], "estado": state, "marca": row.get("brand",""), "modelo": row.get("model",""), "año": row.get("model_year",""), "versión": row.get("version_name",""), "precio_cop": row.get("price_cop",""), "detalle": item["detail"]})
            backup_text = f" Copia de seguridad: {backup_path.name}." if backup_path else ""
            flash(request, f"Importación de precios terminada: {added} agregado(s), {skipped} duplicado(s), {unmatched_count} sin versión exacta, {len(errors)} error(es). Informe: {report_file.name}.{backup_text}" + (" Primeros errores: " + " | ".join(errors[:4]) if errors else ""), "success" if not errors else "error")
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            flash(request, f"No se pudo procesar el CSV: {exc}", "error")
        return RedirectResponse("/admin#importar", status_code=303)

    @app.get("/admin/report/import-issues.csv")
    def admin_report_import_issues(request: Request, run_id: int | None = None, severity: str = "",
                                   conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        sql = ("SELECT run_id, severity, issue_type, page, brand, raw_description, model_year, raw_value, source_ref, detail "
               "FROM import_issues WHERE 1=1")
        args: list = []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if severity in {"pendiente", "descartado", "advertencia", "conflicto"}:
            sql += " AND severity=?"
            args.append(severity)
        rows = conn.execute(sql + " ORDER BY run_id, severity, id", args).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["importacion", "severidad", "tipo", "pagina", "marca", "descripcion_publicada", "anio", "valor_publicado", "localizador", "detalle"])
        writer.writerows([tuple(r) for r in rows])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="autovalor_incidencias_importacion.csv"'})

    @app.get("/admin/report/coverage.csv")
    def admin_report_coverage(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        rows = conn.execute(
            """SELECT b.name AS brand, m.name AS model, v.model_year,
                      COUNT(v.id) AS versions,
                      SUM(CASE WHEN v.verification_status='verificado' THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN v.verification_status IN ('pendiente','parcial') THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN EXISTS(SELECT 1 FROM price_references p WHERE p.version_id=v.id) THEN 1 ELSE 0 END) AS versions_with_any_price,
                      SUM((SELECT COUNT(*) FROM price_references p WHERE p.version_id=v.id)) AS price_references
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               GROUP BY b.name,m.name,v.model_year ORDER BY b.name,m.name,v.model_year"""
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["marca","línea","año_modelo","versiones","verificadas","pendientes_o_parciales","versiones_con_referencias","referencias_de_precio"])
        writer.writerows([tuple(r) for r in rows])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="autovalor_cobertura.csv"'})

    @app.get("/admin/report/pending.csv")
    def admin_report_pending(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        rows = conn.execute(
            """SELECT b.name AS brand,m.name AS model,v.model_year,v.version_name,v.engine_label,v.fuel,v.transmission,
                      v.verification_status,v.technical_source_name,v.technical_source_url,v.technical_source_checked_at,v.notes
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               WHERE v.verification_status IN ('pendiente','parcial') OR v.technical_source_url IS NULL
                  OR v.technical_source_checked_at IS NULL
               ORDER BY v.verification_status,b.name,m.name,v.model_year"""
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["marca","línea","año_modelo","versión","motor","combustible","transmisión","estado","fuente_técnica","url_fuente","fecha_consulta","notas"])
        writer.writerows([tuple(r) for r in rows])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="autovalor_pendientes_verificacion.csv"'})

    @app.get("/admin/report/transmissions.csv")
    def admin_report_transmissions(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        """Cajas que aún requieren revisión: sin confirmar, en conflicto, deducidas o solo documentadas."""
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        rows = conn.execute(
            """SELECT v.id, b.name AS brand, m.name AS model, v.model_year, v.version_name, v.engine_label,
                      v.raw_description, v.transmission, v.transmission_subtype, v.transmission_gears,
                      v.transmission_status, v.transmission_rule, v.transmission_original, v.transmission_evidence,
                      (SELECT COUNT(*) FROM price_references p WHERE p.version_id=v.id) AS precios
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               WHERE COALESCE(v.transmission_status,'pendiente') <> 'verificada'
               ORDER BY CASE COALESCE(v.transmission_status,'pendiente') WHEN 'conflicto' THEN 0 WHEN 'pendiente' THEN 1
                        WHEN 'inferida' THEN 2 ELSE 3 END, b.name, m.name, v.model_year"""
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "marca", "línea", "año_modelo", "versión", "motor", "descripción_fuente", "caja",
                         "subtipo", "marchas", "estado_caja", "regla", "caja_original", "evidencia", "precios_ligados"])
        writer.writerows([tuple(r) for r in rows])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="autovalor_cajas_revision.csv"'})

    @app.get("/admin/report/without-prices.csv")
    def admin_report_without_prices(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        rows = conn.execute(
            """SELECT b.name AS brand,m.name AS model,v.model_year,v.version_name,v.engine_label,v.fuel,v.transmission,
                      v.verification_status,v.technical_source_name,v.technical_source_url
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               WHERE NOT EXISTS (SELECT 1 FROM price_references p WHERE p.version_id=v.id)
               ORDER BY b.name,m.name,v.model_year,v.version_name"""
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["marca","línea","año_modelo","versión","motor","combustible","transmisión","estado","fuente_técnica","url_fuente"])
        writer.writerows([tuple(r) for r in rows])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="autovalor_versiones_sin_precios.csv"'})

    @app.get("/admin/report/duplicates.csv")
    def admin_report_duplicates(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not admin_ok(request):
            return RedirectResponse("/admin/login", status_code=303)
        pairs = find_potential_duplicate_pairs(conn)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id_a","vehiculo_a","generacion_a","id_b","vehiculo_b","generacion_b","motivo"])
        for pair in pairs:
            writer.writerow([pair["id_a"],pair["vehicle_a"],pair["generation_a"],pair["id_b"],pair["vehicle_b"],pair["generation_b"],pair["reason"]])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="autovalor_posibles_duplicados.csv"'})

    return app


def _strict_int(value):
    """Acepta enteros JSON o cadenas de dígitos, sin truncar decimales."""
    if isinstance(value, bool):
        raise ValueError("boolean no es un entero válido")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    raise ValueError("se requiere un número entero")


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme == "https" and bool(parsed.netloc) and "." in parsed.netloc


def _valid_observed_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value and date.fromisoformat(value) <= date.today()
    except (TypeError, ValueError):
        return False


def _form_int_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _form_float_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Se esperaba un número finito no negativo.")
    return number


class _LazyApp:
    """Aplicación ASGI por defecto («uvicorn app:app»), creada en la primera petición.

    Importar este módulo (por ejemplo desde las pruebas, que crean sus propias aplicaciones
    con una base temporal) ya no abre ni migra la base real del proyecto.
    """

    def __init__(self):
        self._app: FastAPI | None = None

    def _get(self) -> FastAPI:
        if self._app is None:
            self._app = create_app()
        return self._app

    async def __call__(self, scope, receive, send):
        await self._get()(scope, receive, send)

    def __getattr__(self, name):
        return getattr(self._get(), name)


app = _LazyApp()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), log_level="info")
