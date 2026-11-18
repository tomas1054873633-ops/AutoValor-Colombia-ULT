"""Motor de valoración explicable de AutoValor Pro.

Principios (ver reports/METODO_VALORACION.md):
  * Se separan cinco categorías que nunca se mezclan ni se presentan como una sola cifra:
      1. Guía de usados de Revista Motor (precio publicado, en millones de COP, convertido a COP).
      2. Precio de lista de vehículo nuevo publicado (en COP; jamás se multiplica).
      3. Anuncios observados (precios solicitados, no transacciones).
      4. Estimación por comparables (versiones relacionadas; NO es un precio publicado).
      5. Valoración AutoValor: referencia directa + ajuste porcentual por kilometraje.
  * El ajuste por kilometraje es relativo al kilometraje ESPERADO para la edad del vehículo
    (la guía ya descuenta la edad, así que no hay doble depreciación), gradual, acotado y
    configurable. No existe un valor fijo en COP por kilómetro.
  * Los parámetros son iniciales y configurables; no se presentan como calibrados con datos
    de mercado. No se calculan porcentajes de «confianza».
"""
from __future__ import annotations

import transmissions as T

import math
import re
from datetime import date, datetime
from statistics import median
from typing import Any

GUIDE_SOURCE_PREFIX = "Revista Motor — Guía de precios de usados"

DEFAULTS = {
    "km_expected_per_year": 14000.0,
    "km_deadband_pct": 10.0,
    "km_deadband_floor_km": 5000.0,
    "km_scale_floor": 10000.0,
    "km_penalty_cap_pct": 15.0,
    "km_bonus_cap_pct": 5.0,
    "km_curve_steepness": 1.0,
    "km_atypical_high_factor": 2.5,
    "km_atypical_low_factor": 0.2,
    "reference_year_offset": 0.0,
    "guide_vs_ads_warning_pct": 20.0,
    "guide_max_age_days": 120.0,
    "freshness_days": 90.0,
    "minimum_prices": 3.0,
    "valuation_rounding_cop": 100000.0,
}


MONTHS_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _int_es(value: float) -> str:
    """Entero con separador de miles colombiano: 98700000 -> '98.700.000'."""
    return f"{int(round(value)):,}".replace(",", ".")


def _cop_es(value: float) -> str:
    # Espacio no separable: la cifra no se parte en dos líneas al mostrarse.
    return "$\u00a0" + _int_es(value)


