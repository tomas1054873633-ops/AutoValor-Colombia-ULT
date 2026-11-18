"""Auditoría reproducible e idempotente de transmisiones de AutoValor Pro Colombia.

Qué hace ``run_audit``:

1. Examina TODAS las versiones del catálogo (no una muestra).
2. Calcula para cada una la transmisión que se puede sostener con evidencia:
   - F  (ficha): una fuente técnica registrada en ``data/fichas_tecnicas`` confirma la caja
     para la marca, línea, años y versión → estado «verificada».
   - R0 (carga inicial): el texto de transmisión con que se cargó la versión («Mecánica
     5 velocidades»...). «verificada» si la versión tiene ficha técnica oficial verificada;
     si no, «documentada».
   - R1 (marca en la descripción): la descripción comercial marca la caja («AUT.», «CVT»,
     «DSG», «S-TRONIC», «MULT.»…) → «documentada».
   - R2 (pareja): la descripción no marca caja, pero la MISMA descripción aparece en la misma
     lista y categoría con marca automática → es la variante no automática → «Mecánica»,
     estado «inferida» (no es una verificación técnica).
   - R3 (eléctricos): versiones de la sección de eléctricos → «Automática», subtipo de
     vehículo eléctrico, estado «regla_tecnica».
   - Sin base → «Sin confirmar», estado «pendiente». Nunca se adivina.
3. Detecta contradicciones (p. ej. marca automática frente a una ficha que solo admite caja
   mecánica) y las deja en estado «conflicto» para revisión.
4. En modo aplicado: conserva el valor original en ``transmission_original``, actualiza los
   campos, registra cada cambio en ``transmission_changes`` y recalcula la clave lógica.
   Ejecutarla de nuevo no cambia nada si los datos y las fichas no cambiaron.

No se modifica ni se borra ningún precio: las referencias de precio siguen ligadas a la misma
fila de versión (solo cambian los campos de transmisión de esa fila).
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import transmissions as T
from catalog_utils import engine_groups, normalize_token, vehicle_canonical_key

ROOT = Path(__file__).resolve().parent
FICHAS_DIR = ROOT / "data" / "fichas_tecnicas"
SOURCES_CSV = FICHAS_DIR / "fuentes_tecnicas.csv"
SPECS_CSV = FICHAS_DIR / "transmisiones_verificadas.csv"
RM_DIR = ROOT / "data" / "revista_motor"
RULES_VERSION = "2026-10-29"
RM_ORIGINS = ("revista_motor_usados", "revista_motor_nuevos")
EV_SECTIONS = {("revista_motor_nuevos", "ELÉCTRICOS")}

SOURCE_FIELDS = ["id", "kind", "title", "publisher", "url", "published_date", "accessed_date", "market", "notes"]
SOURCE_KINDS = {"ficha_oficial", "distribuidor", "prensa_especializada", "glosario_fabricante", "glosario_tecnico",
                "manual_fabricante", "catalogo_historico", "lista_precios"}
# Fuentes que documentan qué tipo de caja designa cada nombre comercial (DSG, S tronic, PowerShift,
# Multitronic, ASG...). Se citan cuando el subtipo sale de esa marca en la descripción.
GLOSSARY_SOURCE_IDS = ("GLOS-QCMC-AUTOMATICAS", "GLOS-WIKI-DSG")
SPEC_FIELDS = ["spec_id", "brand", "model", "year_from", "year_to", "desc_regex", "exclude_regex",
               "engine_regex", "origin", "offering", "level", "mt_gears", "at_subtype", "at_gears", "at_detail",
               "source_ids", "evidence", "checked_at"]
# Nivel de la ficha: «verificada» si la fuente documenta expresamente la caja de esa versión en ese año
# modelo; «inferida» si la caja se deduce de la fuente (lista de precios que solo rotula las automáticas,
# o años intermedios entre dos fuentes de la misma generación con la misma oferta).
SPEC_LEVELS = ("verificada", "inferida")
TRACKED_FIELDS = ("transmission", "transmission_subtype", "transmission_gears", "transmission_detail",
                  "transmission_status", "transmission_rule", "transmission_evidence", "transmission_source_ids")

# Texto de ayuda reutilizado en evidencias
CONVENTION_NOTE = ("En las listas de precios usadas, las versiones automáticas se publican marcadas («AUT.», «CVT», "
                   "«DSG»…) y las no marcadas son la variante mecánica cuando la ficha confirma que esa versión "
                   "tenía caja mecánica.")


# ---------------------------------------------------------------------------
# Carga y validación de fuentes técnicas y fichas
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def load_sources(path: Path = SOURCES_CSV) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            row = {k: (row.get(k) or "").strip() for k in SOURCE_FIELDS}
            sid = row["id"]
            if not sid:
                raise ValueError(f"{path.name}:{n}: fuente sin id")
            if sid in out:
                raise ValueError(f"{path.name}:{n}: id de fuente repetido {sid}")
            if row["kind"] not in SOURCE_KINDS:
                raise ValueError(f"{path.name}:{n}: tipo de fuente inválido {row['kind']!r}")
            if not row["title"] or not row["url"].startswith("https://"):
                raise ValueError(f"{path.name}:{n}: la fuente {sid} requiere título y URL https")
            try:
                accessed = date.fromisoformat(row["accessed_date"])
            except ValueError as exc:
                raise ValueError(f"{path.name}:{n}: fecha de consulta inválida en {sid}") from exc
            if accessed > date.today():
                raise ValueError(f"{path.name}:{n}: fecha de consulta futura en {sid}")
            out[sid] = row
    return out


@dataclass
class Spec:
    spec_id: str
    brand: str
    model: str
    year_from: int
    year_to: int
    desc_regex: str
    exclude_regex: str
    engine_regex: str
    origin: str
    offering: str
    level: str
    mt_gears: int | None
    at_subtype: str | None
    at_gears: int | None
    at_detail: str | None
    source_ids: str
    evidence: str
    checked_at: str
    desc_re: re.Pattern | None = None
    excl_re: re.Pattern | None = None
    engine_re: re.Pattern | None = None


def _opt_int(value: str, where: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"{where}: se esperaba un número entero y llegó {value!r}")
    return int(value)


def load_specs(path: Path = SPECS_CSV, sources: dict[str, dict] | None = None) -> list[Spec]:
    if not path.exists():
        return []
    sources = sources if sources is not None else load_sources()
    specs: list[Spec] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            row = {k: (row.get(k) or "").strip() for k in SPEC_FIELDS}
            where = f"{path.name}:{n}"
            if not row["spec_id"] or row["spec_id"] in seen:
                raise ValueError(f"{where}: spec_id vacío o repetido")
            seen.add(row["spec_id"])
            if row["offering"] not in ("MT", "AT", "MT+AT"):
                raise ValueError(f"{where}: offering debe ser MT, AT o MT+AT")
            if row["level"] not in SPEC_LEVELS:
                raise ValueError(f"{where}: level debe ser verificada o inferida")
            if row["at_subtype"] and row["at_subtype"] not in T.AUTOMATIC_SUBTYPE_CODES:
                raise ValueError(f"{where}: subtipo automático desconocido {row['at_subtype']!r}")
            ids = [s for s in row["source_ids"].split(";") if s.strip()]
            if not ids:
                raise ValueError(f"{where}: cada ficha necesita al menos una fuente")
            missing = [s for s in ids if s.strip() not in sources]
            if missing:
                raise ValueError(f"{where}: fuentes no registradas en fuentes_tecnicas.csv: {missing}")
            if not row["evidence"]:
                raise ValueError(f"{where}: falta la evidencia (qué dice la fuente)")
            y0, y1 = _opt_int(row["year_from"], where), _opt_int(row["year_to"], where)
            if y0 is None or y1 is None or y0 > y1:
                raise ValueError(f"{where}: rango de años inválido")
            spec = Spec(
                spec_id=row["spec_id"], brand=row["brand"], model=row["model"], year_from=y0, year_to=y1,
                desc_regex=row["desc_regex"], exclude_regex=row["exclude_regex"], engine_regex=row["engine_regex"],
                origin=row["origin"], offering=row["offering"], level=row["level"],
                mt_gears=_opt_int(row["mt_gears"], where),
                at_subtype=row["at_subtype"] or None, at_gears=_opt_int(row["at_gears"], where),
                at_detail=row["at_detail"] or None, source_ids=";".join(s.strip() for s in ids),
                evidence=row["evidence"], checked_at=row["checked_at"],
            )
            try:
                spec.desc_re = re.compile(spec.desc_regex) if spec.desc_regex else None
                spec.excl_re = re.compile(spec.exclude_regex) if spec.exclude_regex else None
                spec.engine_re = re.compile(spec.engine_regex, re.IGNORECASE) if spec.engine_regex else None
            except re.error as exc:
                raise ValueError(f"{where}: expresión regular inválida: {exc}") from exc
            specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# Cálculo de la transmisión sostenible para cada versión
# ---------------------------------------------------------------------------
@dataclass
class Target:
    transmission: str
    transmission_subtype: str | None
    transmission_gears: int | None
    transmission_detail: str | None
    transmission_status: str
    transmission_rule: str
    transmission_evidence: str
    transmission_source_ids: str | None
    issues: list[str] = field(default_factory=list)

    def values(self) -> dict:
        return {k: getattr(self, k) for k in TRACKED_FIELDS}


def _subtype_name(code: str | None) -> str | None:
    return T.SUBTYPES.get(code) if code else None


# Marca que llevan en «notes» las variantes automáticas agregadas cuando la ficha técnica indica
# caja mecánica y automática y la guía publica un solo precio sin separar la caja.
VARIANT_NOTE = "Variante automática agregada"


def _is_added_variant(row: dict) -> bool:
    return VARIANT_NOTE in (row.get("notes") or "")


def _desc_text(row: dict) -> str:
    text = T.strip_accents(row.get("raw_description") or row.get("version_name") or "").upper()
    if _is_added_variant(row) and text.endswith(" AUT."):
        text = text[: -len(" AUT.")]  # la ficha se busca con la descripción original de la guía
    return text


def _row_origin(row: dict) -> str:
    return row.get("data_origin") or "catalogo"


def fetch_rows(conn) -> list[dict]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vehicle_versions)")}
    extra = [c for c in ("transmission_original", "transmission_subtype", "transmission_gears",
                         "transmission_detail", "transmission_status", "transmission_rule",
                         "transmission_evidence", "transmission_source_ids", "transmission_checked_at") if c in cols]
    select_extra = "".join(f", v.{c}" for c in extra)
    rows = conn.execute(
        f"""SELECT v.id, b.name AS brand, m.name AS model, v.model_id, v.model_year, v.generation, v.version_name,
                   v.engine_label, v.displacement_cc, v.fuel, v.transmission, v.raw_description, v.data_origin,
                   v.source_category, v.verification_status, v.technical_source_url, v.technical_source_name,
                   v.technical_source_checked_at, v.canonical_key, v.notes {select_extra}
            FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
            ORDER BY v.id"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for c in ("transmission_original", "transmission_subtype", "transmission_gears", "transmission_detail",
                  "transmission_status", "transmission_rule", "transmission_evidence", "transmission_source_ids",
                  "transmission_checked_at"):
            d.setdefault(c, None)
        out.append(d)
    return out


