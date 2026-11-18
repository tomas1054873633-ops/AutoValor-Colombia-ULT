"""Opciones del formulario de consulta y validación de la combinación elegida.

La secuencia es marca → línea → año → motor → transmisión → versión. Este módulo es la única
fuente de verdad para las opciones de motor, de caja y de versión, y la API de valoración
valida la combinación con estas mismas funciones (lo que ofrece el formulario es exactamente
lo que acepta el servidor).

- Motor: grupo de motor equivalente (``catalog_utils.engine_groups``); el identificador que
  viaja en el formulario es el id más bajo del grupo.
- Transmisión: CATEGORÍA normalizada de la caja dentro de ese motor: «Mecánica»,
  «Automática» o, aparte, «Sin confirmar» (versiones cuya caja no se ha podido confirmar; se
  ofrecen como referencia general, nunca como una caja válida). El identificador es el id
  más bajo de las versiones de esa categoría.
"""
from __future__ import annotations

from collections import Counter
from datetime import date

import transmissions as T
from catalog_utils import engine_group_of, engine_groups

# ---------------------------------------------------------------------------
# Versiones con valoración disponible
# ---------------------------------------------------------------------------
# La consulta pública solo ofrece versiones para las que se puede calcular un valor (misma regla
# que ``valuation.valuation_available``). Las demás siguen en la base, en el panel administrativo
# y en los informes de datos pendientes. El conjunto se calcula una vez y se recalcula cuando
# cambian las versiones, los precios, los parámetros o la fecha.
_VALUABLE_CACHE: dict = {}


def _data_signature(conn) -> tuple:
    p = tuple(conn.execute("SELECT COUNT(*), MAX(id), SUM(price_cop), MAX(observed_at), "
                           "GROUP_CONCAT(verification_status, '') FROM price_references").fetchone())
    v = tuple(conn.execute("SELECT COUNT(*), MAX(id), SUM(model_year) FROM vehicle_versions").fetchone())
    c = tuple(conn.execute("SELECT GROUP_CONCAT(key || '=' || value, ';') FROM valuation_config").fetchone())
    return (hash(p), v, c, date.today().isoformat())


def valuable_version_ids(conn) -> set[int]:
    from valuation import NEAR_EQUAL_PCT, load_config, valuation_available

    db = conn.execute("PRAGMA database_list").fetchone()[2]
    sig = _data_signature(conn)
    cached = _VALUABLE_CACHE.get(db)
    if cached and cached[0] == sig:
        return cached[1]
    # Guía de usados: una sola cifra (o cifras casi iguales) en la edición más reciente.
    latest: dict[int, tuple] = {}
    for vid, price, obs in conn.execute(
            "SELECT version_id, price_cop, observed_at FROM price_references WHERE price_type='guia_comercial' "
            "AND verification_status IN ('verificado','parcial')"):
        cur = latest.get(vid)
        if cur is None or obs > cur[0]:
            latest[vid] = (obs, {price})
        elif obs == cur[0]:
            cur[1].add(price)
    ok = {vid for vid, (_, vals) in latest.items()
          if max(vals) <= min(vals) * (1 + NEAR_EQUAL_PCT / 100.0)}
    # El resto (anuncios o precio de nuevo del año en curso) con la misma regla de la valoración.
    cfg, today = load_config(conn), date.today()
    for (vid,) in conn.execute("SELECT id FROM vehicle_versions").fetchall():
        if vid not in ok and valuation_available(conn, vid, cfg, today):
            ok.add(vid)
    _VALUABLE_CACHE[db] = (sig, ok)
    return ok

_VERSION_COLUMNS = """v.id, v.model_id, v.model_year, v.version_name, v.engine_label, v.fuel, v.displacement_cc,
    v.transmission, v.transmission_subtype, v.transmission_gears, v.transmission_detail, v.transmission_status,
    v.verification_status, v.traction"""

CATEGORY_ORDER = {T.CATEGORY_MANUAL: 0, T.CATEGORY_AUTOMATIC: 1, T.CATEGORY_UNCONFIRMED: 2}


def category_of(row) -> str:
    value = row["transmission"]
    return value if value in T.CATEGORIES else T.category_for_key(value)


def model_year_rows(conn, model_id: int, year: int, only_valuable: bool = True) -> list:
    rows = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM vehicle_versions v WHERE v.model_id=? AND v.model_year=? ORDER BY v.id",
        (model_id, year),
    ).fetchall()
    if not only_valuable:
        return rows
    ok = valuable_version_ids(conn)
    return [r for r in rows if r["id"] in ok]


def available_years(conn, model_id: int) -> list[int]:
    ok = valuable_version_ids(conn)
    years = {y for vid, y in conn.execute("SELECT id, model_year FROM vehicle_versions WHERE model_id=?", (model_id,))
             if vid in ok}
    return sorted(years, reverse=True)


def available_model_ids(conn) -> set[int]:
    ok = valuable_version_ids(conn)
    return {mid for vid, mid in conn.execute("SELECT id, model_id FROM vehicle_versions") if vid in ok}


def engine_options(conn, model_id: int, year: int) -> list[dict]:
    rows = model_year_rows(conn, model_id, year)
    return [{k: g[k] for k in ("engine_id", "engine_label", "fuel", "displacement_cc")} | {"versions": len(g["member_ids"])}
            for g in engine_groups(rows)]


