"""Auditoría de motores: completa el motor de las versiones que la guía publica sin cilindrada.

La guía de precios no siempre escribe la cilindrada en la descripción («SAIL 4P LTZ»,
«X1 SDRIVE20I XLINE»), y esas versiones quedaban con «Motor no especificado». Esta auditoría
solo toca versiones sin cilindrada (o con una etiqueta compuesta como «3.6/3.8 L») y les asigna
el motor documentado en una ficha técnica (``data/fichas_tecnicas/motores_verificados.csv``),
que cita su fuente en ``fuentes_tecnicas.csv``. Si ninguna ficha aplica, la versión no cambia.

Reglas
- E:<ficha>: ficha técnica de la línea y los años de la versión (nivel «verificada» si la fuente
  lo dice para esa versión y año; «inferida» si se deduce de la misma generación o línea).
- E-electrico: versión publicada en la categoría de eléctricos (o con combustible eléctrico):
  no tiene motor de combustión; se muestra «Eléctrico».
- Las etiquetas con cilindrada que ya trae la guía no se cambian, salvo con una ficha de corrección
  (columna ``fix_label``): sirve cuando la guía tomó como cilindrada la designación comercial
  (p. ej. el BMW X5 «xDrive40i», publicado como «4.0», tiene motor de 3.0 litros). Esa ficha solo
  se aplica si la etiqueta ORIGINAL de la versión es exactamente la de ``fix_label``.
- Si dos versiones tienen la misma descripción y solo cambia el combustible que publica la guía
  (p. ej. una híbrida enchufable y otra híbrida ligera), la ficha puede filtrar por combustible
  (columna ``fuel_regex``, sobre el combustible original sin tildes y en minúsculas).
- El motor previo (etiqueta, cilindrada y combustible) se guarda en ``engine_original`` antes del
  primer cambio, y cada versión registra regla, nivel, evidencia y fuentes.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import transmissions as T
from catalog_utils import FAMILY_LABEL, engine_signature, fuel_family, normalize_token, populate_canonical_keys

ROOT = Path(__file__).resolve().parent
SPECS_CSV = ROOT / "data" / "fichas_tecnicas" / "motores_verificados.csv"
SOURCES_CSV = ROOT / "data" / "fichas_tecnicas" / "fuentes_tecnicas.csv"
SPEC_FIELDS = ["spec_id", "brand", "model", "year_from", "year_to", "desc_regex", "exclude_regex",
               "engine_label", "displacement_cc", "fuel", "level", "source_ids", "evidence", "checked_at",
               "fuel_regex", "fix_label"]
ELECTRIC_LABEL = "Eléctrico"
LEVELS = ("verificada", "inferida")


@dataclass
class EngineSpec:
    spec_id: str
    brand: str
    model: str
    year_from: int
    year_to: int
    desc_re: re.Pattern | None
    excl_re: re.Pattern | None
    engine_label: str
    displacement_cc: int | None
    fuel: str | None
    level: str
    source_ids: str
    evidence: str
    fuel_re: re.Pattern | None = None   # filtro opcional sobre el combustible original
    fix_label: str | None = None        # ficha de corrección: etiqueta original exacta que corrige


def load_specs(path: Path = SPECS_CSV) -> list[EngineSpec]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["level"] not in LEVELS:
                raise ValueError(f"Nivel no válido en {r['spec_id']}: {r['level']}")
            out.append(EngineSpec(
                spec_id=r["spec_id"], brand=r["brand"], model=r["model"],
                year_from=int(r["year_from"]), year_to=int(r["year_to"]),
                desc_re=re.compile(r["desc_regex"]) if r["desc_regex"] else None,
                excl_re=re.compile(r["exclude_regex"]) if r["exclude_regex"] else None,
                engine_label=r["engine_label"],
                displacement_cc=int(r["displacement_cc"]) if r["displacement_cc"] else None,
                fuel=r["fuel"] or None, level=r["level"], source_ids=r["source_ids"], evidence=r["evidence"],
                fuel_re=re.compile(r["fuel_regex"]) if r.get("fuel_regex") else None,
                fix_label=r.get("fix_label") or None))
    return out


def desc_text(row: dict) -> str:
    return T.strip_accents(row.get("raw_description") or row.get("version_name") or "").upper().strip()


def needs_engine(label, cc) -> bool:
    """True si la etiqueta no trae una cilindrada única (vacía, «No especificado», «3.6/3.8 L»)."""
    return engine_signature(label, cc)[0] is None


def _original(row: dict) -> dict:
    if row.get("engine_original"):
        return json.loads(row["engine_original"])
    return {"engine_label": row.get("engine_label"), "displacement_cc": row.get("displacement_cc"),
            "fuel": row.get("fuel")}


def fetch_rows(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT v.id, b.name AS brand, m.name AS model, v.model_id, v.model_year, v.version_name,
                  v.engine_label, v.displacement_cc, v.fuel, v.transmission, v.raw_description,
                  v.source_category, v.data_origin, v.engine_original, v.engine_status, v.engine_rule,
                  v.engine_evidence, v.engine_source_ids, v.engine_checked_at, v.generation
           FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           ORDER BY v.id""")]