class Context:
    """Índices compartidos por todas las filas (parejas, fichas por marca y línea)."""

    def __init__(self, rows: list[dict], specs: list[Spec], source_ids=()):
        self.source_ids = set(source_ids)
        self.auto_twins: dict[tuple, str] = {}
        for r in rows:
            if _row_origin(r) not in RM_ORIGINS or not r.get("raw_description") or _is_added_variant(r):
                continue  # una variante agregada no es una gemela publicada por la guía
            tok = T.classify_description(r["raw_description"])
            if tok and tok.get("category") == T.CATEGORY_AUTOMATIC:
                key = (r["data_origin"], normalize_token(r["brand"]), r.get("source_category"),
                       T.base_description(r["raw_description"]))
                self.auto_twins.setdefault(key, r["raw_description"])
        self.specs_by_model: dict[tuple, list[Spec]] = defaultdict(list)
        for s in specs:
            self.specs_by_model[(normalize_token(s.brand), normalize_token(s.model))].append(s)

    def twin_of(self, row: dict) -> str | None:
        if _row_origin(row) not in RM_ORIGINS or not row.get("raw_description"):
            return None
        key = (row["data_origin"], normalize_token(row["brand"]), row.get("source_category"),
               T.normalized_description(row["raw_description"]))
        return self.auto_twins.get(key)

    def specs_for(self, row: dict) -> list[Spec]:
        cands = self.specs_by_model.get((normalize_token(row["brand"]), normalize_token(row["model"])), [])
        if not cands:
            return []
        text = _desc_text(row)
        year = int(row["model_year"])
        origin = _row_origin(row)
        out = []
        for s in cands:
            if not (s.year_from <= year <= s.year_to):
                continue
            if s.origin and s.origin != origin:
                continue
            if s.desc_re and not s.desc_re.search(text):
                continue
            if s.excl_re and s.excl_re.search(text):
                continue
            if s.engine_re and not s.engine_re.search(row.get("engine_label") or ""):
                continue
            out.append(s)
        return out


