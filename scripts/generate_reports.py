"""Genera informes de cobertura y calidad directamente desde SQLite."""
from __future__ import annotations

import csv
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DATE = os.environ.get('AUTOVALOR_REPORT_DATE') or date.today().isoformat()
sys.path.insert(0, str(ROOT))

from catalog_utils import find_potential_duplicate_pairs
from db import DEFAULT_DB, connect_db, init_db


def write_csv(path: Path, headers: list[str], rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def main():
    init_db(DEFAULT_DB, seed=False)
    conn = connect_db(DEFAULT_DB)
    out = ROOT / "reports"
    out.mkdir(parents=True, exist_ok=True)
    try:
        count = lambda sql: conn.execute(sql).fetchone()[0]
        stats = {
            "brands": count("SELECT COUNT(*) FROM brands"),
            "models": count("SELECT COUNT(*) FROM models"),
            "versions": count("SELECT COUNT(*) FROM vehicle_versions"),
            "price_references": count("SELECT COUNT(*) FROM price_references"),
            "technical_verified": count("SELECT COUNT(*) FROM vehicle_versions WHERE verification_status='verificado'"),
            "technical_partial": count("SELECT COUNT(*) FROM vehicle_versions WHERE verification_status='parcial'"),
            "technical_pending": count("SELECT COUNT(*) FROM vehicle_versions WHERE verification_status='pendiente'"),
            "versions_with_any_price": count("SELECT COUNT(DISTINCT version_id) FROM price_references"),
            "versions_without_any_price": count("SELECT COUNT(*) FROM vehicle_versions v WHERE NOT EXISTS (SELECT 1 FROM price_references p WHERE p.version_id=v.id)"),
            "used_market_prices": count("SELECT COUNT(*) FROM price_references WHERE price_type IN ('precio_solicitado','transaccion_documentada','guia_comercial')"),
            "new_vehicle_prices": count("SELECT COUNT(*) FROM price_references WHERE price_type='precio_nuevo'"),
            "transactions_verified": count("SELECT COUNT(*) FROM price_references WHERE price_type='transaccion_documentada' AND verification_status='verificado'"),
            "min_year": conn.execute("SELECT MIN(model_year) FROM vehicle_versions").fetchone()[0],
            "max_year": conn.execute("SELECT MAX(model_year) FROM vehicle_versions").fetchone()[0],
            "versions_with_3_recent_verified_used_prices": count("""SELECT COUNT(*) FROM (
                SELECT p.version_id FROM price_references p
                WHERE p.price_type='precio_solicitado' AND p.verification_status='verificado'
                AND julianday('now')-julianday(p.observed_at) BETWEEN 0 AND 90
                GROUP BY p.version_id HAVING COUNT(*)>=3)"""),
            "versions_with_5_recent_verified_used_prices": count("""SELECT COUNT(*) FROM (
                SELECT p.version_id FROM price_references p
                WHERE p.price_type='precio_solicitado' AND p.verification_status='verificado'
                AND julianday('now')-julianday(p.observed_at) BETWEEN 0 AND 90
                GROUP BY p.version_id HAVING COUNT(*)>=5)"""),
            "versions_pending_or_partial": count("SELECT COUNT(*) FROM vehicle_versions WHERE verification_status IN ('pendiente','parcial')"),
            "versions_missing_technical_source_or_date": count("SELECT COUNT(*) FROM vehicle_versions WHERE technical_source_url IS NULL OR technical_source_checked_at IS NULL"),
        }

        by_brand = conn.execute(
            """SELECT b.name, COUNT(DISTINCT m.id) AS models, COUNT(DISTINCT v.id) AS versions,
                      COUNT(DISTINCT CASE WHEN v.verification_status='verificado' THEN v.id END) AS verified_versions,
                      COUNT(DISTINCT CASE WHEN v.verification_status IN ('pendiente','parcial') THEN v.id END) AS pending_versions,
                      COUNT(DISTINCT p.id) AS price_references,
                      COUNT(DISTINCT CASE WHEN p.price_type='precio_nuevo' THEN p.id END) AS new_price_refs,
                      COUNT(DISTINCT CASE WHEN p.price_type IN ('precio_solicitado','transaccion_documentada','guia_comercial') THEN p.id END) AS used_price_refs
               FROM brands b LEFT JOIN models m ON m.brand_id=b.id
               LEFT JOIN vehicle_versions v ON v.model_id=m.id
               LEFT JOIN price_references p ON p.version_id=v.id
               GROUP BY b.id ORDER BY b.name"""
        ).fetchall()
        write_csv(
            out / "cobertura_por_marca.csv",
            ["marca","líneas_modelos","versiones","versiones_verificadas","versiones_pendientes_o_parciales","referencias_precio_totales","referencias_precio_nuevo","referencias_mercado_usado"],
            [[r["name"],r["models"],r["versions"],r["verified_versions"],r["pending_versions"],r["price_references"],r["new_price_refs"],r["used_price_refs"]] for r in by_brand],
        )

        by_version = conn.execute(
            """SELECT v.id,b.name AS brand,m.name AS model,v.model_year,v.generation,v.version_name,
                      v.engine_label,v.displacement_cc,v.fuel,v.horsepower,v.transmission,v.transmission_subtype,
                      v.transmission_gears,v.transmission_status,v.transmission_original,v.traction,v.body_type,
                      v.doors,v.passengers,v.verification_status,v.technical_source_name,v.technical_source_url,
                      v.technical_source_checked_at,v.notes,
                      COUNT(DISTINCT p.id) AS price_count,
                      COUNT(DISTINCT CASE WHEN p.price_type='precio_nuevo' THEN p.id END) AS new_price_count,
                      COUNT(DISTINCT CASE WHEN p.price_type='precio_solicitado' THEN p.id END) AS used_listing_count,
                      COUNT(DISTINCT CASE WHEN p.price_type='precio_solicitado' AND p.verification_status='verificado' THEN p.id END) AS verified_used_price_count
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               LEFT JOIN price_references p ON p.version_id=v.id
               GROUP BY v.id ORDER BY b.name,m.name,v.model_year,v.version_name"""
        ).fetchall()
        version_headers = [
            "id","marca","línea","año_modelo","generación","versión","motor","cilindraje_cc","combustible_energía",
            "potencia_hp","transmisión","subtipo_caja","marchas","estado_caja","transmisión_original","tracción",
            "carrocería","puertas","pasajeros","estado_verificación",
            "fuente_técnica","url_fuente_técnica","fecha_consulta_técnica","notas","referencias_precio_totales",
            "precios_nuevo","anuncios_usados","anuncios_usados_verificados",
        ]
        write_csv(out / "inventario_versiones.csv", version_headers, [[r[k] for k in [
            "id","brand","model","model_year","generation","version_name","engine_label","displacement_cc","fuel",
            "horsepower","transmission","transmission_subtype","transmission_gears","transmission_status",
            "transmission_original","traction","body_type","doors","passengers","verification_status",
            "technical_source_name","technical_source_url","technical_source_checked_at","notes","price_count",
            "new_price_count","used_listing_count","verified_used_price_count"
        ]] for r in by_version])

        no_prices = [r for r in by_version if r["price_count"] == 0]
        write_csv(out / "versiones_sin_precios.csv",
                  ["id","marca","línea","año_modelo","versión","motor","transmisión","estado","fuente_técnica","url_fuente"],
                  [[r["id"],r["brand"],r["model"],r["model_year"],r["version_name"],r["engine_label"],r["transmission"],r["verification_status"],r["technical_source_name"],r["technical_source_url"]] for r in no_prices])

        pending = [r for r in by_version if r["verification_status"] in {"pendiente","parcial"} or not r["technical_source_url"] or not r["technical_source_checked_at"]]
        write_csv(out / "datos_pendientes_verificacion.csv",
                  ["id","marca","línea","año_modelo","versión","motor","combustible","transmisión","estado","fuente_técnica","url_fuente","fecha_consulta","notas"],
                  [[r["id"],r["brand"],r["model"],r["model_year"],r["version_name"],r["engine_label"],r["fuel"],r["transmission"],r["verification_status"],r["technical_source_name"],r["technical_source_url"],r["technical_source_checked_at"],r["notes"]] for r in pending])

        prices = conn.execute(
            """SELECT p.id,b.name AS brand,m.name AS model,v.model_year,v.version_name,v.engine_label,v.transmission,
                      p.price_cop,p.price_type,p.source_url,s.name AS source_name,p.observed_at,p.mileage_km,p.location,
                      p.verification_status,p.notes
               FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id
               JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
               JOIN sources s ON s.id=p.source_id ORDER BY p.observed_at DESC,p.id"""
        ).fetchall()
        write_csv(out / "referencias_precios.csv",
                  ["id","marca","línea","año_modelo","versión","motor","transmisión","precio_cop","tipo_precio","fuente","url_fuente","fecha_observación","kilometraje_km","ubicación","estado_verificación","notas"],
                  [[r[k] for k in ["id","brand","model","model_year","version_name","engine_label","transmission","price_cop","price_type","source_name","source_url","observed_at","mileage_km","location","verification_status","notes"]] for r in prices])

        pairs = find_potential_duplicate_pairs(conn)
        write_csv(out / "posibles_duplicados.csv",
                  ["id_a","vehiculo_a","generacion_a","id_b","vehiculo_b","generacion_b","motivo"],
                  [[p["id_a"],p["vehicle_a"],p["generation_a"],p["id_b"],p["vehicle_b"],p["generation_b"],p["reason"]] for p in pairs])

        lines = [
            "# Informe de cobertura de AutoValor Pro Colombia", "",
            f"Fecha de generación: {REPORT_DATE}", "",
            "## Conteos calculados desde SQLite", "",
            "| Métrica | Resultado |", "|---|---:|",
        ]
        labels = {
            "brands":"Marcas únicas","models":"Líneas/modelos únicos","versions":"Versiones registradas",
            "price_references":"Referencias de precio totales","technical_verified":"Versiones con estado técnico verificado",
            "technical_partial":"Versiones con estado técnico parcial","technical_pending":"Versiones pendientes",
            "versions_with_any_price":"Versiones con alguna referencia económica","versions_without_any_price":"Versiones sin ninguna referencia económica",
            "used_market_prices":"Anuncios/precios solicitados de usados (todos los estados)","new_vehicle_prices":"Precios sugeridos de vehículos nuevos",
            "transactions_verified":"Transacciones documentadas verificadas","min_year":"Año modelo mínimo en la base",
            "max_year":"Año modelo máximo en la base","versions_with_3_recent_verified_used_prices":"Versiones con ≥3 anuncios usados verificados recientes",
            "versions_with_5_recent_verified_used_prices":"Versiones con ≥5 anuncios usados verificados recientes",
            "versions_pending_or_partial":"Versiones pendientes o parciales","versions_missing_technical_source_or_date":"Versiones sin URL técnica o fecha de consulta técnica",
        }
        lines += [f"| {labels[k]} | {v if v is not None else '—'} |" for k,v in stats.items()]
        lines += [
            "", "## Cobertura por marca", "",
            "| Marca | Líneas | Versiones | Verificadas | Pendientes/parciales | Precios nuevos | Referencias usados |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {r['name']} | {r['models']} | {r['versions']} | {r['verified_versions']} | {r['pending_versions']} | {r['new_price_refs']} | {r['used_price_refs']} |"
            for r in by_brand
        ]
        lines += [
            "", "## Interpretación y límites", "",
            "- Los conteos anteriores se calculan directamente de SQLite al ejecutar este script; no son cifras estimadas.",
            "- El catálogo ampliado contiene registros documentados por fichas oficiales de Chevrolet, Mazda, Toyota y Ford, además de una identificación parcial de Kia Picanto basada en anuncios públicos de Kia Usados.",
            "- Las fichas técnicas no demuestran por sí solas que una versión esté disponible en todos los años intermedios. Se conservaron los años modelo indicados por la documentación; no se extrapolaron configuraciones a otros años.",
            "- Algunas versiones permanecen en estado `parcial` porque la fuente consultada no confirma todos los campos. En particular, el PDF del Spark EUV presenta una discrepancia entre la identificación del documento y el año modelo de la portada; Trend y Titanium de Territory requieren confirmar la transmisión.",
            "- La ampliación añadió dos anuncios públicos de Kia Picanto Zenith 2026 en estado no verificado y cuatro precios sugeridos de vehículos nuevos. Ninguna transacción efectiva se documentó.",
            "- Los precios sugeridos caducados se conservan como historial y no participan en la estimación de usados. El calculador sigue usando solo referencias recientes elegibles de `precio_solicitado` para la versión exacta.",
            "- No se afirma que este catálogo incluya todos los vehículos comercializados en Colombia. Los vehículos históricos y años no documentados siguen siendo una brecha de cobertura.",
            "- Los registros sin suficientes referencias de precios no generan una valoración confiable; se muestran como no estimables.",
            "", "## Archivos de auditoría", "",
            "- `inventario_versiones.csv`: detalle de cada versión y su fuente técnica.",
            "- `cobertura_por_marca.csv`: cobertura agregada por marca.",
            "- `versiones_sin_precios.csv`: versiones sin referencias económicas.",
            "- `datos_pendientes_verificacion.csv`: versiones con campos o fuentes pendientes.",
            "- `referencias_precios.csv`: referencias económicas con tipo, URL, fecha, estado y notas.",
            "- `posibles_duplicados.csv`: coincidencias que requieren revisión humana.",
            "- `INFORME_IMPORTACION_CATALOGO.csv`: candidatos añadidos, duplicados y referencias económicas importadas.",
            "",
        ]
        (out / "INFORME_COBERTURA_CATALOGO.md").write_text("\n".join(lines), encoding="utf-8")

        # Reporte de fuentes y referencias económicas con separación explícita por tipo.
        source_rows = conn.execute(
            """SELECT s.name,s.domain,s.source_type,s.base_url,s.usage_notes,s.last_checked_at,
                      COUNT(DISTINCT p.id) AS price_refs,
                      COUNT(DISTINCT v.id) AS versions
               FROM sources s
               LEFT JOIN price_references p ON p.source_id=s.id
               LEFT JOIN vehicle_versions v ON v.technical_source_name=s.name
               GROUP BY s.id ORDER BY s.name"""
        ).fetchall()
        source_lines = [
            "# Fuentes de catálogo y precios", "",
            f"Fecha de generación: {REPORT_DATE}", "",
            "## Fuentes técnicas", "",
            "| Fuente | Tipo | Dominio | Última consulta registrada | Versiones asociadas | URL |",
            "|---|---|---|---|---:|---|",
        ]
        for r in source_rows:
            if r["versions"] or r["source_type"] in {"fichas_tecnicas","ficha_tecnica","catalogo_tecnico"}:
                source_lines.append(f"| {r['name']} | {r['source_type']} | {r['domain'] or '—'} | {r['last_checked_at'] or '—'} | {r['versions']} | {r['base_url'] or '—'} |")
        source_lines += [
            "", "## Referencias de precio", "",
            f"- Referencias totales en SQLite: **{stats['price_references']}**.",
            f"- Referencias de usados/guías: **{stats['used_market_prices']}**.",
            f"- Precios sugeridos de vehículos nuevos: **{stats['new_vehicle_prices']}**.",
            f"- Transacciones documentadas verificadas: **{stats['transactions_verified']}**.",
            "- Las referencias de precios sugeridos de nuevos se conservan como historial separado y no alimentan el calculador de usados.",
            "- Las referencias de precios solicitados de usados no son transacciones efectivas. Solo participan si están verificadas, recientes y asociadas a la versión exacta.",
            "", "## Fuentes oficiales utilizadas en la ampliación", "",
            "- Chevrolet Colombia: fichas técnicas MY27 de Onix Sedán, Onix y Tracker; ficha de Spark EUV marcada para revisión por discrepancia del año modelo en el documento.",
            "- Mazda Colombia: ficha CX-30 publicada en junio de 2024 y aplicable desde año modelo 2024; ficha CX-5 publicada en enero de 2024 y aplicable desde año modelo 2024.",
            "- Toyota Colombia: fichas técnicas Corolla Cross MY2026 y Yaris Cross MY2026.",
            "- Ford Colombia: páginas de producto Ranger y Territory; Territory publica precios sugeridos para modelo 2027 con vigencia en octubre de 2026.",
            "- Kia Usados Colombia: dos anuncios de Picanto Zenith 2026; se conservaron como precios solicitados no verificados, sin utilizarlos en el cálculo.",
            "", "## Licencias y límites", "",
            "- No se descargaron masivamente anuncios de terceros ni se eludieron controles de acceso.",
            "- No se importaron datos de Fasecolda; debe confirmarse la licencia y el alcance permitido antes de cualquier incorporación masiva.",
            "- Los precios sugeridos de fabricantes no sustituyen comparables de vehículos usados.",
            "- La fecha de consulta indica cuándo se revisó la fuente en este proceso; no implica que la página siga vigente después de esa fecha.",
            "",
        ]
        (out / "INFORME_FUENTES_Y_PRECIOS.md").write_text("\n".join(source_lines), encoding="utf-8")

        duplicate_lines = [
            "# Informe de posibles duplicados", "",
            f"Fecha de generación: {REPORT_DATE}", "",
            f"Pares que coinciden al normalizar marca, línea, año, versión, motor, combustible y transmisión, pero difieren en la generación registrada: **{len(pairs)}**.",
            "La tabla completa está en `posibles_duplicados.csv`. La clave canónica tiene índice único para impedir duplicados normalizados exactos en los flujos de importación.",
            "No se fusiona automáticamente una coincidencia que solo sea posible; debe revisarse para no combinar generaciones o versiones auténticamente diferentes.",
            "",
        ]
        (out / "INFORME_DUPLICADOS.md").write_text("\n".join(duplicate_lines), encoding="utf-8")
        pending_lines = [
            "# Informe de datos pendientes de verificación", "",
            f"Fecha de generación: {REPORT_DATE}", "",
            f"Versiones con estado pendiente/parcial o sin URL/fecha de fuente técnica: **{len(pending)}**.",
            f"Versiones sin referencias económicas de ningún tipo: **{len(no_prices)}**.",
            "Revisar `datos_pendientes_verificacion.csv` y `versiones_sin_precios.csv` antes de ampliar la valoración a nuevas versiones.",
            "Los registros parciales se conservan para revisión y no se describen como plenamente verificados.",
            "",
        ]
        (out / "INFORME_DATOS_PENDIENTES.md").write_text("\n".join(pending_lines), encoding="utf-8")
        print(f"Informes generados en {out}")
        print(stats)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