def target_for(row: dict, specs_by_model: dict) -> dict | None:
    """Motor que corresponde a la versión según su estado ORIGINAL (idempotente)."""
    orig = _original(row)
    pending = needs_engine(orig.get("engine_label"), orig.get("displacement_cc"))
    fuel_txt = T.strip_accents(orig.get("fuel") or "").lower()
    if pending and (fuel_txt == "electrico"
                    or (row.get("source_category") or "").upper().replace("É", "E") == "ELECTRICOS"):
        # Un eléctrico no tiene motor de combustión: ninguna ficha de motor se le aplica, aunque
        # comparta descripción con una versión a gasolina (p. ej. «KONA LIMITED» 2026).
        return {"engine_label": ELECTRIC_LABEL, "displacement_cc": None, "fuel": orig.get("fuel") or "Eléctrico",
                "status": "regla_tecnica", "rule": "E-electrico",
                "evidence": "La guía publica esta versión en la categoría de eléctricos: no tiene motor de combustión "
                            "ni cilindrada.", "source_ids": ""}
    text = desc_text(row)
    year = int(row["model_year"])
    cands = [s for s in specs_by_model.get((normalize_token(row["brand"]), normalize_token(row["model"])), [])
             if s.year_from <= year <= s.year_to
             # una ficha normal solo completa versiones sin cilindrada; una de corrección solo toca la
             # etiqueta original exacta que corrige
             and ((s.fix_label is None and pending) or (s.fix_label is not None and s.fix_label == orig.get("engine_label")))
             and (s.desc_re is None or s.desc_re.search(text))
             and not (s.excl_re and s.excl_re.search(text))
             and (s.fuel_re is None or s.fuel_re.search(fuel_txt))]
    if cands:
        outcomes = {(s.engine_label, s.displacement_cc, s.fuel) for s in cands}
        if len(outcomes) > 1:
            return {"conflict": [s.spec_id for s in cands]}
        best = sorted(cands, key=lambda s: (s.level != "verificada", s.desc_re is None, s.year_to - s.year_from))[0]
        fuel = orig.get("fuel") or best.fuel
        if best.fuel and orig.get("fuel") and "según categoría" in orig["fuel"]:
            fuel = best.fuel  # la ficha precisa el tipo de híbrido que la guía solo daba por su categoría
        if best.fix_label is not None and best.fuel:
            fuel = best.fuel  # una ficha de corrección documenta también el combustible correcto
        return {"engine_label": best.engine_label, "displacement_cc": best.displacement_cc, "fuel": fuel,
                "status": best.level, "rule": f"E:{best.spec_id}",
                "evidence": f"Ficha {best.spec_id}: {best.evidence}", "source_ids": best.source_ids}
    return None


USED_LIST, NEW_LIST = "revista_motor_usados", "revista_motor_nuevos"


def _fuel_bucket(fuel) -> str:
    """Familia de energía para decidir si dos versiones son la misma: gasolina y «sin dato» van juntas."""
    fam = fuel_family(fuel)
    return fam if fam in ("hibrido", "diesel", "electrico", "gas") else "gasolina"