def _merge_detail(*parts) -> str | None:
    seen = []
    for p in parts:
        for piece in str(p or "").split(", "):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.append(piece)
    return ", ".join(seen) or None


def _spec_status(spec: Spec) -> str:
    return T.STATUS_VERIFIED if spec.level == "verificada" else T.STATUS_INFERRED


def _level_note(spec: Spec) -> str:
    if spec.level == "verificada":
        return ""
    return (" Nivel de la ficha: inferida (la fuente no lo dice expresamente para esta versión y año; ver la "
            "evidencia), por eso no se marca como verificada.")


def _spec_target(row: dict, spec: Spec, tok: dict | None, twin: str | None) -> Target:
    ev = f"Ficha {spec.spec_id}: {spec.evidence}"
    rule = f"F:{spec.spec_id}"
    if _is_added_variant(row) and spec.offering == "MT+AT":
        return Target(T.CATEGORY_AUTOMATIC, _subtype_name(spec.at_subtype), spec.at_gears, spec.at_detail,
                      _spec_status(spec), rule,
                      f"{ev} Variante automática de la misma versión: la ficha documenta caja mecánica y "
                      f"automática y la guía publica un solo precio sin separar la caja, por eso se ofrecen "
                      f"ambas con la misma referencia de precio.{_level_note(spec)}", spec.source_ids)
    if tok and tok.get("category") == T.CATEGORY_AUTOMATIC:
        marks = ", ".join(tok.get("tokens") or [])
        if spec.offering == "MT":
            return Target(T.CATEGORY_AUTOMATIC, _subtype_name(tok.get("subtype_code")), None, tok.get("detail"),
                          T.STATUS_CONFLICT, rule,
                          f"La descripción marca caja automática («{marks}») pero la ficha {spec.spec_id} solo "
                          f"documenta caja mecánica para esta versión y año. {spec.evidence}",
                          spec.source_ids, ["conflicto_marca_vs_ficha"])
        code = tok.get("subtype_code")
        if code and spec.at_subtype and code != spec.at_subtype:
            return Target(T.CATEGORY_AUTOMATIC, _subtype_name(code), None, tok.get("detail"), T.STATUS_CONFLICT, rule,
                          f"La descripción indica subtipo {T.SUBTYPES[code]} y la ficha {spec.spec_id} indica "
                          f"{T.SUBTYPES[spec.at_subtype]}. {spec.evidence}", spec.source_ids, ["conflicto_subtipo"])
        # Con una ficha «inferida» la categoría sigue apoyada en la marca explícita de la descripción
        # («documentada»): la ficha solo aporta subtipo o marchas y no rebaja ni eleva el estado.
        status = T.STATUS_VERIFIED if spec.level == "verificada" else T.STATUS_DOCUMENTED
        return Target(T.CATEGORY_AUTOMATIC, _subtype_name(code or spec.at_subtype), spec.at_gears,
                      _merge_detail(tok.get("detail"), spec.at_detail), status, rule,
                      f"{ev} La versión figura con marca automática («{marks}»).{_level_note(spec)}", spec.source_ids)
    if tok and tok.get("category") == T.CATEGORY_MANUAL:
        if spec.offering == "AT":
            return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], None, None, T.STATUS_CONFLICT, rule,
                          f"La descripción marca caja mecánica pero la ficha {spec.spec_id} solo documenta caja "
                          f"automática. {spec.evidence}", spec.source_ids, ["conflicto_marca_vs_ficha"])
        return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], spec.mt_gears, None, _spec_status(spec), rule,
                      f"{ev} La versión figura con marca de caja mecánica.{_level_note(spec)}", spec.source_ids)
    # Descripción sin marca de caja
    is_catalog = _row_origin(row) not in RM_ORIGINS
    mt_status = _spec_status(spec)
    mt_note = _level_note(spec)
    if spec.offering == "MT":
        return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], spec.mt_gears, None, mt_status, rule,
                      f"{ev} La ficha solo documenta caja mecánica para esta versión.{mt_note}", spec.source_ids)
    if spec.offering == "AT":
        if twin:
            return Target(T.CATEGORY_UNCONFIRMED, None, None, None, T.STATUS_CONFLICT, rule,
                          f"La lista de precios publica esta versión sin marca y también «{twin}» como automática, "
                          f"pero la ficha {spec.spec_id} solo documenta caja automática. {spec.evidence}",
                          spec.source_ids, ["conflicto_pareja_vs_ficha"])
        return Target(T.CATEGORY_AUTOMATIC, _subtype_name(spec.at_subtype), spec.at_gears, spec.at_detail,
                      _spec_status(spec), rule,
                      f"{ev} La ficha solo documenta caja automática para esta versión; la descripción de "
                      f"precio no la marcaba.{_level_note(spec)}", spec.source_ids)
    # MT+AT
    if is_catalog:
        return Target(T.CATEGORY_UNCONFIRMED, None, None, None, T.STATUS_PENDING, rule,
                      f"La ficha {spec.spec_id} documenta caja mecánica y automática para esta versión, y el "
                      f"registro no indica cuál corresponde. {spec.evidence}", spec.source_ids,
                      ["ambas_cajas_sin_identificar"])
    return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], spec.mt_gears, None, mt_status, rule,
                  f"{ev} {CONVENTION_NOTE}{mt_note}" + (f" Su gemela automática es «{twin}»." if twin else ""),
                  spec.source_ids)


