"""Normalización y deduplicación conservadora del catálogo de vehículos."""
from __future__ import annotations

import re
import unicodedata

from transmissions import category_for_key


def normalize_token(value) -> str:
    """Normaliza acentos, mayúsculas, puntuación y espacios para comparar nombres."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def vehicle_canonical_key(
    brand, model, model_year, generation, version_name, engine_label, fuel, transmission
) -> str:
    """Clave lógica estricta para identificar una configuración/version de vehículo.

    La transmisión entra por su CATEGORÍA normalizada («Mecánica», «Automática» o «Sin
    confirmar»): dos registros que solo difieren en cómo se escribió la caja («Mecánica» y
    «Mecánica 5 velocidades», «AT» y «Automática») son la misma configuración y no pueden
    coexistir como versiones distintas.
    """
    fields = (
        brand, model, str(model_year or ""), generation, version_name,
        engine_label, fuel, category_for_key(transmission),
    )
    return "|".join(normalize_token(field) for field in fields)


def vehicle_broad_key(
    brand, model, model_year, version_name, engine_label, fuel, transmission
) -> str:
    """Clave sin generación para detectar posibles duplicados que requieren revisión."""
    fields = (
        brand, model, str(model_year or ""), version_name,
        engine_label, fuel, category_for_key(transmission),
    )
    return "|".join(normalize_token(field) for field in fields)


# ---------------------------------------------------------------------------
# Agrupación de motores para el formulario (marca → línea → año → MOTOR → caja → versión)
# ---------------------------------------------------------------------------
_LITERS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*L\b", re.IGNORECASE)


def fuel_family(fuel) -> str:
    """Familia de energía para agrupar motores: '' cuando el combustible no está documentado."""
    f = normalize_token(fuel)
    if not f:
        return ""
    if "electrico" in f and "hibrido" not in f and "gasolina" not in f:
        return "electrico"
    if "hibrido" in f or ("gasolina" in f and "electrico" in f):
        return "hibrido"
    if "diesel" in f:
        return "diesel"
    if "gas" in f and "gasolina" not in f:
        return "gas"
    if "gasolina" in f:
        return "gasolina"
    return f


FAMILY_LABEL = {"gasolina": "gasolina", "diesel": "diésel", "hibrido": "híbrido", "electrico": "eléctrico", "gas": "gas"}


def engine_signature(engine_label, displacement_cc) -> tuple:
    """(litros, turbo) leídos de la etiqueta del motor; litros None si no hay cilindrada."""
    label = str(engine_label or "")
    m = _LITERS_RE.search(label)
    liters = None
    if m and "/" not in label:
        liters = m.group(1).replace(",", ".")
    elif displacement_cc:
        liters = f"{round(int(displacement_cc) / 1000 + 1e-9, 1):.1f}"
    turbo = bool(re.search(r"turbo", label, re.IGNORECASE))
    return liters, turbo


def engine_groups(rows) -> list[dict]:
    """Agrupa las versiones de una línea y año por motor equivalente para el selector.

    Problema que resuelve: la misma cilindrada aparecía partida en varias opciones
    («1.6 L» y «1.6 L gasolina») según de qué carga venía cada versión, y cada opción
    mostraba solo parte de las cajas. Reglas conservadoras:
    - misma cilindrada en litros, misma condición turbo y misma familia de energía → un grupo;
    - las versiones sin combustible documentado se unen al grupo de gasolina de su cilindrada
      solo si en ese año la gasolina es la ÚNICA energía documentada para esa cilindrada (las
      listas marcan aparte diésel, híbridos y eléctricos); si no, quedan aparte, sin suponer nada;
    - etiquetas sin cilindrada («No especificado», «3.6/3.8 L») no se mezclan con otras.
    La etiqueta del grupo solo menciona el combustible si todas sus versiones lo documentan.
    Devuelve [{engine_id, engine_label, fuel, displacement_cc, member_ids}], ordenado.
    """
    items = []
    for r in rows:
        r = dict(r)
        liters, turbo = engine_signature(r.get("engine_label"), r.get("displacement_cc"))
        r["_liters"], r["_turbo"], r["_family"] = liters, turbo, fuel_family(r.get("fuel"))
        items.append(r)
    known: dict[tuple, set] = {}
    for r in items:
        if r["_liters"] and r["_family"]:
            known.setdefault((r["_liters"], r["_turbo"]), set()).add(r["_family"])
    groups: dict[tuple, list] = {}
    for r in items:
        family = r["_family"]
        if r["_liters"]:
            if not family:
                fams = known.get((r["_liters"], r["_turbo"]), set())
                family = "gasolina" if fams == {"gasolina"} else ""
            key = ("L", r["_liters"], r["_turbo"], family)
        else:
            key = ("X", normalize_token(r.get("engine_label")), r["_family"])
        groups.setdefault(key, []).append(r)
    out = []
    for key, members in groups.items():
        ids = sorted(int(m["id"]) for m in members)
        labels = {m.get("engine_label") for m in members}
        fams = {m["_family"] for m in members}
        fuels = {m.get("fuel") for m in members}
        ccs = {m.get("displacement_cc") for m in members}
        if key[0] == "L":
            if len(labels) == 1:
                label = next(iter(labels))
            else:
                label = f"{key[1]} L" + (" turbo" if key[2] else "")
                if len(fams) == 1 and key[3]:
                    label += f" {FAMILY_LABEL.get(key[3], key[3])}"
        else:
            label = next(iter(labels)) if len(labels) == 1 else (members[0].get("engine_label") or "")
            if normalize_token(label) in ("noespecificado", ""):
                label = "Motor no especificado"
        out.append({
            "engine_id": ids[0],
            "engine_label": label,
            "fuel": next(iter(fuels)) if len(fuels) == 1 else None,
            "displacement_cc": next(iter(ccs)) if len(ccs) == 1 else None,
            "member_ids": ids,
        })
    out.sort(key=lambda g: (g["engine_label"] == "Motor no especificado", g["engine_label"].lower(), g["engine_id"]))
    return out


def engine_group_of(rows, engine_id: int) -> dict | None:
    for group in engine_groups(rows):
        if int(engine_id) in group["member_ids"]:
            return group
    return None


def get_or_create_brand(conn, name: str, country: str | None = None) -> int:
    """Reutiliza una marca existente aunque varíen acentos, mayúsculas o puntuación."""
    wanted = normalize_token(name)
    if not wanted:
        raise ValueError("La marca está vacía.")
    for row in conn.execute("SELECT id, name FROM brands"):
        if normalize_token(row["name"]) == wanted:
            return int(row["id"])
    conn.execute("INSERT INTO brands(name, country) VALUES(?, ?)", (name.strip(), country))
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def get_or_create_model(conn, brand_id: int, name: str, category: str | None = None) -> int:
    """Reutiliza una línea/modelo existente con normalización conservadora."""
    wanted = normalize_token(name)
    if not wanted:
        raise ValueError("La línea/modelo está vacía.")
    for row in conn.execute("SELECT id, name FROM models WHERE brand_id=?", (brand_id,)):
        if normalize_token(row["name"]) == wanted:
            return int(row["id"])
    conn.execute(
        "INSERT INTO models(brand_id, name, category) VALUES(?, ?, ?)",
        (brand_id, name.strip(), category),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def find_version_duplicate(conn, brand: str, model: str, model_id: int, values: dict):
    """
    Devuelve (tipo, fila) para duplicado exacto o posible.
    'exacto' = misma clave lógica; 'posible' = coincide salvo generación.
    """
    canonical = vehicle_canonical_key(
        brand, model, values.get("model_year"), values.get("generation"),
        values.get("version_name"), values.get("engine_label"),
        values.get("fuel"), values.get("transmission"),
    )
    row = conn.execute(
        "SELECT id, model_year, generation, version_name, engine_label, fuel, transmission, canonical_key "
        "FROM vehicle_versions WHERE canonical_key=?",
        (canonical,),
    ).fetchone()
    if row:
        return "exacto", row

    broad = vehicle_broad_key(
        brand, model, values.get("model_year"), values.get("version_name"),
        values.get("engine_label"), values.get("fuel"), values.get("transmission"),
    )
    rows = conn.execute(
        "SELECT id, model_year, generation, version_name, engine_label, fuel, transmission, canonical_key "
        "FROM vehicle_versions WHERE model_id=? AND model_year=?",
        (model_id, int(values.get("model_year") or 0)),
    ).fetchall()
    for candidate in rows:
        # Se omite la generación a propósito: una diferencia de documentación
        # se considera posible duplicado y debe revisarse, no importarse a ciegas.
        candidate_broad = vehicle_broad_key(
            brand, model, candidate["model_year"], candidate["version_name"],
            candidate["engine_label"], candidate["fuel"], candidate["transmission"],
        )
        if candidate_broad == broad:
            return "posible", candidate
    return None, None


def populate_canonical_keys(conn) -> list[dict]:
    """Rellena claves lógicas de versiones existentes; retorna colisiones detectadas."""
    rows = conn.execute(
        """SELECT v.id, v.model_id, b.name AS brand, m.name AS model, v.model_year,
                  v.generation, v.version_name, v.engine_label, v.fuel, v.transmission
           FROM vehicle_versions v
           JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           ORDER BY v.id"""
    ).fetchall()
    current = {int(r[0]): r[1] for r in conn.execute("SELECT id, canonical_key FROM vehicle_versions")}
    groups: dict[str, list[int]] = {}
    for row in rows:
        key = vehicle_canonical_key(
            row["brand"], row["model"], row["model_year"], row["generation"],
            row["version_name"], row["engine_label"], row["fuel"], row["transmission"],
        )
        groups.setdefault(key, []).append(int(row["id"]))
    wanted: dict[int, str | None] = {}
    collisions = []
    for key, ids in groups.items():
        # Si varias filas dan la misma clave, la conserva la que ya la tenía (o la más antigua);
        # las demás quedan sin clave y se informan como duplicados para revisarlas.
        keeper = next((i for i in ids if current.get(i) == key), ids[0])
        wanted[keeper] = key
        for other in ids:
            if other != keeper:
                wanted[other] = None
                collisions.append({"canonical_key": key, "first_id": keeper, "duplicate_id": other})
    changing = [vid for vid, key in wanted.items() if current.get(vid) != key]
    # Primero se liberan las claves que cambian, para que un intercambio no choque con el índice único.
    conn.executemany("UPDATE vehicle_versions SET canonical_key=NULL WHERE id=?", [(vid,) for vid in changing])
    conn.executemany("UPDATE vehicle_versions SET canonical_key=? WHERE id=?",
                     [(wanted[vid], vid) for vid in changing if wanted[vid] is not None])
    return collisions


def find_potential_duplicate_pairs(conn) -> list[dict]:
    """Lista pares que solo difieren en generación o en escritura normalizada."""
    rows = conn.execute(
        """SELECT v.id, b.name AS brand, m.name AS model, v.model_year, v.generation,
                  v.version_name, v.engine_label, v.fuel, v.transmission, v.canonical_key
           FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           ORDER BY b.name, m.name, v.model_year, v.id"""
    ).fetchall()
    groups: dict[str, list] = {}
    for row in rows:
        key = vehicle_broad_key(
            row["brand"], row["model"], row["model_year"], row["version_name"],
            row["engine_label"], row["fuel"], row["transmission"],
        )
        groups.setdefault(key, []).append(row)
    pairs = []
    for key, group in groups.items():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if a["canonical_key"] != b["canonical_key"]:
                        pairs.append({
                            "broad_key": key,
                            "id_a": a["id"], "id_b": b["id"],
                            "vehicle_a": f'{a["brand"]} {a["model"]} {a["model_year"]} {a["version_name"]}',
                            "vehicle_b": f'{b["brand"]} {b["model"]} {b["model_year"]} {b["version_name"]}',
                            "generation_a": a["generation"] or "",
                            "generation_b": b["generation"] or "",
                            "reason": "Coincidencia sin generación; revisar antes de fusionar.",
                        })
    return pairs