def _engine_members(conn, model_id: int, year: int, engine_id: int, only_valuable: bool = True):
    rows = model_year_rows(conn, model_id, year, only_valuable)
    group = engine_group_of(rows, engine_id)
    if not group:
        return None, []
    ids = set(group["member_ids"])
    return group, [r for r in rows if r["id"] in ids]


def _status_summary(rows) -> str:
    statuses = {r["transmission_status"] or T.STATUS_PENDING for r in rows}
    if statuses == {T.STATUS_VERIFIED}:
        return "verificada"
    if T.STATUS_VERIFIED in statuses:
        return "parcialmente_verificada"
    if statuses & {T.STATUS_PENDING, T.STATUS_CONFLICT}:
        return "sin_confirmar"
    return "sin_ficha_tecnica"


def transmission_options(conn, model_id: int, year: int, engine_id: int) -> list[dict]:
    group, members = _engine_members(conn, model_id, year, engine_id)
    if not group:
        return []
    by_cat: dict[str, list] = {}
    for r in members:
        by_cat.setdefault(category_of(r), []).append(r)
    options = []
    for cat in sorted(by_cat, key=lambda c: CATEGORY_ORDER.get(c, 9)):
        rows = by_cat[cat]
        subtypes = Counter(r["transmission_subtype"] for r in rows if r["transmission_subtype"]
                           and r["transmission_subtype"] != T.SUBTYPES["manual"])
        confirmed = cat in T.CONFIRMED_CATEGORIES
        options.append({
            "transmission_id": min(r["id"] for r in rows),
            "transmission": cat,
            "label": cat if confirmed else "Caja sin confirmar (referencia general)",
            "confirmed": confirmed,
            "status": _status_summary(rows) if confirmed else "sin_confirmar",
            "subtypes": [T.SUBTYPE_SHORT.get(T.subtype_code(s) or "", s) or s for s, _ in subtypes.most_common()],
            "versions": len(rows),
        })
    return options


def version_options(conn, model_id: int, year: int, engine_id: int, transmission_id: int) -> list | None:
    """Versiones del motor y la categoría de caja elegidos; None si la combinación no es coherente."""
    group, members = _engine_members(conn, model_id, year, engine_id)
    if not group:
        return None
    chosen = next((r for r in members if r["id"] == transmission_id), None)
    if chosen is None:
        return None
    cat = category_of(chosen)
    return sorted((r for r in members if category_of(r) == cat), key=lambda r: (r["version_name"].lower(), r["id"]))


def path_for_version(conn, version_id: int) -> dict | None:
    """Ruta completa (ids de cada casilla) para abrir una versión desde un enlace compartido."""
    v = conn.execute(
        """SELECT v.id AS version_id, v.model_id, m.brand_id, b.name AS brand, m.name AS model, v.model_year,
                  v.version_name, v.engine_label, v.transmission
           FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           WHERE v.id=?""", (version_id,)).fetchone()
    if not v:
        return None
    item = dict(v)
    # Una versión sin valoración no aparece en el formulario, pero un enlace compartido a ella
    # sigue resolviéndose (la consulta responde que no hay valoración, en vez de fallar).
    only_valuable = int(version_id) in valuable_version_ids(conn)
    item["valuation_listed"] = only_valuable
    group, members = _engine_members(conn, v["model_id"], v["model_year"], v["version_id"], only_valuable)
    if not group:
        return None
    cat = v["transmission"] if v["transmission"] in T.CATEGORIES else T.category_for_key(v["transmission"])
    item["engine_id"] = group["engine_id"]
    item["engine_option"] = group["engine_label"]
    item["transmission"] = cat
    item["transmission_id"] = min(r["id"] for r in members if category_of(r) == cat)
    return item


def validate_combination(conn, version_id: int, *, brand_id=None, model_id=None, model_year=None,
                         engine_id=None, transmission_id=None) -> str | None:
    """Devuelve None si la combinación del formulario es coherente; si no, el motivo.

    Los campos que no se envían no se comprueban (un enlace compartido solo trae la versión),
    pero si se envían deben coincidir exactamente con lo que el formulario ofrece.
    """
    path = path_for_version(conn, version_id)
    if path is None:
        return "La versión no existe en el catálogo."
    if brand_id is not None and int(brand_id) != int(path["brand_id"]):
        return "La versión elegida no pertenece a esa marca."
    if model_id is not None and int(model_id) != int(path["model_id"]):
        return "La versión elegida no pertenece a esa línea o modelo."
    if model_year is not None and int(model_year) != int(path["model_year"]):
        return "La versión elegida no corresponde a ese año modelo."
    listed = path.get("valuation_listed", True)
    if engine_id is not None:
        group, _ = _engine_members(conn, path["model_id"], path["model_year"], int(engine_id), listed)
        if not group or int(version_id) not in group["member_ids"]:
            return "La versión elegida no corresponde a ese motor."
    if transmission_id is not None:
        trow = conn.execute("SELECT model_id, model_year, transmission FROM vehicle_versions WHERE id=?",
                            (int(transmission_id),)).fetchone()
        if (trow is None or trow["model_id"] != path["model_id"] or trow["model_year"] != path["model_year"]):
            return "La transmisión elegida no corresponde a ese vehículo."
        if engine_id is not None:
            group, _ = _engine_members(conn, path["model_id"], path["model_year"], int(engine_id), listed)
            if not group or int(transmission_id) not in group["member_ids"]:
                return "La transmisión elegida no corresponde a ese motor."
        if category_of(trow) != path["transmission"]:
            return "La versión elegida no tiene esa transmisión."
    return None