def _contradicts(spec: Spec, tok: dict | None, twin: str | None) -> bool:
    cat = (tok or {}).get("category")
    if cat == T.CATEGORY_AUTOMATIC and spec.offering == "MT":
        return True
    if cat == T.CATEGORY_MANUAL and spec.offering == "AT":
        return True
    if cat is None and twin and spec.offering == "AT":
        return True
    return False


def compute_target(row: dict, ctx: Context) -> Target:
    origin = _row_origin(row)
    specs = ctx.specs_for(row)
    issues: list[str] = []
    spec = None
    if len(specs) == 1:
        spec = specs[0]
    elif len(specs) > 1:
        offers = {(s.offering, s.mt_gears, s.at_subtype, s.at_gears) for s in specs}
        if len(offers) == 1:
            spec = specs[0]
        elif len({s.offering for s in specs}) == 1 or (
                {s.offering for s in specs} == {"MT", "MT+AT"} and origin in RM_ORIGINS
                and not (T.classify_description(row.get("raw_description") or "") or {}).get("category")):
            # Varias fichas coinciden en la caja (MT, AT o ambas) y solo difieren en detalle
            # (marchas o subtipo). En una fila de la guía sin marca de caja, «MT» y «MT+AT»
            # llevan a la misma variante mecánica. Se usa la ficha más sólida y específica.
            spec = sorted(specs, key=lambda s: (s.level != "verificada", not s.desc_regex,
                                                s.year_to - s.year_from, -len(s.source_ids)))[0]
            issues.append("fichas_coincidentes:" + ",".join(s.spec_id for s in specs))
        else:
            issues.append("fichas_superpuestas:" + ",".join(s.spec_id for s in specs))

    if origin in RM_ORIGINS:
        raw = row.get("raw_description") or ""
        tok = T.classify_description(raw)
        twin = ctx.twin_of(row)
        if spec and spec.level == "inferida" and _contradicts(spec, tok, twin):
            # Una ficha «inferida» nunca contradice una marca explícita de la guía ni su pareja
            # mecánica/automática: en ese caso no se aplica y la fila conserva su evidencia propia.
            issues.append(f"ficha_inferida_no_aplicada:{spec.spec_id}")
            spec = None
        if tok and tok.get("conflict"):
            return Target(T.CATEGORY_UNCONFIRMED, None, None, None, T.STATUS_CONFLICT, "R1-marcas-contradictorias",
                          f"La descripción «{raw}» contiene marcas de caja mecánica y automática a la vez.", None,
                          issues + ["marcas_contradictorias"])
        if spec:
            target = _spec_target(row, spec, tok, twin)
            target.issues = issues + target.issues
            return target
        if tok and tok.get("category") == T.CATEGORY_AUTOMATIC:
            marks = ", ".join(tok.get("tokens") or [])
            code = tok.get("subtype_code")
            ev = f"La descripción de la versión marca caja automática («{marks}»)."
            glossary = None
            if code:
                glossary = ";".join(g for g in GLOSSARY_SOURCE_IDS if g in ctx.source_ids) or None
                ev += (f" «{marks}» designa una caja {T.SUBTYPES[code]}"
                       + (" (equivalencia documentada en el glosario de fuentes)." if glossary else "."))
            if tok.get("subtype_conflict"):
                issues.append("subtipos_contradictorios_en_descripcion")
            return Target(T.CATEGORY_AUTOMATIC, _subtype_name(code), None, tok.get("detail"), T.STATUS_DOCUMENTED,
                          "R1-marca-en-descripcion", ev + " Sin ficha técnica registrada para esta versión y año.",
                          glossary, issues)
        if tok and tok.get("category") == T.CATEGORY_MANUAL:
            return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], None, None, T.STATUS_DOCUMENTED,
                          "R1-marca-en-descripcion", "La descripción marca caja mecánica.", None, issues)
        if (row.get("data_origin"), row.get("source_category")) in EV_SECTIONS:
            return Target(T.CATEGORY_AUTOMATIC, T.SUBTYPES["ev"], None, None, T.STATUS_RULE, "R3-electrico",
                          "Versión publicada en la sección de vehículos eléctricos: un eléctrico no usa caja de "
                          "cambios convencional ni pedal de embrague; para la consulta se agrupa como automática.",
                          None, issues)
        if twin:
            return Target(T.CATEGORY_MANUAL, T.SUBTYPES["manual"], None, None, T.STATUS_INFERRED, "R2-pareja",
                          f"La versión no marca caja y la misma descripción aparece en la misma lista y categoría "
                          f"como automática («{twin}»): se registra como la variante mecánica. Pendiente de ficha "
                          f"técnica.", None, issues)
        if T.has_ambiguous_token(raw):
            issues.append("marca_ambigua_ST")
        return Target(T.CATEGORY_UNCONFIRMED, None, None, None, T.STATUS_PENDING, "R9-sin-base",
                      "La descripción no indica la caja, no tiene gemela automática en la lista y no hay ficha "
                      "técnica registrada para esta versión y año.", None, issues + ["pendiente_sin_base"])

    # Versiones del catálogo inicial o creadas desde el panel administrativo
    original = row.get("transmission_original")
    if original is None:
        original = row.get("transmission")
    parsed = T.classify_text(original)
    tech_ok = (row.get("verification_status") == "verificado" and str(row.get("technical_source_url") or "")
               .startswith("https://"))
    if parsed:
        if spec:
            agrees = (spec.offering == "MT+AT"
                      or (spec.offering == "MT" and parsed["category"] == T.CATEGORY_MANUAL)
                      or (spec.offering == "AT" and parsed["category"] == T.CATEGORY_AUTOMATIC))
            if not agrees:
                return Target(parsed["category"], _subtype_name(parsed.get("subtype_code")), parsed.get("gears"),
                              parsed.get("detail"), T.STATUS_CONFLICT, f"F:{spec.spec_id}",
                              f"El registro dice «{original}» y la ficha {spec.spec_id} lo contradice. {spec.evidence}",
                              spec.source_ids, issues + ["conflicto_texto_vs_ficha"])
        ev = f"Transmisión registrada con la versión: «{original}»."
        if tech_ok:
            ev += (f" Ficha técnica de la versión: {row.get('technical_source_name') or 'fuente técnica'} "
                   f"({row.get('technical_source_url')}), consultada el {row.get('technical_source_checked_at') or 's. f.'}.")
            return Target(parsed["category"], _subtype_name(parsed.get("subtype_code")), parsed.get("gears"),
                          parsed.get("detail"), T.STATUS_VERIFIED, "R0-ficha-de-la-version", ev,
                          "ficha-de-la-version", issues)
        if spec:
            return Target(parsed["category"], _subtype_name(parsed.get("subtype_code") or (
                spec.at_subtype if parsed["category"] == T.CATEGORY_AUTOMATIC else "manual")),
                parsed.get("gears") or (spec.at_gears if parsed["category"] == T.CATEGORY_AUTOMATIC else spec.mt_gears),
                parsed.get("detail"), _spec_status(spec) if _spec_status(spec) == T.STATUS_VERIFIED else T.STATUS_DOCUMENTED,
                f"F:{spec.spec_id}", f"{ev} Ficha {spec.spec_id}: {spec.evidence}{_level_note(spec)}", spec.source_ids, issues)
        return Target(parsed["category"], _subtype_name(parsed.get("subtype_code")), parsed.get("gears"),
                      parsed.get("detail"), T.STATUS_DOCUMENTED, "R0-texto-registrado",
                      ev + " Sin ficha técnica verificada para la versión.", None, issues)
    if spec:
        target = _spec_target(row, spec, None, None)
        target.issues = issues + target.issues
        return target
    if normalize_token(row.get("fuel")) == "electrico":
        return Target(T.CATEGORY_AUTOMATIC, T.SUBTYPES["ev"], None, None, T.STATUS_RULE, "R3-electrico",
                      "Vehículo eléctrico: no usa caja de cambios convencional; para la consulta se agrupa como "
                      "automática.", None, issues)
    return Target(T.CATEGORY_UNCONFIRMED, None, None, None, T.STATUS_PENDING, "R9-sin-base",
                  f"El registro no documenta la caja («{original or 'vacío'}») y no hay ficha técnica registrada.",
                  None, issues + ["pendiente_sin_base"])


