"""Pruebas de la auditoría de motores (engine_audit.py).

Las pruebas de datos usan una COPIA de la base real (database/autovalor.db). Las de reglas usan
filas de ejemplo en memoria, sin base de datos.
"""
import csv
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog_selectors import valuable_version_ids
from db import connect_db
from engine_audit import (ELECTRIC_LABEL, LEVELS, SOURCES_CSV, SPECS_CSV, EngineSpec, load_specs, needs_engine,
                          run_engine_audit, target_for)

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "database" / "autovalor.db"

# Versiones que la guía publica sin cilindrada y cuyo motor NO se pudo confirmar con una fuente
# (dos motores posibles el mismo año, versión que no aparece en ninguna ficha, etc.). Se dejan
# como «Motor no especificado» en vez de inventar el dato. (marca, línea, descripción): años.
PENDING = {
    ('Audi', 'A6', 'A6 AMBITION ST'): (2019, 2020, 2021, 2022, 2023),
    ('BMW', '114', '114 COUPÉ'): (2018,),
    ('Citroën', 'C3', 'C3 FEEL'): (2020,),
    ('Fiat', 'Cronos', 'CRONOS'): (2019, 2020),
    ('Ford', 'Escape', 'ESCAPE ST LINE'): (2019, 2020),
    ('Ford', 'F-150', 'F-150 XLT SINGLE CAB'): (2022, 2023, 2024),
    ('Honda', 'CR-V', 'CR-V CITY PLUS 4WD'): (2022, 2023),
    ('Honda', 'CR-V', 'CR-V EXL AUT.'): (2018, 2019),
    ('Hyundai', 'I20', 'I20 ADVANCE A.A. AUT.'): (2018, 2019),
    ('Hyundai', 'Santa Fe', 'SANTAFE ADVANCE 4X2'): (2018, 2019),
    ('Hyundai', 'Santa Fe', 'SANTAFE ADVANCE 4X2 AUT.'): (2018, 2019),
    ('Hyundai', 'Santa Fe', 'SANTAFE ADVANCE 4X4 AUT. DIÉSEL'): (2018, 2019),
    ('Hyundai', 'Santa Fe', 'SANTAFE LIMITED 4X4 AUT.'): (2018, 2019, 2020),
    ('Hyundai', 'Santa Fe', 'SANTAFE PREMIUM 4X4 AUT.'): (2018, 2019, 2020),
    ('JMC', 'Vigus', 'VIGUS WORK DOB 4X4 AUT'): (2026,),
    ('Kia', 'Sportage', 'SPORTAGE DESIRE AUT. 4X2'): (2020, 2021, 2022),
    ('Land Rover', 'Range Rover Evoque', 'RANGE ROVER EVOQUE BRONZE'): (2023,),
    ('Mercedes-Benz', 'B 180', 'B 180 URBAN'): (2018, 2019, 2020),
    ('Mercedes-Benz', 'C 200', 'C 200 C43'): (2023,),
    ('Mitsubishi', 'L200', 'L200 TRITON GLS'): (2023, 2024),
    ('Mitsubishi', 'L200', 'L200 TRITON GLX'): (2020, 2021, 2022, 2023, 2024),
    ('Toyota', 'Prado', 'NEW PRADO 5P TX AUT.'): (2026,),
    ('Toyota', 'Prado', 'NEW PRADO 5P TX-L AUT.'): (2026,),
    ('Toyota', 'Prado', 'PRADO 3P'): (2009,),
}


def _copy_db(src: Path, dst: Path) -> None:
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    a.backup(b)
    b.close()
    a.close()


def _row(desc, year, label="No especificado", fuel=None, brand="Marca", model="Linea", category="AUTOMÓVILES"):
    return {"id": 1, "brand": brand, "model": model, "model_year": year, "raw_description": desc,
            "version_name": desc, "engine_label": label, "displacement_cc": None, "fuel": fuel,
            "source_category": category, "engine_original": None}


def _spec(spec_id, label, fuel=None, desc=None, fuel_re=None, fix=None, level="verificada"):
    return EngineSpec(spec_id=spec_id, brand="Marca", model="Linea", year_from=2018, year_to=2026,
                      desc_re=re.compile(desc) if desc else None, excl_re=None, engine_label=label,
                      displacement_cc=None, fuel=fuel, level=level, source_ids="X", evidence="prueba",
                      fuel_re=re.compile(fuel_re) if fuel_re else None, fix_label=fix)


