"""Auditoría reproducible de transmisiones del catálogo de AutoValor Pro Colombia.

Uso (desde la carpeta del proyecto):

    python scripts/auditar_transmisiones.py                 # simulación: no cambia la base
    python scripts/auditar_transmisiones.py --aplicar       # copia de seguridad + aplica + informes
    python scripts/auditar_transmisiones.py --etiqueta previo   # nombra los informes (previo/final)

Examina todas las versiones, normaliza la transmisión (categoría + subtipo + estado),
compara con las fichas técnicas de ``data/fichas_tecnicas``, detecta duplicados,
conflictos y vínculos de precio rotos, y escribe los informes en
``reports/auditoria_transmisiones``. Ejecutarla otra vez no duplica datos ni cambios.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import transmissions as T  # noqa: E402
from db import DEFAULT_DB, connect_db, init_db  # noqa: E402
from transmission_audit import run_audit, selector_inventory, RULES_VERSION  # noqa: E402

OUT_DIR = ROOT / "reports" / "auditoria_transmisiones"


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _backup(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_path.parent / "backups" / f"autovalor_pre_auditoria_transmisiones_{stamp}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    return dst


def inventory(conn, result: dict) -> dict:
    """Clasifica los hallazgos en las 12 familias de error pedidas en la auditoría."""
    rows, targets = result["rows"], result["targets"]
    by_id = {r["id"]: r for r in rows}
    sel = selector_inventory(rows, targets)

    def orig(r):
        return r.get("transmission_original") or r["transmission"]

    inv: dict[str, dict] = {}
    # 1. Mecánica y automática existentes, pero el selector no mostraba la mecánica
    hidden_manual = []
    for s in sel:
        before = [T.classify_text(x) for x in s["transmissions_before"].split(" | ")]
        had_manual = any(b and b["category"] == T.CATEGORY_MANUAL for b in before)
        after = s["after_detail"]
        if T.CATEGORY_MANUAL in after and T.CATEGORY_AUTOMATIC in after and not had_manual:
            hidden_manual.append(s)
    inv["01_mecanica_y_automatica_pero_solo_se_veia_una"] = {
        "count": len(hidden_manual), "unit": "combinaciones línea-año-motor",
        "examples": [f"{s['brand']} {s['model']} {s['model_year']} {s['engine_option']}: antes «{s['transmissions_before']}»"
                     for s in hidden_manual[:8]]}
    # 2 y 3. Cambios de categoría confirmada
    a2m = [r for r in rows if (T.classify_text(orig(r)) or {}).get("category") == T.CATEGORY_AUTOMATIC
           and targets[r["id"]].transmission == T.CATEGORY_MANUAL]
    m2a = [r for r in rows if (T.classify_text(orig(r)) or {}).get("category") == T.CATEGORY_MANUAL
           and targets[r["id"]].transmission == T.CATEGORY_AUTOMATIC]
    inv["02_automaticas_registradas_como_mecanicas"] = {"count": len(m2a), "unit": "versiones",
        "examples": [f"{r['brand']} {r['model']} {r['model_year']} {r['version_name']}" for r in m2a[:8]]}
    inv["03_mecanicas_registradas_como_automaticas"] = {"count": len(a2m), "unit": "versiones",
        "examples": [f"{r['brand']} {r['model']} {r['model_year']} {r['version_name']}" for r in a2m[:8]]}
    # 4. «No especificada» identificable
    unspec = [r for r in rows if T.classify_text(orig(r)) is None]
    ident = [r for r in unspec if targets[r["id"]].transmission in T.CONFIRMED_CATEGORIES
             and targets[r["id"]].transmission_status != T.STATUS_CONFLICT]
    by_status = Counter(targets[r["id"]].transmission_status for r in ident)
    inv["04_no_especificada_pero_identificable"] = {"count": len(ident), "unit": "versiones",
        "detail": dict(by_status), "of_total_unspecified": len(unspec)}
    # 5. Duplicados por escritura de caja
    exact = result["duplicates"]["exact"]
    inv["05_duplicados_por_escritura_de_la_caja"] = {"count": len(exact), "unit": "grupos",
        "examples": [", ".join(map(str, v)) for v in list(exact.values())[:8]]}
    # 6. Posibles fusiones incorrectas: varios precios de guía para la misma versión y año
    merged = conn.execute(
        """SELECT version_id, model_year, COUNT(*) n, COUNT(DISTINCT price_cop) d FROM price_references
           WHERE price_type='guia_comercial' GROUP BY version_id, model_year HAVING COUNT(*) > 1""").fetchall()
    link_problems = (result.get("integrity") or {}).get("problems", [])
    inv["06_versiones_distintas_fusionadas"] = {"count": len(merged) + len(link_problems), "unit": "casos",
        "detail": {"versiones_con_varios_precios_mismo_anio": len(merged), "precios_ligados_a_otra_descripcion": len(link_problems)},
        "examples": [f"versión {m['version_id']} año {m['model_year']}: {m['n']} precios" for m in merged[:8]]}
    # 7. Motor/caja incompatibles con la ficha
    conflicts = [r for r in rows if targets[r["id"]].transmission_status == T.STATUS_CONFLICT]
    inv["07_caja_incompatible_con_la_ficha"] = {"count": len(conflicts), "unit": "versiones",
        "examples": [f"{r['brand']} {r['model']} {r['model_year']} {r['version_name']}: {targets[r['id']].transmission_evidence[:140]}"
                     for r in conflicts[:8]]}
    # 8. Sin información técnica suficiente
    no_engine = [r for r in rows if not any(ch.isdigit() for ch in str(r["engine_label"] or ""))]
    pending = [r for r in rows if targets[r["id"]].transmission_status == T.STATUS_PENDING]
    inv["08_versiones_sin_informacion_tecnica_suficiente"] = {"count": len({r['id'] for r in no_engine} | {r['id'] for r in pending}),
        "unit": "versiones", "detail": {"sin_cilindrada_publicada": len(no_engine), "caja_sin_confirmar": len(pending)}}
    # 9. Mensaje «caja no verificada»
    generic_before = [r for r in rows if r["verification_status"] != "verificado"]
    verified_now = [r for r in generic_before if targets[r["id"]].transmission_status == T.STATUS_VERIFIED]
    inv["09_registros_con_mensaje_caja_no_verificada"] = {"count": len(generic_before), "unit": "versiones",
        "detail": {"antes_aviso_generico": len(generic_before),
                   "ahora_caja_verificada_sin_aviso_de_caja": len(verified_now),
                   "ahora_aviso_especifico_segun_estado": len(generic_before) - len(verified_now)}}
    # 10. Asociación transmisión-año-motor-precio
    # El contexto «precio de nuevo de la misma versión» se buscaba solo por nombre: una versión mecánica
    # podía mostrar el precio de la automática homónima. Se cuentan esas asociaciones con las cajas auditadas.
    new_priced = conn.execute(
        """SELECT DISTINCT v.id, v.model_id, lower(v.version_name) FROM vehicle_versions v
           JOIN price_references p ON p.version_id=v.id AND p.price_type='precio_nuevo'""").fetchall()
    by_name = defaultdict(set)
    for vid, mid, name in new_priced:
        by_name[(mid, name)].add(targets[vid].transmission if vid in targets else None)
    new_mismatch = 0
    for r in rows:
        if r.get("data_origin") != "revista_motor_usados":
            continue
        cats = by_name.get((r["model_id"], (r["version_name"] or "").lower()))
        if cats and any(c != targets[r["id"]].transmission for c in cats):
            new_mismatch += 1
    inv["10_asociacion_caja_anio_motor_precio"] = {"count": len(link_problems) + int(new_mismatch), "unit": "casos",
        "detail": {"precios_de_guia_mal_ligados": len(link_problems),
                   "versiones_usadas_que_tomaban_precio_nuevo_de_otra_caja": int(new_mismatch)}}
    # 11. Selectores que no mostraban todas las opciones (motor partido en varias opciones)
    split = [s for s in sel if s["engine_options_before"] > 1]
    inv["11_selectores_incompletos_motor_partido"] = {"count": len(split), "unit": "combinaciones línea-año-motor",
        "examples": [f"{s['brand']} {s['model']} {s['model_year']}: {s['engine_labels_before']}" for s in split[:8]]}
    # 12. Opciones de caja que no eran una configuración válida
    invalid = [s for s in sel if any(T.classify_text(x) is None for x in s["transmissions_before"].split(" | "))]
    inv["12_opciones_de_caja_no_validas_en_el_formulario"] = {"count": len(invalid), "unit": "combinaciones línea-año-motor",
        "examples": [f"{s['brand']} {s['model']} {s['model_year']} {s['engine_option']}: «{s['transmissions_before']}»" for s in invalid[:8]]}
    return {"inventory": inv, "selectors": sel, "merged": merged}


def write_reports(conn, result: dict, label: str, out_dir: Path) -> dict:
    rows, targets = result["rows"], result["targets"]
    inv = inventory(conn, result)
    prefix = f"{label}_" if label else ""
    by_id = {r["id"]: r for r in rows}

    def orig(r):
        return r.get("transmission_original") or r["transmission"]

    # Detalle por versión (todas)
    _write_csv(out_dir / f"{prefix}auditoria_por_version.csv",
               ["version_id", "marca", "linea", "anio", "version", "motor", "combustible", "descripcion_fuente",
                "origen", "categoria_fuente", "transmision_original", "categoria", "subtipo", "marchas", "detalle",
                "estado", "regla", "fuentes", "evidencia", "observaciones"],
               [[r["id"], r["brand"], r["model"], r["model_year"], r["version_name"], r["engine_label"], r["fuel"] or "",
                 r.get("raw_description") or "", r.get("data_origin") or "catalogo", r.get("source_category") or "",
                 orig(r), t.transmission, t.transmission_subtype or "", t.transmission_gears or "",
                 t.transmission_detail or "", t.transmission_status, t.transmission_rule,
                 t.transmission_source_ids or "", t.transmission_evidence, "; ".join(t.issues)]
                for r in rows for t in [targets[r["id"]]]])
    # Pendientes (sin confirmar) y confirmadas sin ficha técnica
    pend = [r for r in rows if targets[r["id"]].transmission_status in (T.STATUS_PENDING, T.STATUS_CONFLICT)]
    _write_csv(out_dir / f"{prefix}pendientes.csv",
               ["version_id", "marca", "linea", "anio", "version", "motor", "descripcion_fuente", "origen",
                "estado", "categoria_actual", "motivo", "precios_ligados"],
               [[r["id"], r["brand"], r["model"], r["model_year"], r["version_name"], r["engine_label"],
                 r.get("raw_description") or "", r.get("data_origin") or "catalogo",
                 targets[r["id"]].transmission_status, targets[r["id"]].transmission,
                 targets[r["id"]].transmission_evidence,
                 conn.execute("SELECT COUNT(*) FROM price_references WHERE version_id=?", (r["id"],)).fetchone()[0]]
                for r in pend])
    nover = [r for r in rows if targets[r["id"]].transmission_status in (T.STATUS_DOCUMENTED, T.STATUS_INFERRED, T.STATUS_RULE)]
    _write_csv(out_dir / f"{prefix}sin_ficha_tecnica.csv",
               ["version_id", "marca", "linea", "anio", "version", "descripcion_fuente", "categoria", "subtipo", "estado", "regla"],
               [[r["id"], r["brand"], r["model"], r["model_year"], r["version_name"], r.get("raw_description") or "",
                 targets[r["id"]].transmission, targets[r["id"]].transmission_subtype or "",
                 targets[r["id"]].transmission_status, targets[r["id"]].transmission_rule] for r in nover])
    # Cambios (propuestos en simulación, aplicados en modo aplicado)
    _write_csv(out_dir / f"{prefix}{'cambios_aplicados' if result['applied'] else 'cambios_propuestos'}.csv",
               ["version_id", "marca", "linea", "anio", "version", "campo", "valor_anterior", "valor_nuevo", "regla", "fuentes"],
               [[c["version_id"], by_id[c["version_id"]]["brand"], by_id[c["version_id"]]["model"],
                 by_id[c["version_id"]]["model_year"], by_id[c["version_id"]]["version_name"], c["field"],
                 "" if c["old_value"] is None else c["old_value"], "" if c["new_value"] is None else c["new_value"],
                 c["rule"], c["source_ids"] or ""] for c in result["changes"]
                if c["field"] not in ("transmission_evidence",)])
    # Duplicados
    dup_rows = []
    for key, ids in result["duplicates"]["exact"].items():
        for vid in ids:
            r = by_id[vid]
            dup_rows.append(["exacto (solo cambia la escritura de la caja)", key, vid, r["brand"], r["model"],
                             r["model_year"], r["version_name"], r["engine_label"], orig(r), r.get("data_origin") or "catalogo"])
    for a, b in result["duplicates"]["possible"]:
        for r in (a, b):
            dup_rows.append(["posible (misma versión y caja, otra carga o descripción)", f"{a['id']}-{b['id']}", r["id"],
                             r["brand"], r["model"], r["model_year"], r["version_name"], r["engine_label"], orig(r),
                             r.get("data_origin") or "catalogo"])
    _write_csv(out_dir / f"{prefix}duplicados.csv",
               ["tipo", "grupo", "version_id", "marca", "linea", "anio", "version", "motor", "transmision_original", "origen"],
               dup_rows)
    # Selectores
    _write_csv(out_dir / f"{prefix}selectores_linea_anio_motor.csv",
               ["marca", "linea", "anio", "opcion_motor", "versiones", "opciones_de_motor_antes", "etiquetas_antes",
                "cajas_antes", "cajas_despues"],
               [[s["brand"], s["model"], s["model_year"], s["engine_option"], s["versions"], s["engine_options_before"],
                 s["engine_labels_before"], s["transmissions_before"], s["transmissions_after"]]
                for s in sorted(inv["selectors"], key=lambda x: (x["brand"], x["model"], x["model_year"], x["engine_option"]))])
    # Vínculos de precio
    integ = result.get("integrity") or {"checked": 0, "problems": []}
    _write_csv(out_dir / f"{prefix}vinculos_precio_version.csv",
               ["price_id", "version_id", "source_ref", "tipo", "detalle"],
               [[p["price_id"], p["version_id"], p["source_ref"], "problema", p["problema"]] for p in integ["problems"]]
               + [[v["price_id"], v["version_id"], v["source_ref"], "variante_equivalente", v["detalle"]]
                  for v in integ.get("variants", [])])

    # Fuentes técnicas: qué dice cada una y cuántas fichas y versiones respalda
    spec_count, version_count = Counter(), Counter()
    for sp in result["specs"]:
        for sid in sp.source_ids.split(";"):
            spec_count[sid] += 1
    for r in rows:
        for sid in (targets[r["id"]].transmission_source_ids or "").split(";"):
            if sid:
                version_count[sid] += 1
    src_lines = ["# Fuentes técnicas de la auditoría de transmisiones", "",
                 "Fuentes consultadas para verificar o inferir la caja de cambios de las versiones del catálogo "
                 "(mercado colombiano). Se conservan en `data/fichas_tecnicas/fuentes_tecnicas.csv` y en la tabla "
                 "`technical_sources` de la base. Cada ficha (`transmisiones_verificadas.csv`) cita sus fuentes y resume "
                 "lo que dicen. Información de uso interno: la página pública no muestra fuentes.", "",
                 f"Fuentes: {len(result['sources'])} · Fichas: {len(result['specs'])}.", "",
                 "| Id | Tipo | Publicación | Fecha | Fichas | Versiones | Qué documenta |", "|---|---|---|---|---:|---:|---|"]
    for sid, src in sorted(result["sources"].items()):
        title = src["title"].replace("|", "/")
        src_lines.append(f"| {sid} | {src['kind']} | [{title}]({src['url']}) — {src['publisher']} | "
                         f"{src['published_date'] or 's. f.'} | {spec_count[sid]} | {version_count[sid]} | "
                         f"{src['notes'].replace('|', '/')} |")
    src_lines += ["", f"Además, {version_count['ficha-de-la-version']} versiones del catálogo inicial se verifican con la ficha "
                  "técnica oficial que ya tenían registrada (campo `technical_source_url`)."]
    (out_dir / f"{prefix}fuentes_tecnicas.md").write_text("\n".join(src_lines), encoding="utf-8")

    s = result["summary"]
    summary = {"etiqueta": label, "fecha": date.today().isoformat(), "modo": "aplicada" if result["applied"] else "simulacion",
               "run_id": result["run_id"], "resumen": s, "inventario": inv["inventory"],
               "vinculos_precio": {"revisados": integ["checked"], "problemas": len(integ["problems"]),
                                   "variantes_equivalentes": len(integ.get("variants", []))}}
    (out_dir / f"{prefix}resumen.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = [f"# Auditoría de transmisiones — {label or 'ejecución'} ({summary['modo']})", "",
          f"Fecha: {summary['fecha']} · Reglas {RULES_VERSION} · Versiones examinadas: **{s['versions_examined']}** (todas las del catálogo).", "",
          "## Resultado por categoría y estado", "", "| Categoría / estado | Versiones |", "|---|---:|"]
    md += [f"| {k} | {v} |" for k, v in s["category_by_status"].items()]
    md += ["", "## Subtipos identificados", "", "| Subtipo | Versiones |", "|---|---:|"]
    md += [f"| {k} | {v} |" for k, v in s["subtypes"].items()]
    md += ["", "## Reglas aplicadas", "", "| Regla | Versiones |", "|---|---:|"]
    md += [f"| {k} | {v} |" for k, v in s["rules"].items()]
    md += ["", "## Inventario de inconsistencias (12 familias)", "", "| Familia | Cantidad | Unidad | Detalle |", "|---|---:|---|---|"]
    for k, v in inv["inventory"].items():
        det = v.get("detail") or ""
        md.append(f"| {k} | {v['count']} | {v['unit']} | {json.dumps(det, ensure_ascii=False) if det else ''} |")
    md += ["", "### Ejemplos", ""]
    for k, v in inv["inventory"].items():
        for e in v.get("examples", [])[:5]:
            md.append(f"- {k}: {e}")
    md += ["", f"Vínculos precio-versión revisados: {integ['checked']}; problemas: {len(integ['problems'])}; "
                   f"variantes de escritura equivalentes (no son errores): {len(integ.get('variants', []))}.",
           f"Cambios de campo {'aplicados' if result['applied'] else 'propuestos'}: {s['field_changes']} en {s['versions_changed']} versiones.",
           f"Fichas técnicas cargadas: {s['specs_loaded']} (usadas: {s['specs_used']}); fuentes: {s['sources_loaded']}.", ""]
    (out_dir / f"{prefix}resumen.md").write_text("\n".join(md), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--aplicar", action="store_true", help="aplica los cambios (por defecto solo simula)")
    ap.add_argument("--sin-copia", action="store_true", help="no crear copia de seguridad antes de aplicar")
    ap.add_argument("--etiqueta", default="", help="prefijo de los informes (p. ej. previo o final)")
    ap.add_argument("--salida", default=str(OUT_DIR))
    args = ap.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No existe la base {db_path}", file=sys.stderr)
        return 2
    tmpdir = None
    if args.aplicar:
        if not args.sin_copia:
            print("Copia de seguridad:", _backup(db_path))
        work_db = db_path
    else:
        # La simulación trabaja sobre una copia temporal: la base real no se toca en absoluto
        # (ni siquiera para añadir las columnas nuevas de la migración).
        import tempfile
        tmpdir = tempfile.TemporaryDirectory(prefix="auditoria_cajas_")
        work_db = Path(tmpdir.name) / "simulacion.db"
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(work_db)
        src.backup(dst)
        dst.close()
        src.close()
    init_db(work_db, seed=False, auto_audit=False)  # columnas y tablas de la migración 003
    conn = connect_db(work_db)
    try:
        result = run_audit(conn, apply=args.aplicar)
        summary = write_reports(conn, result, args.etiqueta, Path(args.salida))
    finally:
        conn.close()
        if tmpdir is not None:
            tmpdir.cleanup()
    s = summary["resumen"]
    print(json.dumps({"modo": summary["modo"], "versiones": s["versions_examined"], "cambiadas": s["versions_changed"],
                      "categorias": s["categories"], "estados": s["statuses"], "vinculos_precio": summary["vinculos_precio"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