# ---------------------------------------------------------------------------
# Comprobaciones de integridad (precios ↔ versiones, duplicados, selectores)
# ---------------------------------------------------------------------------
_USED_REF = re.compile(r"^revista-motor:(?P<ed>[^:]+):p(?P<page>\d+):x(?P<x>[^:]+):y(?P<y>[^:]+):a(?P<year>\d{4}):usado$")
_NEW_REF = re.compile(r"^revista-motor:(?P<ed>[^:]+):p(?P<page>\d+):c(?P<col>[^:]+):y(?P<y>[^:]+):nuevo$")


def _load_rm_rows(rm_dir: Path = RM_DIR) -> tuple[dict, dict]:
    used, new = {}, {}
    for path in sorted(rm_dir.glob("revista_motor_*_usados_extraido.csv")):
        edition = path.name.split("_")[2]
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                used[(edition, r["page"], r["col_x"], r["row_top"], r["model_year"])] = r
    for path in sorted(rm_dir.glob("revista_motor_*_nuevos_extraido.csv")):
        edition = path.name.split("_")[2]
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                new[(edition, r["page"], r["col"], r["y"])] = r
    return used, new


def _desc_signature(text: str | None) -> tuple:
    toks = []
    for t in T.strip_accents(text or "").upper().split():
        t = t.strip(".,;")
        toks.append("T" if t == "TURBO" else t)
    return tuple(sorted(x for x in toks if x))


_BOX_MARK_RE = re.compile(r"\b(AUT|AT|MT|CVT|DSG|DCT|IVT|MEC)\b\.?", re.IGNORECASE)


def _strip_box_marks(text: str | None) -> str:
    """Descripción sin las marcas de caja («AUT.», «CVT»…), para comparar la misma versión."""
    return _BOX_MARK_RE.sub(" ", T.strip_accents(text or ""))