class EngineRuleTests(unittest.TestCase):
    """Reglas de la auditoría con filas de ejemplo."""

    def by_model(self, *specs):
        return {("marca", "linea"): list(specs)}

    def test_label_with_displacement_is_never_touched_by_a_normal_spec(self):
        specs = self.by_model(_spec("A", "2.0 L"))
        self.assertIsNone(target_for(_row("X 1.6", 2020, label="1.6 L"), specs))
        self.assertEqual(target_for(_row("X", 2020), specs)["engine_label"], "2.0 L")

    def test_fix_label_only_corrects_that_exact_label(self):
        # La guía tomó «xDrive40i» como «4.0»; la ficha de corrección solo toca esa etiqueta exacta.
        specs = self.by_model(_spec("FIX", "3.0 L turbo", fuel="Gasolina", fix="4.0 L"))
        self.assertEqual(target_for(_row("X5 XLINE 4.0", 2020, label="4.0 L"), specs)["engine_label"], "3.0 L turbo")
        self.assertIsNone(target_for(_row("X5 XLINE 3.0", 2020, label="3.0 L"), specs))
        self.assertIsNone(target_for(_row("X5 XLINE", 2020), specs))  # sin cilindrada: no es una corrección
        # la corrección también fija el combustible que documenta
        self.assertEqual(target_for(_row("GLE 500 4.7 DIESEL", 2019, label="4.7 L", fuel="Diésel"),
                                    self.by_model(_spec("F2", "4.7 L", fuel="Gasolina", fix="4.7 L")))["fuel"], "Gasolina")

    def test_fuel_regex_separates_rows_with_the_same_description(self):
        specs = self.by_model(_spec("MHEV", "3.3 L", fuel="Híbrido ligero", fuel_re="ligero"),
                              _spec("PHEV", "2.5 L", fuel="Híbrido enchufable", fuel_re="enchufable"))
        self.assertEqual(target_for(_row("CX-60 GTS", 2026, fuel="Híbrido ligero"), specs)["engine_label"], "3.3 L")
        self.assertEqual(target_for(_row("CX-60 GTS", 2026, fuel="Híbrido enchufable"), specs)["engine_label"], "2.5 L")

    def test_electric_versions_get_no_combustion_engine(self):
        specs = self.by_model(_spec("A", "1.5 L turbo"))
        for row in (_row("KONA LIMITED", 2026, fuel="Eléctrico"), _row("KONA LIMITED", 2026, category="ELÉCTRICOS")):
            tgt = target_for(row, specs)
            self.assertEqual((tgt["engine_label"], tgt["rule"]), (ELECTRIC_LABEL, "E-electrico"))

    def test_hybrid_fuel_from_category_is_made_precise_by_the_spec(self):
        specs = self.by_model(_spec("A", "1.5 L", fuel="Híbrido completo"))
        tgt = target_for(_row("ZS HYBRID +", 2025, fuel="Híbrido (según categoría de la guía)"), specs)
        self.assertEqual(tgt["fuel"], "Híbrido completo")
        # un combustible publicado por la guía no se cambia con una ficha normal
        self.assertEqual(target_for(_row("X", 2025, fuel="Diésel"), specs)["fuel"], "Diésel")

    def test_two_specs_with_different_engines_are_a_conflict(self):
        specs = self.by_model(_spec("A", "1.5 L"), _spec("B", "1.6 L"))
        self.assertIn("conflict", target_for(_row("X", 2020), specs))


class EngineSpecFileTests(unittest.TestCase):
    """El archivo de fichas es coherente y cada ficha cita fuentes existentes."""

    def test_specs_are_valid_and_cite_known_sources(self):
        with SOURCES_CSV.open(encoding="utf-8", newline="") as fh:
            known = {r["id"] for r in csv.DictReader(fh)}
        specs = load_specs()
        self.assertGreater(len(specs), 350)
        self.assertEqual(len({s.spec_id for s in specs}), len(specs))
        for s in specs:
            self.assertIn(s.level, LEVELS, s.spec_id)
            self.assertTrue(s.evidence.strip(), s.spec_id)
            self.assertLessEqual(s.year_from, s.year_to, s.spec_id)
            ids = [x for x in s.source_ids.split(";") if x]
            self.assertTrue(ids, s.spec_id)
            for x in ids:
                self.assertIn(x, known, f"{s.spec_id} cita la fuente inexistente {x}")
            self.assertFalse(needs_engine(s.engine_label, s.displacement_cc), f"{s.spec_id}: etiqueta sin cilindrada")
            if s.fix_label:
                self.assertFalse(needs_engine(s.fix_label, None), f"{s.spec_id}: fix_label debe tener cilindrada")