def _num_es(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _pct_es(value: float) -> str:
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{abs(value):.2f}".replace(".", ",") + "\u00a0%"


def _date_es(iso_text: str | None) -> str:
    try:
        d = datetime.strptime(str(iso_text)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return str(iso_text or "fecha no indicada")
    return f"{d.day} de {MONTHS_ES[d.month - 1]} de {d.year}"


def load_config(conn) -> dict[str, float]:
    cfg = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM valuation_config"):
        if row["key"] in cfg:
            try:
                cfg[row["key"]] = float(row["value"])
            except (TypeError, ValueError):
                pass
    return cfg


def _days_since(date_text: str, today: date) -> int | None:
    try:
        return (today - datetime.strptime(str(date_text)[:10], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Kilometraje
# ---------------------------------------------------------------------------
def expected_km(age_years: int, cfg: dict[str, float]) -> int:
    return int(round(max(0, age_years) * cfg["km_expected_per_year"]))


def km_adjustment_pct(mileage_km: float, expected: float, cfg: dict[str, float]) -> float:
    """Ajuste porcentual (negativo = descuento) por diferencia frente al kilometraje esperado.

        d        = km - esperado
        permitido= max(zona_muerta_% × esperado, piso_zona_muerta_km)      (sin ajuste dentro)
        escala   = max(esperado, piso_de_escala_km)
        r        = (|d| - permitido) / escala                               (0 dentro de la zona)
        ajuste%  = ± tope × tanh(pendiente × r)       (tope_penalización si d>0, tope_bonificación si d<0)

    Continua, monótona, suave y acotada por los topes configurables.
    """
    d = float(mileage_km) - float(expected)
    allowed = max(cfg["km_deadband_pct"] / 100.0 * expected, cfg["km_deadband_floor_km"])
    scale = max(float(expected), cfg["km_scale_floor"])
    excess = abs(d) - allowed
    if excess <= 0:
        return 0.0
    r = excess / scale
    if d > 0:
        return -cfg["km_penalty_cap_pct"] * math.tanh(cfg["km_curve_steepness"] * r)
    return cfg["km_bonus_cap_pct"] * math.tanh(cfg["km_curve_steepness"] * r)


def _round_value(value: float, cfg: dict[str, float]) -> int:
    step = cfg.get("valuation_rounding_cop") or 0
    if step and step > 1:
        return int(round(value / step) * step)
    return int(round(value))


def _displacement_key(engine_label: str | None) -> str | None:
    m = re.match(r"\s*(\d[.,]\d{1,2})", engine_label or "")
    return m.group(1).replace(",", ".") if m else None


# ---------------------------------------------------------------------------
# Consultas de referencias
# ---------------------------------------------------------------------------
def _guide_reference(conn, version_id: int, cfg: dict[str, float], today: date) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT p.id, p.price_cop, p.observed_at, p.verification_status, p.source_ref, p.source_page,
                  p.column_year, p.raw_value, s.name AS source_name
           FROM price_references p JOIN sources s ON s.id=p.source_id
           WHERE p.version_id=? AND p.price_type='guia_comercial' AND p.verification_status IN ('verificado','parcial')
           ORDER BY p.observed_at DESC, p.id""", (version_id,)).fetchall()
    obs = [dict(r) for r in rows]
    out: dict[str, Any] = {"category": "guia_usados", "label": "Guía de precios de usados (Revista Motor)",
                           "available": False, "usable": False, "observations": obs}
    if not obs:
        return out
    latest = max(o["observed_at"] for o in obs)
    latest_obs = [o for o in obs if o["observed_at"] == latest]
    distinct = sorted({o["price_cop"] for o in latest_obs})
    out.update({
        "available": True, "edition_date": latest, "source_name": latest_obs[0]["source_name"],
        "age_days": _days_since(latest, today), "distinct_values_cop": distinct,
        "unit_note": "Publicado en millones de COP; convertido a pesos (×1.000.000).",
        "market_note": "Referencia de usados en Bogotá, en buen estado y con kilometraje esperado para su edad.",
    })
    if len(distinct) == 1:
        out.update({"usable": True, "price_cop": distinct[0], "raw_value": latest_obs[0]["raw_value"],
                    "source_page": latest_obs[0]["source_page"]})
    elif distinct[-1] <= distinct[0] * (1 + NEAR_EQUAL_PCT / 100.0):
        # La misma versión aparece dos veces en la guía con valores casi iguales (diferencia de
        # hasta NEAR_EQUAL_PCT %): se usa el promedio y se avisa. Si difieren más, no se elige.
        out.update({"usable": True, "price_cop": int(round(sum(distinct) / len(distinct))),
                    "near_equal_values": True, "raw_value": latest_obs[0]["raw_value"],
                    "source_page": latest_obs[0]["source_page"]})
    else:
        out["conflict"] = True
    return out


# Diferencia máxima (%) entre dos valores publicados para la misma versión y año que se acepta
# promediando; por encima de ella no se elige ningún valor.
NEAR_EQUAL_PCT = 2.0


def _new_price_basis(new_ref: dict[str, Any], model_year: int, today: date) -> int | None:
    """Precio de lista de nuevo como base solo para modelos del año en curso (o posteriores) que
    todavía no tienen valor de usado. Devuelve el precio o None."""
    if int(model_year) < today.year:
        return None
    exact = new_ref.get("exact") or []
    if not exact:
        return None
    latest = exact[0]["observed_at"]
    values = sorted({int(r["price_cop"]) for r in exact if r["observed_at"] == latest and r["price_cop"]})
    if not values or values[-1] > values[0] * (1 + NEAR_EQUAL_PCT / 100.0):
        return None
    return int(round(sum(values) / len(values)))


def _new_reference(conn, version_id: int, model_id: int) -> dict[str, Any]:
    exact = conn.execute(
        """SELECT p.price_cop, p.observed_at, p.source_page, s.name AS source_name
           FROM price_references p JOIN sources s ON s.id=p.source_id
           WHERE p.version_id=? AND p.price_type='precio_nuevo' AND p.verification_status IN ('verificado','parcial')
           ORDER BY p.observed_at DESC, p.id DESC""", (version_id,)).fetchall()
    related = conn.execute(
        """SELECT v.id AS version_id, v.version_name, v.model_year, p.price_cop, p.observed_at, s.name AS source_name
           FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id JOIN sources s ON s.id=p.source_id
           WHERE v.model_id=? AND p.price_type='precio_nuevo' AND p.version_id<>?
             AND p.verification_status IN ('verificado','parcial')
           ORDER BY p.price_cop LIMIT 8""", (model_id, version_id)).fetchall()
    return {
        "category": "precio_nuevo_publicado",
        "label": "Precio de lista de vehículo nuevo (publicado)",
        "exact": [dict(r) for r in exact],
        "related_same_model": [dict(r) for r in related],
        "note": "Publicado en pesos COP; no se convierte ni se multiplica. No es un precio de usado y el año modelo de los nuevos no se publica.",
    }


def _ads_reference(conn, version_id: int, expected: float, cfg: dict[str, float], today: date) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT p.*, s.name AS source_name FROM price_references p JOIN sources s ON s.id=p.source_id
           WHERE p.version_id=? AND p.price_type='precio_solicitado'
           ORDER BY p.observed_at DESC, p.id DESC""", (version_id,)).fetchall()
    all_refs = [dict(r) for r in rows]
    usable, excluded = [], {"antiguas_o_fecha_invalida": 0, "no_verificadas": 0, "sin_kilometraje": 0, "futuras": 0}
    for ref in all_refs:
        age = _days_since(ref.get("observed_at"), today)
        if age is None or age > cfg["freshness_days"]:
            excluded["antiguas_o_fecha_invalida"] += 1
        elif age < 0:
            excluded["futuras"] += 1
        elif ref.get("verification_status") != "verificado":
            excluded["no_verificadas"] += 1
        elif ref.get("mileage_km") is None:
            excluded["sin_kilometraje"] += 1
        else:
            usable.append(ref)
    out: dict[str, Any] = {
        "category": "anuncios_observados", "label": "Anuncios observados (precios solicitados)",
        "total": len(all_refs), "used_count": len(usable), "excluded": excluded,
        "references": all_refs[:20], "usable": False,
        "note": "Son precios solicitados por vendedores; no son transacciones ni garantizan un precio de venta.",
    }
    if usable:
        raw = [float(r["price_cop"]) for r in usable]
        out["raw_median_cop"] = int(round(median(raw)))
        out["raw_min_cop"], out["raw_max_cop"] = int(min(raw)), int(max(raw))
    if len(usable) >= int(cfg["minimum_prices"]):
        # Cada anuncio se lleva al kilometraje esperado deshaciendo el ajuste porcentual con la
        # misma curva configurable; así no hay pendiente empírica ni COP/km fijos.
        normalized = []
        for r in usable:
            pct = km_adjustment_pct(float(r["mileage_km"]), expected, cfg)
            normalized.append(float(r["price_cop"]) / (1.0 + pct / 100.0))
        out["usable"] = True
        out["baseline_cop"] = float(median(normalized))
    return out


def _comparable_estimate(conn, version, guide_self: dict[str, Any]) -> dict[str, Any] | None:
    """Versiones relacionadas con precio de guía (mismo modelo y año). NO es un precio publicado."""
    rows = conn.execute(
        """SELECT v.id, v.version_name, v.engine_label, v.transmission, p.price_cop
           FROM vehicle_versions v JOIN price_references p ON p.version_id=v.id
           WHERE v.model_id=? AND v.model_year=? AND v.id<>? AND p.price_type='guia_comercial'
             AND p.verification_status IN ('verificado','parcial')""",
        (version["model_id"], version["model_year"], version["id"])).fetchall()
    if not rows:
        return None
    disp = _displacement_key(version["engine_label"])
    by_version: dict[int, dict] = {}
    for r in rows:
        by_version.setdefault(r["id"], dict(r))
    pool = list(by_version.values())
    same_engine = [r for r in pool if disp and _displacement_key(r["engine_label"]) == disp]
    chosen = same_engine if len(same_engine) >= 2 else pool
    basis = ("misma marca, modelo, año y cilindrada" if chosen is same_engine
             else "misma marca, modelo y año (cilindrada distinta o no publicada)")
    prices = sorted(r["price_cop"] for r in chosen)
    if len(prices) < 2:
        return {"category": "estimacion_por_comparables", "available": False, "count": len(prices),
                "label": "Estimación por comparables (no es un precio publicado)"}
    return {
        "category": "estimacion_por_comparables", "available": True,
        "label": "Estimación por comparables (no es un precio publicado)",
        "basis": basis, "count": len(prices),
        "min_cop": int(prices[0]), "median_cop": int(round(median(prices))), "max_cop": int(prices[-1]),
        "versions": [{"version_id": r["id"], "version_name": r["version_name"], "engine_label": r["engine_label"],
                      "transmission": r["transmission"], "price_cop": r["price_cop"]}
                     for r in sorted(chosen, key=lambda x: x["price_cop"])[:10]],
        "note": "Precios de guía de OTRAS versiones del mismo modelo y año. No equivalen al precio de esta versión; sirven solo de contexto.",
    }


# ---------------------------------------------------------------------------
# Valoración
# ---------------------------------------------------------------------------
def estimate_vehicle(conn, version_id: int, mileage_km: int, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    version = conn.execute(
        """SELECT v.*, m.name AS model_name, m.category, b.name AS brand_name
           FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           WHERE v.id=?""", (version_id,)).fetchone()
    if not version:
        return {"estimable": False, "error": "La versión seleccionada no existe en la base de datos."}
    cfg = load_config(conn)
    model_year = int(version["model_year"])
    ref_year = today.year + int(cfg["reference_year_offset"])
    age_years = max(0, ref_year - model_year)
    expected = expected_km(age_years, cfg)
    diff = int(mileage_km) - expected
    pct = km_adjustment_pct(mileage_km, expected, cfg)
    allowed = max(cfg["km_deadband_pct"] / 100.0 * expected, cfg["km_deadband_floor_km"])
    if abs(diff) <= allowed:
        km_status, km_label = "esperado", "Dentro de la zona sin ajuste"
    elif diff > 0:
        km_status, km_label = "superior", "Superior al kilometraje esperado"
    else:
        km_status, km_label = "inferior", "Inferior al kilometraje esperado"
    atypical = None
    if expected > 0 and mileage_km > cfg["km_atypical_high_factor"] * expected:
        atypical = "El kilometraje supera con amplitud el esperado para su edad; el ajuste está acotado y la valoración es menos representativa."
    elif age_years >= 2 and mileage_km < cfg["km_atypical_low_factor"] * expected:
        atypical = "El kilometraje es muy inferior al esperado para su edad; conviene verificar el odómetro y el historial."
    km_block = {
        "mileage_km": int(mileage_km), "age_years": age_years, "reference_year": ref_year,
        "expected_km": expected, "expected_km_per_year": int(cfg["km_expected_per_year"]),
        "difference_km": diff, "difference_pct_of_expected": (round(100.0 * diff / expected, 1) if expected else None),
        "no_adjustment_zone_km": int(round(allowed)),
        "status": km_status, "status_label": km_label, "adjustment_pct": round(pct, 2), "atypical_warning": atypical,
        "basis": f"{int(cfg['km_expected_per_year']):,} km/año × {age_years} año(s) de edad".replace(",", "."),
        # Parámetros de la curva para que la página pueda simular otros kilometrajes con la misma fórmula.
        "curve": {
            "deadband_km": float(allowed),
            "scale_km": float(max(float(expected), cfg["km_scale_floor"])),
            "penalty_cap_pct": float(cfg["km_penalty_cap_pct"]),
            "bonus_cap_pct": float(cfg["km_bonus_cap_pct"]),
            "steepness": float(cfg["km_curve_steepness"]),
            "rounding_cop": float(cfg["valuation_rounding_cop"]),
        },
    }

    guide = _guide_reference(conn, version_id, cfg, today)
    ads = _ads_reference(conn, version_id, expected, cfg, today)
    new_ref = _new_reference(conn, version_id, version["model_id"])
    comparable = _comparable_estimate(conn, version, guide)

    warnings: list[str] = list(technical_warnings(version))
    if version["year_basis"] and str(version["year_basis"]).startswith("ASUMIDO"):
        warnings.append("Para esta versión, el año modelo 2026 es una suposición: no está confirmado.")
    if atypical:
        warnings.append(atypical)

    result: dict[str, Any] = {
        "estimable": False,
        "vehicle": {k: version[k] for k in version.keys()},
        "mileage_km": int(mileage_km), "age_years": age_years,
        "km": km_block,
        "references": {"guide": guide, "new_published": new_ref, "ads": ads, "comparable_estimate": comparable},
        "valuation": None,
        "warnings": warnings,
    }

    basis = None
    reference_price = None
    method_steps: list[str] = []
    if guide.get("usable"):
        basis, reference_price = "guia", float(guide["price_cop"])
        method_steps.append(f"Se parte del valor de referencia de mercado para esta versión y año: {_cop_es(reference_price)}.")
        if guide.get("near_equal_values"):
            vals = " y ".join(_cop_es(v) for v in guide["distinct_values_cop"])
            warnings.append(f"Para esta versión y año hay dos valores de referencia casi iguales ({vals}); se usa su promedio.")
        if guide.get("age_days") is not None and guide["age_days"] > cfg["guide_max_age_days"]:
            warnings.append(f"Los valores de referencia tienen {guide['age_days']} días; pueden estar desactualizados.")
        if ads.get("usable"):
            ads_value = ads["baseline_cop"]
            gap = 100.0 * (ads_value - reference_price) / reference_price
            ads["gap_vs_guide_pct"] = round(gap, 1)
            if abs(gap) > cfg["guide_vs_ads_warning_pct"]:
                warnings.append(f"Los anuncios de venta observados para esta versión difieren un {abs(gap):.0f}\u00a0% del valor de referencia; revísalo antes de usar la cifra.")
    elif guide.get("conflict"):
        vals = " y ".join(_cop_es(v) for v in guide["distinct_values_cop"])
        warnings.append(f"Hay valores de referencia distintos para esta versión y año ({vals}); no elegimos uno, así que no se usa ninguno.")
    if basis is None and ads.get("usable"):
        basis, reference_price = "anuncios", float(ads["baseline_cop"])
        method_steps.append(f"Se parte del valor de referencia de mercado para esta versión y año, calculado con {ads['used_count']} anuncios "
                            f"de venta verificados y recientes, cada uno llevado al kilometraje esperado: {_cop_es(reference_price)}.")
    if basis is None:
        new_price = _new_price_basis(new_ref, model_year, today)
        if new_price:
            basis, reference_price = "precio_nuevo", float(new_price)
            method_steps.append("Para esta versión del año en curso todavía no hay un valor de usado; se parte del "
                                f"precio de lista del vehículo nuevo: {_cop_es(reference_price)}.")
            warnings.append("Es un modelo del año en curso sin valor de usado publicado: el cálculo parte del precio "
                            "de lista del vehículo nuevo, y lo que se pague por un usado puede ser menor.")
    if basis is None:
        reasons = []
        if not guide["available"]:
            reasons.append("no hay un valor de referencia para esta versión y año")
        elif guide.get("conflict"):
            reasons.append("los valores de referencia de esta versión y año no coinciden entre sí")
        n_ads = ads["used_count"]
        reasons.append(f"hay {n_ads} {'anuncio verificado y reciente' if n_ads == 1 else 'anuncios verificados y recientes'} "
                       f"con kilometraje (mínimo requerido: {int(cfg['minimum_prices'])})")
        result["reason"] = ("No se generó una valoración: " + "; ".join(reasons)
                            + ". Para no inventar una cifra, no mostramos una valoración sin una referencia directa.")
        return result

    adj_cop = reference_price * pct / 100.0
    final = reference_price + adj_cop
    steps = list(method_steps)
    years_txt = f"{age_years} año" if age_years == 1 else f"{age_years} años"
    km_context = (f"Con {_int_es(mileage_km)} km frente a {_int_es(expected)} km esperados "
                  f"({_int_es(cfg['km_expected_per_year'])} km por año durante {years_txt})")
    if abs(pct) < 0.005:
        steps.append(f"{km_context}, la diferencia queda dentro de la zona sin ajuste: el valor no cambia.")
    else:
        steps.append(f"{km_context} se aplica un ajuste de {_pct_es(pct)} ({'−' if adj_cop < 0 else '+'}\u2060{_cop_es(abs(adj_cop))}). "
                     f"El ajuste nunca supera un {_num_es(cfg['km_penalty_cap_pct'])} % de descuento ni un {_num_es(cfg['km_bonus_cap_pct'])} % de aumento.")
    steps.append(f"El resultado se redondea a múltiplos de {_cop_es(cfg['valuation_rounding_cop'])} para no sugerir una exactitud que el método no tiene.")
    value = _round_value(final, cfg)

    warnings.extend([
        "Es una valoración orientativa de AutoValor; no es una tasación oficial ni un avalúo pericial, y no sustituye la inspección del vehículo.",
        "El ajuste por kilometraje usa parámetros iniciales configurables; no están calibrados con datos de transacciones.",
        "No se evalúan estado mecánico, accidentes, documentación legal ni ciudad de venta distinta de Bogotá.",
    ])
    result.update({
        "estimable": True,
        "valuation": {
            "label": "Valoración AutoValor (orientativa)",
            "basis": basis,
            "basis_label": {"guia": "Guía de usados de Revista Motor",
                            "anuncios": "Anuncios observados normalizados por kilometraje",
                            "precio_nuevo": "Precio de lista del vehículo nuevo (modelo del año en curso)"}[basis],
            "reference_price_cop": int(round(reference_price)),
            "km_adjustment_pct": round(pct, 2),
            "km_adjustment_cop": int(round(adj_cop)),
            "value_before_rounding_cop": int(round(final)),
            "value_cop": value,
            "rounding_cop": int(cfg["valuation_rounding_cop"]),
            "steps": steps,
        },
    })
    return result


# ---------------------------------------------------------------------------
# Vista pública
# ---------------------------------------------------------------------------
# La página pública muestra la valoración y la explicación del ajuste por kilometraje, sin
# identificar de qué publicación o fuente proviene cada valor de referencia. La trazabilidad
# completa (fuente, edición, página, valor original) se conserva en la base, en el panel
# administrativo y en los informes internos.
PUBLIC_VEHICLE_FIELDS = ("id", "brand_name", "model_name", "model_year", "version_name", "engine_label",
                         "fuel", "transmission", "body_type", "traction", "verification_status")


def technical_warnings(version) -> list[str]:
    """Avisos sobre la ficha técnica, separando la caja de cambios del resto de la ficha.

    Antes había un único aviso genérico («motor, combustible, transmisión no verificados»)
    que aparecía incluso cuando la caja sí estaba confirmada. Ahora el aviso de la caja
    depende de su estado real y una caja verificada no genera ningún aviso de caja.
    """
    keys = version.keys() if hasattr(version, "keys") else []
    get = (lambda k: version[k] if k in keys else None)
    status = get("transmission_status") or T.STATUS_PENDING
    category = get("transmission")
    out: list[str] = []
    if category not in T.CONFIRMED_CATEGORIES or status in (T.STATUS_PENDING, T.STATUS_CONFLICT):
        if status == T.STATUS_CONFLICT:
            out.append("La transmisión de esta versión está en revisión porque la información disponible es "
                       "contradictoria. Usa la valoración con precaución.")
        else:
            out.append("La transmisión de esta versión no está confirmada: la valoración es una referencia general "
                       "de la versión y no corresponde a una caja específica.")
    elif status == T.STATUS_DOCUMENTED:
        out.append(f"La caja ({category.lower()}) figura en la descripción comercial de la versión, pero aún no se ha "
                   "contrastado con una ficha técnica oficial.")
    elif status == T.STATUS_INFERRED:
        out.append("La caja mecánica se dedujo porque la misma versión también se ofrece como automática; aún no se "
                   "ha contrastado con una ficha técnica oficial.")
    if get("verification_status") != "verificado":
        if status == T.STATUS_VERIFIED or status == T.STATUS_RULE:
            out.append("El motor y el combustible de esta versión provienen de su descripción comercial y no se han "
                       "contrastado con una ficha técnica oficial.")
        else:
            out.append("El motor y el combustible de esta versión tampoco se han contrastado con una ficha técnica oficial.")
    return out
PUBLIC_VALUATION_FIELDS = ("label", "reference_price_cop", "km_adjustment_pct", "km_adjustment_cop",
                           "value_before_rounding_cop", "value_cop", "rounding_cop", "steps")


def public_fuel(fuel: str | None) -> str | None:
    """Etiqueta de combustible para el público (sin notas internas de procedencia)."""
    if fuel and "según categoría" in fuel:
        return "Híbrido"
    return fuel


def valuation_available(conn, version_id: int, cfg: dict[str, float] | None = None, today: date | None = None) -> bool:
    """True si la versión tiene base para una valoración (misma regla que estimate_vehicle)."""
    cfg = cfg or load_config(conn)
    today = today or date.today()
    if _guide_reference(conn, version_id, cfg, today).get("usable"):
        return True
    if _ads_reference(conn, version_id, 0.0, cfg, today).get("usable"):
        return True
    row = conn.execute("SELECT model_id, model_year FROM vehicle_versions WHERE id=?", (version_id,)).fetchone()
    return bool(row and _new_price_basis(_new_reference(conn, version_id, row["model_id"]), row["model_year"], today))


def public_view(result: dict[str, Any]) -> dict[str, Any]:
    """Recorta el resultado interno a lo que se publica en la página."""
    vehicle = result.get("vehicle") or {}
    public_vehicle = {k: vehicle.get(k) for k in PUBLIC_VEHICLE_FIELDS}
    public_vehicle["fuel"] = public_fuel(public_vehicle.get("fuel"))
    public_vehicle.update(T.public_transmission(vehicle))
    if result.get("body_style"):
        public_vehicle["body_style"] = result["body_style"]
        public_vehicle["body_style_label"] = BODY_STYLE_LABELS.get(result["body_style"], "Automóvil")
    valuation = result.get("valuation")
    out: dict[str, Any] = {
        "estimable": bool(result.get("estimable")),
        "vehicle": public_vehicle,
        "mileage_km": result.get("mileage_km"),
        "age_years": result.get("age_years"),
        "km": dict(result.get("km") or {}),
        "valuation": {k: valuation.get(k) for k in PUBLIC_VALUATION_FIELDS} if valuation else None,
        "warnings": list(result.get("warnings") or []),
    }
    if result.get("reason"):
        out["reason"] = result["reason"]
    return out



# ---------------------------------------------------------------------------
# Datos complementarios para la página pública (sin procedencia)
# ---------------------------------------------------------------------------
BODY_STYLE_LABELS = {
    "sedan": "Sedán", "hatchback": "Hatchback", "suv": "Camioneta", "pickup": "Pick-up",
    "van": "Van", "coupe": "Coupé", "auto": "Automóvil",
}
_CATEGORY_STYLE = {
    "PICK UPS": "pickup", "PICK UPS 4X2/4X4": "pickup", "PICK-UP": "pickup",
    "VANS PASAJEROS": "van",
    "CAMIONETAS": "suv", "CAMIONETA": "suv", "CAMPEROS": "suv", "SUV": "suv", "4X4": "suv",
    "UTILITARIOS DEPORTIVOS 4X2/4X4": "suv",
    "AUTOMÓVILES": "car", "AUTOMÓVIL": "car",
}
_PICKUP_WORDS = (" D/C", "DOBLE CAB", " DOB.", " DOB ", " C/S", "CABINA SENCILLA", "PICK UP", "PICK-UP", "PICKUP")


def _norm(text: str | None) -> str:
    return f" {str(text or '').upper()} "


def _car_style(text: str) -> str:
    if any(w in text for w in ("SEDÁN", "SEDAN", " SD ")):
        return "sedan"
    if any(w in text for w in ("COUPÉ", "COUPE", "CONVERTIBLE", "CABRIO", "ROADSTER", "SPIDER")):
        return "coupe"
    if any(w in text for w in (" HB ", " HB.", "HATCH", " 5P", " 3P", "SPORTBACK")):
        return "hatchback"
    return "auto"


def body_style(conn, version: dict[str, Any]) -> str:
    """Tipo de carrocería ilustrativo (para la silueta). Se deduce de la categoría y la descripción."""
    text = _norm(" ".join(str(version.get(k) or "") for k in ("raw_description", "version_name", "model_name")))
    if any(w in text for w in _PICKUP_WORDS):
        return "pickup"
    explicit = str(version.get("body_type") or "").lower()
    for key, style in (("pick", "pickup"), ("suv", "suv"), ("sed", "sedan"), ("hatch", "hatchback"), ("van", "van"), ("coup", "coupe")):
        if key in explicit:
            return style
    category = str(version.get("source_category") or "").upper()
    style = _CATEGORY_STYLE.get(category)
    if style is None and version.get("model_id") is not None:
        # Híbridos y eléctricos: se usa la categoría más frecuente del mismo modelo en otras versiones.
        rows = conn.execute(
            """SELECT source_category, COUNT(*) n FROM vehicle_versions
               WHERE model_id=? AND source_category IS NOT NULL AND source_category NOT IN ('HÍBRIDOS','ELÉCTRICOS')
               GROUP BY source_category ORDER BY n DESC""", (version["model_id"],)).fetchall()
        for row in rows:
            style = _CATEGORY_STYLE.get(str(row[0]).upper())
            if style:
                break
        if style is None:
            model_cat = conn.execute("SELECT category FROM models WHERE id=?", (version["model_id"],)).fetchone()
            style = _CATEGORY_STYLE.get(str(model_cat[0] if model_cat else "").upper())
    if style == "car" or style is None:
        return _car_style(text)
    return style


def _latest_unique_guide_value(rows: list) -> int | None:
    if not rows:
        return None
    latest = max(r["observed_at"] for r in rows)
    values = {r["price_cop"] for r in rows if r["observed_at"] == latest}
    return values.pop() if len(values) == 1 else None


def value_history(conn, version_id: int) -> list[dict[str, Any]]:
    """Valor de referencia de la misma versión en cada año modelo (solo valores sin conflicto)."""
    v = conn.execute(
        "SELECT id, model_id, raw_description, version_name, engine_label, transmission FROM vehicle_versions WHERE id=?",
        (version_id,)).fetchone()
    if not v:
        return []
    if v["raw_description"]:
        siblings = conn.execute(
            "SELECT id, model_year FROM vehicle_versions WHERE model_id=? AND raw_description=?",
            (v["model_id"], v["raw_description"])).fetchall()
    else:
        siblings = conn.execute(
            """SELECT id, model_year FROM vehicle_versions WHERE model_id=? AND version_name=? AND engine_label=?
               AND transmission=?""", (v["model_id"], v["version_name"], v["engine_label"], v["transmission"])).fetchall()
    by_year: dict[int, list[int]] = {}
    for sib in siblings:
        rows = conn.execute(
            """SELECT price_cop, observed_at FROM price_references
               WHERE version_id=? AND price_type='guia_comercial' AND verification_status IN ('verificado','parcial')""",
            (sib["id"],)).fetchall()
        value = _latest_unique_guide_value(rows)
        if value is not None:
            by_year.setdefault(int(sib["model_year"]), []).append(int(value))
    points = []
    for year in sorted(by_year):
        values = set(by_year[year])
        if len(values) == 1:  # dos filas distintas para el mismo año serían un conflicto: no se muestran
            points.append({"model_year": year, "value_cop": values.pop()})
    return points


def new_price_context(conn, version_id: int) -> dict[str, Any] | None:
    """Precio de lista de un vehículo nuevo de la misma línea (contexto, no equivalencia)."""
    v = conn.execute("SELECT model_id, version_name, transmission FROM vehicle_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return None
    same = None
    if v["transmission"] in T.CONFIRMED_CATEGORIES:
        # Solo se toma como «misma versión» un vehículo nuevo con el mismo nombre Y la misma caja
        # confirmada; antes bastaba el nombre y una versión mecánica podía mostrar el precio de la automática.
        same = conn.execute(
            """SELECT p.price_cop FROM vehicle_versions n JOIN price_references p ON p.version_id=n.id
               WHERE n.model_id=? AND lower(n.version_name)=lower(?) AND n.transmission=?
                 AND p.price_type='precio_nuevo' AND p.verification_status IN ('verificado','parcial')
               ORDER BY p.observed_at DESC, p.id DESC LIMIT 1""",
            (v["model_id"], v["version_name"], v["transmission"])).fetchone()
    rng = conn.execute(
        """SELECT MIN(p.price_cop), MAX(p.price_cop), COUNT(*) FROM vehicle_versions n JOIN price_references p ON p.version_id=n.id
           WHERE n.model_id=? AND p.price_type='precio_nuevo' AND p.verification_status IN ('verificado','parcial')""",
        (v["model_id"],)).fetchone()
    if not rng or not rng[2]:
        return None
    return {
        "same_version_cop": int(same[0]) if same else None,
        "model_min_cop": int(rng[0]), "model_max_cop": int(rng[1]), "model_count": int(rng[2]),
    }