def verify_price_links(conn, rm_dir: Path = RM_DIR) -> dict:
    """Cada precio importado debe seguir ligado a la versión cuya descripción y año lo publicaron.

    Además comprueba que la caja que marca la descripción del precio («AUT.», «CVT»...) coincida
    con la categoría de caja de la versión: un precio de una automática nunca puede quedar ligado a
    una versión mecánica (ni al revés). Las descripciones que solo cambian de escritura entre listas
    («AUT» / «AUT.», orden de palabras, «TURBO» / «T.») se informan como variantes equivalentes.
    """
    used, new = _load_rm_rows(rm_dir)
    # Precios que la auditoría de motores pasó de una versión a otra al unir la misma versión publicada
    # en la lista de usados y en la de nuevos (acción «engine_audit_fusion» de audit_log).
    merged_prices: dict[int, str] = {}
    for (details,) in conn.execute("SELECT details FROM audit_log WHERE action='engine_audit_fusion'"):
        try:
            info = json.loads(details or "{}")
        except ValueError:
            continue
        for moved in info.get("precios_movidos") or []:
            merged_prices[int(moved["id"])] = info.get("descripcion_eliminada") or ""
    rows = conn.execute(
        """SELECT p.id, p.version_id, p.source_ref, p.price_cop, p.model_year AS price_year, p.price_type,
                  v.raw_description, v.model_year, v.transmission, b.name AS brand
           FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id
           JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
           WHERE p.source_ref LIKE 'revista-motor:%'"""
    ).fetchall()
    checked, problems, variants = 0, [], []
    for r in rows:
        checked += 1
        ref = r["source_ref"]
        m = _USED_REF.match(ref)
        src = None
        if m:
            src = used.get((m["ed"], m["page"], m["x"], m["y"], m["year"]))
            year_ok = src is not None and int(src["model_year"]) == int(r["model_year"]) == int(r["price_year"])
            price_src = int(src["price_cop"]) if src else None
        else:
            m = _NEW_REF.match(ref)
            if m:
                src = new.get((m["ed"], m["page"], m["col"], m["y"]))
            year_ok = True
            price_src = int(src["price_cop"]) if src and src.get("price_cop") else None
        if src is None:
            problems.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                             "problema": "fila de origen no encontrada en el CSV extraído"})
            continue
        tok = T.classify_description(src["description"])
        cat_src = (tok or {}).get("category")
        cat_ver = r["transmission"] if r["transmission"] in T.CATEGORIES else T.category_for_key(r["transmission"])
        if cat_src and cat_ver in T.CONFIRMED_CATEGORIES and cat_src != cat_ver:
            problems.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                             "problema": f"caja distinta: el precio es de «{src['description']}» ({cat_src}) y la versión es {cat_ver}"})
        elif (src["description"] or "").strip() != (r["raw_description"] or "").strip():
            if _desc_signature(src["description"]) == _desc_signature(r["raw_description"]):
                variants.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                                 "detalle": f"misma versión escrita distinto: «{src['description']}» / «{r['raw_description']}»"})
            elif not cat_src and _desc_signature(_strip_box_marks(src["description"])) == \
                    _desc_signature(_strip_box_marks(r["raw_description"])):
                # La lista de nuevos no marca la caja y la versión (de la guía de usados) sí: es la
                # misma versión cuando, sin las marcas de caja, ambas descripciones coinciden.
                variants.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                                 "detalle": f"misma versión; la fuente no marca la caja: «{src['description']}» / «{r['raw_description']}»"})
            elif r["id"] in merged_prices and price_src == int(r["price_cop"]) and year_ok and \
                    _desc_signature(_strip_box_marks(merged_prices[r["id"]])) == \
                    _desc_signature(_strip_box_marks(src["description"])):
                variants.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                                 "detalle": f"misma versión publicada en las dos listas, unida por la auditoría de motores: «{src['description']}» / «{r['raw_description']}»"})
            else:
                problems.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                                 "problema": f"descripción distinta: CSV «{src['description']}» vs versión «{r['raw_description']}»"})
        elif price_src != int(r["price_cop"]):
            problems.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                             "problema": f"precio distinto: CSV {price_src} vs base {r['price_cop']}"})
        elif not year_ok:
            problems.append({"price_id": r["id"], "version_id": r["version_id"], "source_ref": ref,
                             "problema": "año modelo distinto entre la columna de la guía y la versión"})
    return {"checked": checked, "problems": problems, "variants": variants}