class RealCatalogEngineTests(unittest.TestCase):
    """Datos de la base real auditada (copia temporal)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "real.db"
        _copy_db(REAL_DB, cls.db_path)
        cls.conn = connect_db(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def version(self, raw, year):
        row = self.conn.execute("SELECT * FROM vehicle_versions WHERE raw_description=? AND model_year=? ORDER BY id",
                                (raw, year)).fetchall()
        self.assertTrue(row, f"no existe {raw} {year}")
        return row

    def test_audit_is_idempotent(self):
        res = run_engine_audit(self.conn, apply=False)
        self.assertEqual((res["changed"], res["merged"]), (0, 0))
        self.assertEqual(res["conflicts"], [])
        self.assertEqual(res["collisions"], [])

    def test_only_documented_versions_remain_without_engine(self):
        rows = self.conn.execute(
            """SELECT v.id, b.name, m.name, v.raw_description, v.model_year, v.engine_label, v.displacement_cc
               FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id""").fetchall()
        pending = {}
        for vid, brand, model, desc, year, label, cc in rows:
            if label != ELECTRIC_LABEL and needs_engine(label, cc):
                pending.setdefault((brand, model, desc), []).append(year)
        self.assertEqual({k: tuple(sorted(v)) for k, v in pending.items()}, PENDING)
        listed = valuable_version_ids(self.conn)
        self.assertGreater(len(listed), 5500)

    def test_every_documented_engine_is_traceable(self):
        with SOURCES_CSV.open(encoding="utf-8", newline="") as fh:
            known = {r["id"] for r in csv.DictReader(fh)}
        rows = self.conn.execute("SELECT id, engine_rule, engine_status, engine_original, engine_evidence, engine_source_ids "
                                 "FROM vehicle_versions WHERE engine_rule IS NOT NULL").fetchall()
        self.assertGreater(len(rows), 1900)
        for vid, rule, status, original, evidence, source_ids in rows:
            self.assertTrue(original, vid)
            json.loads(original)
            self.assertTrue(evidence, vid)
            if rule == "E-electrico":
                self.assertEqual(status, "regla_tecnica")
                continue
            self.assertTrue(rule.startswith("E:"), vid)
            self.assertIn(status, LEVELS, vid)
            for x in (source_ids or "").split(";"):
                self.assertIn(x, known, (vid, x))

    def test_electric_versions_show_electric(self):
        rows = self.conn.execute("SELECT engine_label, engine_rule FROM vehicle_versions WHERE "
                                 "(fuel='Eléctrico' OR source_category='ELÉCTRICOS') AND engine_rule IS NOT NULL").fetchall()
        self.assertGreater(len(rows), 100)
        self.assertEqual({tuple(r) for r in rows}, {(ELECTRIC_LABEL, "E-electrico")})

    def test_documented_cases(self):
        cases = [
            ("L200 GLS SPORTERO", 2019, "2.5 L turbo", "Diésel"),
            ("L200 TRITON GLS", 2026, "2.4 L turbo", "Diésel"),
            ("WRANGLER SAHARA 4XE", 2023, "2.0 L turbo", "Híbrido enchufable"),
            ("FORCE 4X4", 2026, "2.2 L turbo", "Diésel"),
            ("ECLIPSE CROSS 4X4 CVT", 2021, "2.0 L", "Gasolina"),
            ("CX-90 GRAND TOURING SIGNATURE", 2025, "3.3 L", "Híbrido ligero"),
            # la guía escribió la designación comercial como cilindrada
            ("X5 XDRIVE XLINE 4.0 4X4 AUT. G05", 2020, "3.0 L turbo", "Gasolina"),
            ("X3 XDRIVE 3.0 4X4 AUT. G01", 2020, "2.0 L turbo", "Gasolina"),
            ("GLE 500 4.7 4X4 AUT. DIÉSEL", 2019, "4.7 L", "Gasolina"),
        ]
        for desc, year, label, fuel in cases:
            for row in self.version(desc, year):
                self.assertEqual((row["engine_label"], row["fuel"]), (label, fuel), (desc, year))
        # Misma descripción, dos energías: cada una con su motor
        cx60 = {r["fuel"]: r["engine_label"] for r in self.version("CX-60 4X4 GRAND TOURING SIGNATURE AUT", 2026)}
        self.assertEqual(cx60, {"Híbrido ligero": "3.3 L", "Híbrido enchufable": "2.5 L"})

    def test_same_version_from_both_lists_was_merged_without_losing_prices(self):
        merges = [json.loads(d) for (d,) in self.conn.execute(
            "SELECT details FROM audit_log WHERE action='engine_audit_fusion'")]
        self.assertTrue(merges)
        for m in merges:
            self.assertIsNone(self.conn.execute("SELECT id FROM vehicle_versions WHERE id=?",
                                                (m["version_eliminada"],)).fetchone())
            kept = self.conn.execute("SELECT model_year FROM vehicle_versions WHERE id=?",
                                     (m["version_conservada"],)).fetchone()
            self.assertEqual(kept[0], m["anio"])
            for p in m["precios_movidos"]:
                self.assertEqual(tuple(self.conn.execute("SELECT version_id, price_cop FROM price_references WHERE id=?",
                                                         (p["id"],)).fetchone()), (m["version_conservada"], p["price_cop"]))
        # La versión unida conserva el valor de usado y el precio de nuevo
        types = {t for (t,) in self.conn.execute(
            "SELECT p.price_type FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id "
            "WHERE v.raw_description='STONIC 1.0 T DESIRE' AND v.model_year=2026")}
        self.assertEqual(types, {"guia_comercial", "precio_nuevo"})

    def test_no_duplicate_versions_after_documenting_engines(self):
        dup = self.conn.execute(
            """SELECT model_id, model_year, version_name, engine_label, transmission, COUNT(*) FROM vehicle_versions
               GROUP BY 1,2,3,4,5 HAVING COUNT(*) > 1""").fetchall()
        self.assertEqual(dup, [])
        # Dos generaciones con el mismo nombre el mismo año: el nombre dice cuál es
        names = {r["version_name"] for r in self.version("NUEVO SANDERO EXPRESSION", 2016)}
        self.assertEqual(names, {"Expression (Nuevo Sandero)"})


if __name__ == "__main__":
    unittest.main()