def run_engine_audit(conn, apply: bool = False, specs: list[EngineSpec] | None = None) -> dict:
    """Simula (o aplica) los motores documentados.

    Choques: si al ponerle el motor a una versión queda igual a otra del mismo año (mismo nombre,
    motor, tipo de caja y familia de energía), no se crea un duplicado:
    - si una viene de la lista de usados y la otra de la lista de nuevos de la misma edición de la
      guía, son la misma versión publicada dos veces: sus precios se pasan a la que ya tiene el
      motor y la otra se elimina (fusión registrada en ``audit_log``, acción ``engine_audit_fusion``);
    - en cualquier otro caso la versión queda sin cambio y se informa como colisión.
    """
    specs = load_specs() if specs is None else specs
    by_model: dict[tuple, list[EngineSpec]] = {}
    for s in specs:
        by_model.setdefault((normalize_token(s.brand), normalize_token(s.model)), []).append(s)
    rows = fetch_rows(conn)
    by_id = {r["id"]: r for r in rows}

    def nkey(r, label, fuel):
        return (r["model_id"], r["model_year"], normalize_token(r["version_name"]), label,
                T.category_for_key(r["transmission"]), _fuel_bucket(fuel))

    def xkey(r, label):  # restricción UNIQUE de la tabla
        return (r["model_id"], r["model_year"], r["version_name"], label, r["transmission"])

    taken = {nkey(r, r["engine_label"], r["fuel"]): r["id"] for r in rows}
    exact = {xkey(r, r["engine_label"]): r["id"] for r in rows}
    price_keys: dict[int, set] = {}
    for vid, url, price, obs in conn.execute("SELECT version_id, source_url, price_cop, observed_at FROM price_references"):
        price_keys.setdefault(vid, set()).add((url, price, obs))
    today = date.today().isoformat()
    changes, merges, conflicts, collisions, missing, used = [], [], [], [], [], set()
    merged_away: set[int] = set()
    cur_fuel = {r["id"]: r["fuel"] for r in rows}   # combustible vigente (tras los cambios ya decididos)

    def find_other(r, name, label, fuel):
        rr = dict(r, version_name=name)
        on, ox = taken.get(nkey(rr, label, fuel)), exact.get(xkey(rr, label))
        return on, next((o for o in (on, ox) if o is not None and o != r["id"]), None)

    for r in rows:
        tgt = target_for(r, by_model)
        if tgt is None:
            if needs_engine(r["engine_label"], r["displacement_cc"]):
                missing.append(r)
            continue
        if "conflict" in tgt:
            conflicts.append({"version_id": r["id"], "specs": tgt["conflict"]})
            continue
        used.add(tgt["rule"])
        name = r["version_name"]
        other_n, other = find_other(r, name, tgt["engine_label"], tgt["fuel"])
        if other is not None:
            o = by_id[other]
            same_version = (other == other_n and other not in merged_away
                            and {r["data_origin"], o["data_origin"]} == {USED_LIST, NEW_LIST}
                            and not (price_keys.get(r["id"], set()) & price_keys.get(other, set())))
            if same_version:
                merges.append((r, tgt, other))
                merged_away.add(r["id"])
                price_keys.setdefault(other, set()).update(price_keys.pop(r["id"], set()))
                taken.pop(nkey(r, r["engine_label"], r["fuel"]), None)
                exact.pop(xkey(r, r["engine_label"]), None)
                continue
            fam = _fuel_bucket(tgt["fuel"])
            if fam != _fuel_bucket(cur_fuel.get(other)) and fam != "gasolina":
                # Misma descripción y motor pero distinta energía (p. ej. la híbrida y la de gasolina):
                # son versiones distintas; la etiqueta dice cuál es la híbrida/diésel.
                label = f"{tgt['engine_label']} {FAMILY_LABEL.get(fam, fam)}"
                if find_other(r, name, label, tgt["fuel"])[1] is None:
                    tgt = dict(tgt, engine_label=label,
                               evidence=tgt["evidence"] + f" La etiqueta dice «{FAMILY_LABEL.get(fam, fam)}» para "
                                                          "distinguirla de la versión del mismo nombre con otra energía.")
                    other = None
            elif normalize_token(r.get("generation")) != normalize_token(o.get("generation")) and r.get("generation"):
                # Dos generaciones con el mismo nombre de versión el mismo año (p. ej. «Nuevo Sandero
                # Expression» y «Sandero 1.6 Expression» de 2016): el nombre lleva la generación.
                new_name = f"{name} ({r['generation']} {r['model']})"
                if find_other(r, new_name, tgt["engine_label"], tgt["fuel"])[1] is None:
                    tgt = dict(tgt, rename=new_name)
                    name = new_name
                    other = None
            if other is not None:
                collisions.append({"version_id": r["id"], "con": other, "motor": tgt["engine_label"]})
                continue
        same = (r["engine_label"] == tgt["engine_label"] and r["displacement_cc"] == tgt["displacement_cc"]
                and r["fuel"] == tgt["fuel"] and r["engine_rule"] == tgt["rule"]
                and r["engine_evidence"] == tgt["evidence"] and (r["engine_source_ids"] or "") == tgt["source_ids"]
                and r["engine_status"] == tgt["status"] and not tgt.get("rename"))
        if same:
            continue
        changes.append((r, tgt))
        taken.pop(nkey(r, r["engine_label"], r["fuel"]), None)
        exact.pop(xkey(r, r["engine_label"]), None)
        rr = dict(r, version_name=name)
        taken[nkey(rr, tgt["engine_label"], tgt["fuel"])] = r["id"]
        exact[xkey(rr, tgt["engine_label"])] = r["id"]
        cur_fuel[r["id"]] = tgt["fuel"]
    if apply and (changes or merges):
        # Primero cada versión que cambia recibe una etiqueta provisional única: así el orden de las
        # actualizaciones no choca con la restricción UNIQUE (una versión puede tomar la etiqueta que
        # otra deja libre en esta misma pasada).
        conn.executemany("UPDATE vehicle_versions SET engine_label=? WHERE id=?",
                         [(f"__provisional_{r['id']}", r["id"]) for r, _ in changes])
        for r, tgt in changes:
            orig = r["engine_original"] or json.dumps(
                {"engine_label": r["engine_label"], "displacement_cc": r["displacement_cc"], "fuel": r["fuel"]},
                ensure_ascii=False)
            conn.execute(
                """UPDATE vehicle_versions SET engine_label=?, displacement_cc=?, fuel=?, engine_original=?,
                       engine_status=?, engine_rule=?, engine_evidence=?, engine_source_ids=?, engine_checked_at=?,
                       version_name=?
                   WHERE id=?""",
                (tgt["engine_label"], tgt["displacement_cc"], tgt["fuel"], orig, tgt["status"], tgt["rule"],
                 tgt["evidence"], tgt["source_ids"], today, tgt.get("rename") or r["version_name"], r["id"]))
            if tgt.get("rename"):
                conn.execute(
                    "INSERT INTO audit_log(action, entity_type, entity_id, details, created_at) VALUES(?,?,?,?,datetime('now'))",
                    ("engine_audit_nombre", "vehicle_versions", r["id"], json.dumps({
                        "nombre_anterior": r["version_name"], "nombre_nuevo": tgt["rename"],
                        "descripcion": r["raw_description"], "anio": r["model_year"],
                        "motivo": "Otra generación del mismo año tiene una versión con el mismo nombre, motor y caja; "
                                  "el nombre lleva la generación para no mostrar dos versiones iguales.",
                    }, ensure_ascii=False)))
        for r, tgt, keep in merges:
            moved = [dict(x) for x in conn.execute(
                "SELECT id, price_type, price_cop, model_year, source_url FROM price_references WHERE version_id=?",
                (r["id"],))]
            conn.execute("UPDATE price_references SET version_id=? WHERE version_id=?", (keep, r["id"]))
            conn.execute("DELETE FROM vehicle_versions WHERE id=?", (r["id"],))
            conn.execute(
                "INSERT INTO audit_log(action, entity_type, entity_id, details, created_at) VALUES(?,?,?,?,datetime('now'))",
                ("engine_audit_fusion", "vehicle_versions", keep, json.dumps({
                    "version_eliminada": r["id"], "version_conservada": keep,
                    "descripcion_eliminada": r["raw_description"], "origen_eliminada": r["data_origin"],
                    "nombre": r["version_name"], "anio": r["model_year"], "motor": tgt["engine_label"],
                    "regla_motor": tgt["rule"], "precios_movidos": moved,
                    "motivo": "Misma versión publicada en la lista de usados y en la de nuevos de la misma edición: "
                              "al documentar su motor quedaba duplicada (mismo año, nombre, motor y tipo de caja).",
                }, ensure_ascii=False)))
        key_collisions = populate_canonical_keys(conn)
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details, created_at) VALUES(?,?,?,?,datetime('now'))",
            ("engine_audit", "vehicle_versions", None,
             json.dumps({"versiones_cambiadas": len(changes), "versiones_fusionadas": len(merges),
                         "colisiones_clave": len(key_collisions)}, ensure_ascii=False)))
        conn.commit()
    unused = sorted({f"E:{s.spec_id}" for s in specs} - used)
    return {"changed": len(changes), "merged": len(merges), "conflicts": conflicts, "collisions": collisions,
            "missing": missing, "unused_specs": unused, "changes": changes, "merges": merges}