def name_collisions(rows: list[dict], targets: dict[int, Target]) -> dict[int, str]:
    """Versiones distintas que, al asignarles la caja, chocarían con la restricción única de la base
    (línea, año, nombre de versión, motor y caja) porque solo se diferencian por el combustible
    (p. ej. la versión «Allure 1.2» y su variante híbrida ligera de la misma lista de precios).

    Devuelve {id: nombre nuevo}: a la variante con combustible documentado se le añade el combustible
    al nombre, «Allure (Híbrido ligero)», para que ambas convivan sin fusionarse ni perder su precio.
    Si dos filas chocan con el mismo combustible no se renombra nada (es un duplicado real).
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["model_id"], r["model_year"], r["version_name"], r["engine_label"],
                targets[r["id"]].transmission)].append(r)
    renames: dict[int, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        fuels = [normalize_token(m.get("fuel") or "") for m in members]
        if len(set(fuels)) != len(fuels):
            continue
        for m in members:
            if m.get("fuel") and normalize_token(m["fuel"]) not in normalize_token(m["version_name"]):
                fuel = re.sub(r"\s*\(.*\)\s*$", "", m["fuel"]).strip() or m["fuel"]
                renames[m["id"]] = f"{m['version_name']} ({fuel})"
    return renames


def duplicate_report(rows: list[dict], targets: dict[int, Target]) -> dict:
    """Duplicados por escritura de caja (bloqueantes) y posibles duplicados a revisar.

    - exact: misma clave lógica tras normalizar la caja (solo difieren en cómo se escribió).
    - possible: misma marca, línea, año, nombre de versión y categoría de caja, con motores
      compatibles (misma cilindrada o una sin documentar) pero registros distintos; suelen ser la
      misma versión llegada por dos cargas distintas. No se fusionan automáticamente.
    """
    from catalog_utils import engine_signature
    by_key: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tgt = targets[r["id"]].transmission
        key = vehicle_canonical_key(r["brand"], r["model"], r["model_year"], r.get("generation"), r["version_name"],
                                    r["engine_label"], r["fuel"], tgt)
        by_key[key].append(r["id"])
        name = "|".join(normalize_token(x) for x in (r["brand"], r["model"], r["model_year"], r["version_name"], tgt))
        by_name[name].append(r)
    exact = {k: v for k, v in by_key.items() if len(v) > 1}
    possible = []
    for name, members in by_name.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                la, _ = engine_signature(a["engine_label"], a["displacement_cc"])
                lb, _ = engine_signature(b["engine_label"], b["displacement_cc"])
                if la and lb and la != lb:
                    continue
                if (a.get("raw_description") and a.get("raw_description") == b.get("raw_description")
                        and a.get("data_origin") == b.get("data_origin")):
                    continue
                possible.append((a, b))
    return {"exact": exact, "possible": possible}


def selector_inventory(rows: list[dict], targets: dict[int, Target] | None) -> list[dict]:
    """Opciones de motor y caja por línea y año: antes (texto original) y después (categoría)."""
    by_my: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_my[(r["model_id"], r["model_year"])].append(r)
    out = []
    for (model_id, year), members in by_my.items():
        old_engines: dict[tuple, list[dict]] = defaultdict(list)
        for r in members:
            old_engines[(r["engine_label"], r["fuel"], r["displacement_cc"])].append(r)
        groups = engine_groups(members)
        by_id = {r["id"]: r for r in members}
        for g in groups:
            g_rows = [by_id[i] for i in g["member_ids"]]
            before_opts = sorted({(r.get("transmission_original") or r["transmission"]) for r in g_rows})
            old_split = sorted({(r["engine_label"], r["fuel"], r["displacement_cc"]) for r in g_rows}, key=str)
            after = Counter()
            after_status = defaultdict(Counter)
            if targets:
                for r in g_rows:
                    t = targets[r["id"]]
                    after[t.transmission] += 1
                    after_status[t.transmission][t.transmission_status] += 1
            out.append({
                "brand": g_rows[0]["brand"], "model": g_rows[0]["model"], "model_id": model_id, "model_year": year,
                "engine_option": g["engine_label"], "engine_id": g["engine_id"], "versions": len(g_rows),
                "engine_options_before": len(old_split), "engine_labels_before": " | ".join(
                    f"{e[0]}{' ' + e[1] if e[1] else ''}" for e in old_split),
                "transmissions_before": " | ".join(before_opts),
                "transmissions_after": " | ".join(f"{k} ({v})" for k, v in sorted(after.items())),
                "after_detail": {k: dict(v) for k, v in after_status.items()},
            })
    return out


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------
def _norm_value(v):
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def sync_reference_tables(conn, sources: dict[str, dict], specs: list[Spec]) -> None:
    """Copia fuentes y fichas a la base (idempotente) para que la evidencia viaje con los datos."""
    for s in sources.values():
        conn.execute(
            """INSERT INTO technical_sources(id, kind, title, publisher, url, published_date, accessed_date, market, notes)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, title=excluded.title, publisher=excluded.publisher,
                 url=excluded.url, published_date=excluded.published_date, accessed_date=excluded.accessed_date,
                 market=excluded.market, notes=excluded.notes, updated_at=CURRENT_TIMESTAMP
               WHERE technical_sources.title IS NOT excluded.title OR technical_sources.url IS NOT excluded.url
                 OR technical_sources.notes IS NOT excluded.notes OR technical_sources.accessed_date IS NOT excluded.accessed_date
                 OR technical_sources.kind IS NOT excluded.kind OR technical_sources.publisher IS NOT excluded.publisher
                 OR technical_sources.published_date IS NOT excluded.published_date OR technical_sources.market IS NOT excluded.market""",
            tuple(s[k] or None for k in SOURCE_FIELDS),
        )
    for sp in specs:
        vals = (sp.spec_id, sp.brand, sp.model, sp.year_from, sp.year_to, sp.desc_regex or None, sp.exclude_regex or None,
                sp.engine_regex or None, sp.origin or None, sp.offering, sp.level, sp.mt_gears, sp.at_subtype,
                sp.at_gears, sp.at_detail, sp.source_ids, sp.evidence, sp.checked_at or None)
        conn.execute(
            """INSERT INTO transmission_specs(spec_id, brand, model, year_from, year_to, desc_regex, exclude_regex,
                 engine_regex, origin, offering, level, mt_gears, at_subtype, at_gears, at_detail, source_ids, evidence,
                 checked_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(spec_id) DO UPDATE SET brand=excluded.brand, model=excluded.model, year_from=excluded.year_from,
                 year_to=excluded.year_to, desc_regex=excluded.desc_regex, exclude_regex=excluded.exclude_regex,
                 engine_regex=excluded.engine_regex, origin=excluded.origin, offering=excluded.offering,
                 level=excluded.level, mt_gears=excluded.mt_gears, at_subtype=excluded.at_subtype, at_gears=excluded.at_gears,
                 at_detail=excluded.at_detail, source_ids=excluded.source_ids, evidence=excluded.evidence,
                 checked_at=excluded.checked_at, updated_at=CURRENT_TIMESTAMP
               WHERE transmission_specs.evidence IS NOT excluded.evidence OR transmission_specs.source_ids IS NOT excluded.source_ids
                 OR transmission_specs.offering IS NOT excluded.offering OR transmission_specs.desc_regex IS NOT excluded.desc_regex
                 OR transmission_specs.exclude_regex IS NOT excluded.exclude_regex OR transmission_specs.year_from IS NOT excluded.year_from
                 OR transmission_specs.year_to IS NOT excluded.year_to OR transmission_specs.at_subtype IS NOT excluded.at_subtype
                 OR transmission_specs.at_gears IS NOT excluded.at_gears OR transmission_specs.mt_gears IS NOT excluded.mt_gears
                 OR transmission_specs.at_detail IS NOT excluded.at_detail OR transmission_specs.engine_regex IS NOT excluded.engine_regex
                 OR transmission_specs.origin IS NOT excluded.origin OR transmission_specs.checked_at IS NOT excluded.checked_at
                 OR transmission_specs.level IS NOT excluded.level
                 OR transmission_specs.brand IS NOT excluded.brand OR transmission_specs.model IS NOT excluded.model""",
            vals,
        )
    if specs is not None:
        wanted = {sp.spec_id for sp in specs}
        for (sid,) in conn.execute("SELECT spec_id FROM transmission_specs").fetchall():
            if sid not in wanted:
                conn.execute("DELETE FROM transmission_specs WHERE spec_id=?", (sid,))


def run_audit(conn, *, apply: bool = False, specs_path: Path = SPECS_CSV, sources_path: Path = SOURCES_CSV,
              today: date | None = None, with_integrity: bool = True) -> dict:
    """Ejecuta la auditoría. Con ``apply=False`` no escribe nada (simulación)."""
    today = today or date.today()
    sources = load_sources(sources_path)
    specs = load_specs(specs_path, sources)
    rows = fetch_rows(conn)
    ctx = Context(rows, specs, sources.keys())
    targets = {r["id"]: compute_target(r, ctx) for r in rows}

    changes: list[dict] = []
    changed_ids: set[int] = set()
    originals_set = 0
    for r in rows:
        tgt = targets[r["id"]].values()
        diff = {k: (r.get(k), v) for k, v in tgt.items() if _norm_value(r.get(k)) != _norm_value(v)}
        if r.get("transmission_original") is None:
            originals_set += 1
        if diff:
            changed_ids.add(r["id"])
            for k, (old, new) in diff.items():
                changes.append({"version_id": r["id"], "field": k, "old_value": old, "new_value": new,
                                "rule": tgt["transmission_rule"], "evidence": tgt["transmission_evidence"],
                                "source_ids": tgt["transmission_source_ids"]})

    renames = name_collisions(rows, targets)
    for r in rows:
        if r["id"] in renames:
            changed_ids.add(r["id"])
            changes.append({"version_id": r["id"], "field": "version_name", "old_value": r["version_name"],
                            "new_value": renames[r["id"]], "rule": "desambiguar-nombre",
                            "evidence": "Dos versiones distintas de la misma lista solo se diferencian por el "
                                        "combustible; se añade el combustible al nombre para que no se confundan.",
                            "source_ids": None})
    dups = duplicate_report([{**r, "version_name": renames.get(r["id"], r["version_name"])} for r in rows], targets)
    run_id = None
    if apply:
        if dups["exact"]:
            raise RuntimeError(
                "La normalización produciría versiones duplicadas (misma clave lógica): "
                + "; ".join(",".join(map(str, v)) for v in list(dups["exact"].values())[:10])
                + ". Resuélvelas antes de aplicar.")
        sync_reference_tables(conn, sources, specs)
        cur = conn.execute(
            """INSERT INTO transmission_audit_runs(mode, rules_version, specs_sha256, sources_sha256, versions_examined)
               VALUES('aplicada', ?, ?, ?, ?)""",
            (RULES_VERSION, _sha256(specs_path), _sha256(sources_path), len(rows)))
        run_id = int(cur.lastrowid)
        stamp = today.isoformat()
        for r in rows:
            if r.get("transmission_original") is None:
                conn.execute("UPDATE vehicle_versions SET transmission_original=? WHERE id=?",
                             (r["transmission"], r["id"]))
                conn.execute(
                    """INSERT INTO transmission_changes(run_id, version_id, field, old_value, new_value, rule, evidence)
                       VALUES(?,?,?,?,?,?,?)""",
                    (run_id, r["id"], "transmission_original", None, r["transmission"], "conservar-original",
                     "Valor previo a la normalización, conservado sin cambios."))
        for vid, new_name in renames.items():
            conn.execute("UPDATE vehicle_versions SET version_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (new_name, vid))
        for r in rows:
            if r["id"] in renames:
                r["version_name"] = renames[r["id"]]
        if changed_ids:
            for vid in sorted(changed_ids):
                t = targets[vid].values()
                conn.execute(
                    f"""UPDATE vehicle_versions SET {', '.join(f'{k}=?' for k in TRACKED_FIELDS)},
                        transmission_checked_at=?, canonical_key=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (*[t[k] for k in TRACKED_FIELDS], stamp, vid))
            conn.executemany(
                """INSERT INTO transmission_changes(run_id, version_id, field, old_value, new_value, rule, evidence, source_ids)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(run_id, c["version_id"], c["field"], None if c["old_value"] is None else str(c["old_value"]),
                  None if c["new_value"] is None else str(c["new_value"]), c["rule"], c["evidence"], c["source_ids"])
                 for c in changes])
            # Clave lógica recalculada solo para las filas cambiadas (las demás no cambian).
            for r in rows:
                if r["id"] in changed_ids:
                    key = vehicle_canonical_key(r["brand"], r["model"], r["model_year"], r.get("generation"),
                                                r["version_name"], r["engine_label"], r["fuel"],
                                                targets[r["id"]].transmission)
                    conn.execute("UPDATE vehicle_versions SET canonical_key=? WHERE id=?", (key, r["id"]))

    summary = summarize(rows, targets, changes, changed_ids, originals_set, specs, sources)
    integrity = verify_price_links(conn) if with_integrity else None
    if apply:
        conn.execute(
            """UPDATE transmission_audit_runs SET finished_at=CURRENT_TIMESTAMP, versions_changed=?, summary=?
               WHERE id=?""",
            (len(changed_ids), json.dumps(summary, ensure_ascii=False, default=str), run_id))
        conn.commit()
    return {"run_id": run_id, "applied": apply, "rows": rows, "targets": targets, "changes": changes,
            "changed_ids": changed_ids, "duplicates": dups, "summary": summary, "integrity": integrity,
            "specs": specs, "sources": sources}


def summarize(rows, targets, changes, changed_ids, originals_set, specs, sources) -> dict:
    status = Counter(t.transmission_status for t in targets.values())
    category = Counter(t.transmission for t in targets.values())
    subtypes = Counter(t.transmission_subtype for t in targets.values() if t.transmission_subtype)
    rules = Counter(t.transmission_rule.split(":")[0] if t.transmission_rule.startswith("F:") else t.transmission_rule
                    for t in targets.values())
    issues = Counter(i.split(":")[0] for t in targets.values() for i in t.issues)
    before = Counter((r.get("transmission_original") or r["transmission"]) for r in rows)
    by_status_category = Counter((t.transmission, t.transmission_status) for t in targets.values())
    spec_hits = Counter(t.transmission_rule[2:] for t in targets.values() if t.transmission_rule.startswith("F:"))
    return {
        "rules_version": RULES_VERSION,
        "versions_examined": len(rows),
        "versions_changed": len(changed_ids),
        "field_changes": len(changes),
        "originals_preserved_now": originals_set,
        "before_values": dict(before.most_common()),
        "categories": dict(category),
        "statuses": dict(status),
        "category_by_status": {f"{c} / {s}": n for (c, s), n in sorted(by_status_category.items())},
        "subtypes": dict(subtypes.most_common()),
        "rules": dict(rules.most_common()),
        "issues": dict(issues.most_common()),
        "specs_loaded": len(specs),
        "specs_used": len(spec_hits),
        "specs_unused": sorted(s.spec_id for s in specs if s.spec_id not in spec_hits),
        "sources_loaded": len(sources),
    }


def needs_audit(conn) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vehicle_versions)")}
    if "transmission_status" not in cols:
        return False
    return conn.execute("SELECT 1 FROM vehicle_versions WHERE transmission_status IS NULL LIMIT 1").fetchone() is not None
