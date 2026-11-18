"""Pruebas obligatorias de la auditoría de transmisiones (cajas mecánicas, automáticas y subtipos).

Se ejecutan sobre una COPIA de la base real del proyecto (database/autovalor.db), de modo que
cubren los datos de verdad: Suzuki Vitara 2018 1.6 L (el caso de la captura) y otros modelos
con cajas distintas (Renault Duster, Mazda 2, Chevrolet Onix, Nissan NP300 Frontier).
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import transmissions as T
from app import create_app
from catalog_selectors import model_year_rows, engine_options, transmission_options, validate_combination, version_options
from catalog_utils import engine_groups
from db import connect_db, init_db
from transmission_audit import run_audit, verify_price_links
from valuation import estimate_vehicle

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "database" / "autovalor.db"
BACKUPS = ROOT / "database" / "backups"
RM = ROOT / "data" / "revista_motor"


def _copy_db(src: Path, dst: Path) -> None:
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    a.backup(b)
    b.close()
    a.close()


class ClassificationUnitTests(unittest.TestCase):
    """Equivalencias de nomenclatura (sin base de datos)."""

    def test_manual_text_is_mecanica(self):
        for text in ("Mecánica", "Mecánica 5 velocidades", "Manual", "MT", "mecanica de 6 velocidades"):
            parsed = T.classify_text(text)
            self.assertEqual(parsed["category"], T.CATEGORY_MANUAL, text)
        self.assertEqual(T.classify_text("Mecánica 6 velocidades")["gears"], 6)

    def test_automatic_text_is_automatica(self):
        for text in ("Automática", "AT", "Automática 6 velocidades SKYACTIV-DRIVE", "Automática 10 velocidades"):
            self.assertEqual(T.classify_text(text)["category"], T.CATEGORY_AUTOMATIC, text)
        self.assertEqual(T.classify_text("Automática 6 velocidades SKYACTIV-DRIVE")["detail"], "SKYACTIV-DRIVE")

    def test_cvt_and_dct_are_automatic_with_subtype(self):
        cvt = T.classify_text("Automática CVT + modo manual (10 velocidades)")
        self.assertEqual((cvt["category"], cvt["subtype_code"], cvt["gears"]), (T.CATEGORY_AUTOMATIC, "cvt", None))
        self.assertEqual(T.classify_description("DUSTER INTENS 1.3 T. CVT")["subtype_code"], "cvt")
        self.assertEqual(T.classify_description("OCTAVIA STYLE 1.4 TSI DSG")["subtype_code"], "dct")
        self.assertEqual(T.classify_description("NEW A3 1.2 T. 3P S-TRONIC")["subtype_code"], "dct")
        self.assertEqual(T.classify_description("A5 1.8 TFSI CABRIO RED HOOD MULT.")["subtype_code"], "cvt")
        self.assertEqual(T.classify_description("NEW GOL VOYAGE HIGHLINE 1.6 ASG")["subtype_code"], "amt")

    def test_modo_manual_does_not_make_it_mechanical(self):
        self.assertEqual(T.classify_text("Automática CVT + modo manual (10 velocidades)")["category"], T.CATEGORY_AUTOMATIC)

    def test_unspecified_is_never_a_gearbox(self):
        for text in ("No especificada", "No documentada en fuente consultada", "Sin confirmar", "", None):
            self.assertIsNone(T.classify_text(text))
            self.assertEqual(T.category_for_key(text), T.CATEGORY_UNCONFIRMED)
        self.assertIsNone(T.classify_description("DUSTER 1.6 EXPRESSION 4X2"))
        self.assertIsNone(T.classify_description("RS3 SB ST QUATTRO"))  # «ST» es ambiguo: no se interpreta
        self.assertEqual(T.transmission_label(T.CATEGORY_UNCONFIRMED), "Caja sin confirmar")

    def test_spelling_variants_share_canonical_category(self):
        from catalog_utils import vehicle_canonical_key
        a = vehicle_canonical_key("Mazda", "3", 2020, None, "Touring", "2.0 L", None, "Automática")
        b = vehicle_canonical_key("Mazda", "3", 2020, None, "Touring", "2.0 L", None, "Automática 6 velocidades")
        c = vehicle_canonical_key("Mazda", "3", 2020, None, "Touring", "2.0 L", None, "Mecánica")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class RealCatalogTransmissionTests(unittest.TestCase):
    """Comportamiento sobre la base real auditada (copia temporal)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "real.db"
        _copy_db(REAL_DB, cls.db_path)
        cls.app = create_app({"TESTING": True, "DATABASE_PATH": str(cls.db_path), "SECRET_KEY": "k" * 32,
                              "ADMIN_PASSWORD": "pw-test-admin", "SEED_DATA": True, "SESSION_COOKIE_SECURE": False})
        cls.client = TestClient(cls.app)
        cls.conn = connect_db(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    # -- utilidades ---------------------------------------------------------
    def version(self, raw, year):
        row = self.conn.execute(
            "SELECT * FROM vehicle_versions WHERE raw_description=? AND model_year=? ORDER BY id LIMIT 1",
            (raw, year)).fetchone()
        self.assertIsNotNone(row, f"no existe {raw} {year}")
        return row

    def model_id(self, brand, model):
        return self.conn.execute(
            "SELECT m.id FROM models m JOIN brands b ON b.id=m.brand_id WHERE b.name=? AND m.name=?",
            (brand, model)).fetchone()[0]

    def engine_id_for(self, version_row):
        engines = self.client.get("/api/engines", params={"model_id": version_row["model_id"],
                                                          "year": version_row["model_year"]}).json()
        # Mismas filas que ofrece el formulario (solo versiones con valoración disponible)
        rows = model_year_rows(self.conn, version_row["model_id"], version_row["model_year"])
        group = next(g for g in engine_groups(rows) if version_row["id"] in g["member_ids"])
        self.assertIn(group["engine_id"], [e["engine_id"] for e in engines])
        return group["engine_id"]

    def options(self, version_row):
        engine_id = self.engine_id_for(version_row)
        return engine_id, self.client.get("/api/transmissions", params={
            "model_id": version_row["model_id"], "year": version_row["model_year"], "engine_id": engine_id}).json()

    # -- 1 y 2: mecánica y automática identificadas (caso de la captura) -----
    def test_01_vitara_2018_manual_is_verified_mecanica(self):
        v = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        self.assertEqual(v["transmission"], "Mecánica")
        self.assertEqual(v["transmission_status"], "verificada")
        self.assertEqual(v["transmission_gears"], 5)
        self.assertEqual(v["transmission_original"], "No especificada")  # se conserva el valor original
        self.assertIn("SUZ-VITARA-LIVE-16-2018-2019", v["transmission_rule"])

    def test_02_vitara_2018_automatic_is_verified_automatica(self):
        v = self.version("VITARA LIVE GL 1.6 4X2 AUT.", 2018)
        self.assertEqual((v["transmission"], v["transmission_status"], v["transmission_gears"]),
                         ("Automática", "verificada", 6))

    # -- 3: CVT es automática y conserva su subtipo --------------------------
    def test_03_cvt_is_automatic_with_cvt_subtype(self):
        v = self.version("DUSTER INTENS 1.3 T. CVT", 2022)
        self.assertEqual(v["transmission"], "Automática")
        self.assertEqual(v["transmission_subtype"], T.SUBTYPES["cvt"])
        self.assertEqual(v["transmission_status"], "verificada")
        item = next(i for i in self.client.get("/api/versions", params={
            "model_id": v["model_id"], "year": 2022, "engine_id": self.engine_id_for(v), "transmission_id": v["id"]}).json()
            if i["id"] == v["id"])
        self.assertEqual(item["transmission"], "Automática")
        self.assertIn("CVT", item["transmission_label"])

    # -- 4: los años no se mezclan -------------------------------------------
    def test_04_years_are_not_mixed(self):
        v18 = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        v22 = self.version("VITARA LIVE GL 1.6 4X2", 2022)
        self.assertEqual(v22["transmission"], "Mecánica")
        self.assertEqual(v22["transmission_status"], "inferida")  # la ficha 2018-2019 no se copia a 2022
        engine_id, opts = self.options(v18)
        for opt in opts:
            rows = version_options(self.conn, v18["model_id"], 2018, engine_id, opt["transmission_id"])
            self.assertTrue(rows)
            self.assertEqual({r["model_year"] for r in rows}, {2018})
        # Un motor de 2022 no sirve para pedir cajas de 2018
        self.assertEqual(self.client.get("/api/transmissions", params={
            "model_id": v18["model_id"], "year": 2018, "engine_id": v22["id"]}).json(), [])

    # -- 5: no se ofrecen motores incompatibles ------------------------------
    def test_05_only_engines_of_that_year_are_offered(self):
        mid = self.model_id("Suzuki", "Vitara")
        e2018 = self.client.get("/api/engines", params={"model_id": mid, "year": 2018}).json()
        self.assertEqual([e["engine_label"] for e in e2018], ["1.6 L"])
        e2020 = {e["engine_label"] for e in self.client.get("/api/engines", params={"model_id": mid, "year": 2020}).json()}
        # Todas las versiones de 2020 tienen su motor documentado (ya no aparece «Motor no especificado»)
        self.assertEqual(e2020, {"1.6 L"})
        # Motor partido en dos opciones («1.6 L» y «1.6 L gasolina») ahora es una sola opción
        logan = self.model_id("Renault", "Logan")
        labels = [e["engine_label"] for e in self.client.get("/api/engines", params={"model_id": logan, "year": 2020}).json()]
        self.assertEqual(len([l for l in labels if l.startswith("1.6")]), 1, labels)

    # -- 6: el selector muestra las cajas documentadas (caso de la captura) ---
    def test_06_vitara_2018_selector_shows_manual_and_automatic_only(self):
        v = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        _, opts = self.options(v)
        self.assertEqual([o["transmission"] for o in opts], ["Mecánica", "Automática"])
        self.assertTrue(all(o["confirmed"] for o in opts))
        self.assertTrue(all(o["status"] == "verificada" for o in opts))
        text = str(opts)
        self.assertNotIn("No especificada", text)
        self.assertNotIn("Sin confirmar", text)

    def test_06b_other_models_show_their_documented_gearboxes(self):
        cases = [("2 SEDÁN 1.5 PRIME", 2021, {"Mecánica", "Automática"}),          # Mazda 2
                 ("ONIX LT 1.0 T.", 2021, {"Mecánica", "Automática"}),            # Onix Turbo LT
                 ("NEW NP300 FRONTIER LE 2.5 4X4 T. DIÉSEL DOB.", 2021, {"Mecánica", "Automática"})]
        for raw, year, expected in cases:
            _, opts = self.options(self.version(raw, year))
            self.assertTrue(expected <= {o["transmission"] for o in opts}, (raw, opts))
        # Sin marca en la guía; el RS Turbo 2021 se lanzó mecánico (marzo 2020) y también figuró «AT»
        # (abril 2020): existió con ambas cajas y la fila sin marca es la mecánica, por convención documentada.
        rs = self.version("ONIX RS 1.0 T. 5P", 2021)
        self.assertEqual((rs["transmission"], rs["transmission_status"]), ("Mecánica", "verificada"))
        # Caso de la captura del usuario: Montero Hard Top 2009 2.4 ya no queda «sin confirmar»
        mt = self.version("MONTERO HARD TOP 2.4", 2009)
        self.assertEqual((mt["transmission"], mt["transmission_status"]), ("Mecánica", "verificada"))

    # -- 7: cambiar de motor actualiza las opciones ---------------------------
    def test_07_changing_engine_changes_options(self):
        mid = self.model_id("Renault", "Duster")
        engines = {e["engine_label"]: e["engine_id"] for e in self.client.get(
            "/api/engines", params={"model_id": mid, "year": 2013}).json()}
        opts16 = {o["transmission"] for o in self.client.get("/api/transmissions", params={
            "model_id": mid, "year": 2013, "engine_id": engines["1.6 L"]}).json()}
        opts20 = {o["transmission"] for o in self.client.get("/api/transmissions", params={
            "model_id": mid, "year": 2013, "engine_id": engines["2.0 L"]}).json()}
        self.assertEqual(opts16, {"Mecánica"})
        self.assertEqual(opts20, {"Mecánica", "Automática"})

    # -- 8: cambiar de año no conserva una caja incompatible -------------------
    def test_08_transmission_from_other_year_is_not_accepted(self):
        v18 = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        v20 = self.version("VITARA LIVE GL 1.6 4X2", 2020)
        engine20 = self.engine_id_for(v20)
        # transmission_id de 2018 con año 2020: no hay versiones
        self.assertEqual(self.client.get("/api/versions", params={
            "model_id": v20["model_id"], "year": 2020, "engine_id": engine20, "transmission_id": v18["id"]}).json(), [])
        # y la valoración rechaza la combinación
        r = self.client.post("/api/estimate", json={"version_id": v20["id"], "mileage_km": 50000, "model_id": v20["model_id"],
                                                    "model_year": 2020, "engine_id": engine20, "transmission_id": v18["id"]})
        self.assertEqual(r.status_code, 422)

    # -- 9 y 10: el servidor acepta lo válido y rechaza lo incompatible --------
    def test_09_backend_accepts_valid_combination(self):
        v = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        engine_id, opts = self.options(v)
        manual = next(o for o in opts if o["transmission"] == "Mecánica")
        brand_id = self.conn.execute("SELECT brand_id FROM models WHERE id=?", (v["model_id"],)).fetchone()[0]
        r = self.client.post("/api/estimate", json={
            "version_id": v["id"], "mileage_km": 60000, "brand_id": brand_id, "model_id": v["model_id"],
            "model_year": 2018, "engine_id": engine_id, "transmission_id": manual["transmission_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["estimable"])
        self.assertEqual(data["vehicle"]["transmission"], "Mecánica")
        self.assertTrue(data["vehicle"]["transmission_verified"])

    def test_10_backend_rejects_incompatible_combination(self):
        man = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        aut = self.version("VITARA LIVE GL 1.6 4X2 AUT.", 2018)
        engine_id, opts = self.options(man)
        automatic = next(o for o in opts if o["transmission"] == "Automática")
        brand_id = self.conn.execute("SELECT brand_id FROM models WHERE id=?", (man["model_id"],)).fetchone()[0]
        bad = [
            {"transmission_id": automatic["transmission_id"]},                 # versión mecánica con caja automática
            {"model_year": 2019},                                              # año distinto
            {"model_id": self.model_id("Renault", "Logan")},                   # otra línea
            {"brand_id": brand_id + 1000},                                     # otra marca
            {"engine_id": self.version("VITARA ALL-GRIP GLX", 2020)["id"]},    # motor de otro año
        ]
        for extra in bad:
            payload = {"version_id": man["id"], "mileage_km": 60000, "brand_id": brand_id, "model_id": man["model_id"],
                       "model_year": 2018, "engine_id": engine_id,
                       "transmission_id": next(o for o in opts if o["transmission"] == "Mecánica")["transmission_id"]}
            payload.update(extra)
            r = self.client.post("/api/estimate", json=payload)
            self.assertEqual(r.status_code, 422, (extra, r.text))
        self.assertIsNone(validate_combination(self.conn, aut["id"], model_id=aut["model_id"], model_year=2018,
                                               engine_id=engine_id, transmission_id=automatic["transmission_id"]))

    # -- 11: los precios siguen ligados a la versión correcta -----------------
    def test_11_prices_still_linked_to_their_versions(self):
        integrity = verify_price_links(self.conn)
        self.assertGreater(integrity["checked"], 5000)
        self.assertEqual([p for p in integrity["problems"] if "caja" in p["problema"] or "no encontrada" in p["problema"]], [])
        man = self.version("VITARA LIVE GL 1.6 4X2", 2018)
        aut = self.version("VITARA LIVE GL 1.6 4X2 AUT.", 2018)
        price = lambda vid: self.conn.execute("SELECT price_cop FROM price_references WHERE version_id=? AND "
                                              "price_type='guia_comercial'", (vid,)).fetchone()[0]
        self.assertEqual((price(man["id"]), price(aut["id"])), (59_400_000, 61_600_000))
        backups = sorted(BACKUPS.glob("autovalor_pre_auditoria_transmisiones_*.db"))
        if backups:
            old = sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
            before = old.execute("SELECT id, version_id, price_cop, price_type, model_year FROM price_references ORDER BY id").fetchall()
            old.close()
            after = self.conn.execute("SELECT id, version_id, price_cop, price_type, model_year FROM price_references ORDER BY id").fetchall()
            # Ningún precio se pierde ni cambia de valor. Solo cambian de versión los precios trasladados
            # en una fusión documentada en audit_log (la misma versión publicada dos veces).
            moved = {json.loads(d).get("precio_trasladado") for (d,) in self.conn.execute(
                "SELECT details FROM audit_log WHERE action='merge_version'")}
            # También los que pasó la auditoría de motores al unir la misma versión de las dos listas.
            for (d,) in self.conn.execute("SELECT details FROM audit_log WHERE action='engine_audit_fusion'"):
                moved |= {m["id"] for m in json.loads(d)["precios_movidos"]}
            # Las únicas referencias nuevas son copias documentadas para las variantes automáticas agregadas.
            before_ids = {r[0] for r in before}
            added = [r[0] for r in after if r[0] not in before_ids]
            notes = {pid: n for pid, n in self.conn.execute("SELECT id, notes FROM price_references")}
            self.assertTrue(all("Variante automática agregada" in (notes[pid] or "") for pid in added))
            after = [r for r in after if r[0] in before_ids]
            self.assertEqual([(r[0], r[2], r[3], r[4]) for r in before], [(r[0], r[2], r[3], r[4]) for r in after])
            changed = {b[0] for b, a in zip(before, after) if b[1] != a[1]}
            self.assertLessEqual(changed, moved)

    # -- 12: no se generan duplicados -----------------------------------------
    def test_12_no_duplicates_and_audit_is_idempotent(self):
        dup = self.conn.execute("SELECT canonical_key, COUNT(*) FROM vehicle_versions WHERE canonical_key IS NOT NULL "
                                "GROUP BY canonical_key HAVING COUNT(*) > 1").fetchall()
        self.assertEqual(dup, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE canonical_key IS NULL").fetchone()[0], 0)
        n_versions = self.conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
        second = run_audit(self.conn, apply=True, with_integrity=False)
        self.assertEqual(second["summary"]["versions_changed"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0], n_versions)

    def test_12b_reimporting_the_price_guide_creates_no_versions(self):
        from importers.revista_import import run_import
        tmp = Path(self.tmp.name) / "reimport.db"
        _copy_db(self.db_path, tmp)
        conn = connect_db(tmp)
        try:
            before = conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
            summary = run_import(conn, RM / "revista_motor_2026-10-14_usados_extraido.csv",
                                 RM / "revista_motor_2026-10-14_nuevos_extraido.csv")
            self.assertEqual(summary["contadores"].get("versiones_nuevas", 0), 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0], before)
            again = run_audit(conn, apply=True, with_integrity=False)
            self.assertEqual(again["summary"]["versions_changed"], 0)
        finally:
            conn.close()

    # -- 13: lo pendiente no se presenta como verificado ----------------------
    def test_13_pending_versions_are_not_presented_as_verified(self):
        # En la base real ya no queda ninguna versión con la caja sin confirmar.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE transmission_status='pendiente' "
                                           "OR transmission='Sin confirmar'").fetchone()[0], 0)
        # Si en el futuro se importa una versión sin caja documentada, se muestra como pendiente y no
        # como verificada. Se comprueba sobre una copia aparte con una versión marcada como pendiente.
        tmp = Path(self.tmp.name) / "pendiente.db"
        _copy_db(self.db_path, tmp)
        conn = connect_db(tmp)
        vid = conn.execute(
            """SELECT v.id FROM vehicle_versions v WHERE v.transmission_status='inferida'
               AND EXISTS(SELECT 1 FROM price_references p WHERE p.version_id=v.id AND p.price_type='guia_comercial')
               ORDER BY v.id LIMIT 1""").fetchone()[0]
        conn.execute("UPDATE vehicle_versions SET transmission='Sin confirmar', transmission_status='pendiente', "
                     "transmission_subtype=NULL, transmission_gears=NULL, canonical_key=canonical_key||'|prueba' WHERE id=?", (vid,))
        conn.commit()
        app = create_app({"TESTING": True, "DATABASE_PATH": str(tmp), "SECRET_KEY": "k" * 32,
                          "ADMIN_PASSWORD": "pw-test-admin", "SEED_DATA": True, "SESSION_COOKIE_SECURE": False})
        client = TestClient(app)
        pending = conn.execute("SELECT * FROM vehicle_versions WHERE id=?", (vid,)).fetchone()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["transmission"], "Sin confirmar")
        engine_id = self.engine_id_for(pending)
        opts = client.get("/api/transmissions", params={"model_id": pending["model_id"], "year": pending["model_year"], "engine_id": engine_id}).json()
        opt = next(o for o in opts if o["transmission"] == "Sin confirmar")
        self.assertFalse(opt["confirmed"])
        self.assertNotIn(opt["label"], ("Mecánica", "Automática", "No especificada"))
        item = next(i for i in client.get("/api/versions", params={
            "model_id": pending["model_id"], "year": pending["model_year"], "engine_id": engine_id,
            "transmission_id": opt["transmission_id"]}).json() if i["id"] == pending["id"])
        self.assertFalse(item["transmission_verified"])
        self.assertFalse(item["transmission_confirmed"])
        self.assertEqual(item["transmission_label"], "Caja sin confirmar")
        data = client.post("/api/estimate", json={"version_id": pending["id"], "mileage_km": 40000}).json()
        self.assertFalse(data["vehicle"]["transmission_verified"])
        self.assertTrue(any("no está confirmada" in w for w in data["warnings"]))
        conn.close()
        # No queda ningún «No especificada» como caja en la base
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE transmission NOT IN "
                                           "('Mecánica','Automática','Sin confirmar')").fetchone()[0], 0)

    # -- 13b: versiones con las dos cajas ofrecen ambas; sin nombre = «Estándar» ----
    def test_13b_both_gearboxes_offered_and_standard_name(self):
        v = self.version("AMAROK 2.0 HIGHLINE BI-T. 4X4 DIÉSEL", 2019)
        engine_id, opts = self.options(v)
        self.assertEqual(sorted(o["transmission"] for o in opts), ["Automática", "Mecánica"])
        for o in opts:
            names = [i["version_name"] if "version_name" in i else i.get("name") for i in self.client.get(
                "/api/versions", params={"model_id": v["model_id"], "year": 2019, "engine_id": engine_id,
                                         "transmission_id": o["transmission_id"]}).json()]
            self.assertTrue(any("Highline" in (n or "") for n in names), names)
        # La variante automática usa la misma referencia de precio que la guía publica para la versión
        aut = self.conn.execute("SELECT id FROM vehicle_versions WHERE model_id=? AND model_year=2019 AND "
                                "version_name=? AND transmission='Automática'", (v["model_id"], v["version_name"])).fetchone()
        price = lambda vid: self.conn.execute("SELECT price_cop FROM price_references WHERE version_id=? AND "
                                              "price_type='guia_comercial'", (vid,)).fetchone()[0]
        self.assertEqual(price(aut[0]), price(v["id"]))
        # Una versión con una sola caja solo ofrece esa caja (Peugeot 208 1.6 HDi diésel: mecánica)
        d = self.version("208 ACTIVE 1.6 DIÉSEL", 2019)
        self.assertEqual(d["transmission"], "Mecánica")
        # Ninguna versión queda sin nombre: las filas sin versión en la guía se llaman «Estándar»
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE version_name IN "
                                           "('', 'Sin versión especificada') OR version_name IS NULL").fetchone()[0], 0)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE version_name='Estándar'").fetchone()[0], 100)

    # -- 13c: toda versión que ofrece el formulario da un valor ------------------------
    def test_13c_every_listed_version_has_a_valuation(self):
        from catalog_selectors import valuable_version_ids
        listed = sorted(valuable_version_ids(self.conn))
        self.assertGreater(len(listed), 5500)
        for vid in listed:
            result = estimate_vehicle(self.conn, vid, 60000)
            self.assertTrue(result["estimable"], (vid, result.get("reason")))
            self.assertGreater(result["valuation"]["value_cop"], 0)
        # Modelo del año en curso con solo precio de lista de nuevo: valora y lo advierte
        polo = self.conn.execute("SELECT id FROM vehicle_versions WHERE id=5268").fetchone()
        if polo:
            r = estimate_vehicle(self.conn, 5268, 5000)
            self.assertEqual(r["valuation"]["basis"], "precio_nuevo")
            self.assertTrue(any("precio de lista del vehículo nuevo" in w for w in r["warnings"]))
        # Dos valores casi iguales en la guía para la misma versión y año: se promedian
        d = self.version("DISCOVERY 3.0 S", 2023)
        r = estimate_vehicle(self.conn, d["id"], 40000)
        self.assertTrue(r["estimable"])
        self.assertEqual(r["valuation"]["reference_price_cop"], 310_500_000)

    # -- 14: sin «caja no verificada» cuando la caja sí está verificada --------
    def test_14_no_unverified_gearbox_message_for_verified_configurations(self):
        verified = self.conn.execute(
            """SELECT v.id FROM vehicle_versions v WHERE v.transmission_status='verificada'
               AND EXISTS(SELECT 1 FROM price_references p WHERE p.version_id=v.id AND p.price_type='guia_comercial')""").fetchall()
        self.assertGreater(len(verified), 50)
        for (vid,) in verified[:80]:
            result = estimate_vehicle(self.conn, vid, 50000)
            joined = " ".join(result["warnings"]).lower()
            self.assertNotIn("transmisión de esta versión no está confirmada", joined)
            self.assertNotIn("(motor, combustible, transmisión) no están verificadas", joined)
            self.assertNotIn("caja", joined)

    # -- 15: el resto de AutoValor sigue funcionando ---------------------------
    def test_15_rest_of_app_and_km_method_unchanged(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertNotIn("revista", home.text.lower())
        backups = sorted(BACKUPS.glob("autovalor_pre_auditoria_transmisiones_*.db"))
        if not backups:
            self.skipTest("sin copia previa para comparar")
        old = connect_db(backups[0])
        try:
            sample = [r[0] for r in self.conn.execute(
                """SELECT v.id FROM vehicle_versions v JOIN price_references p ON p.version_id=v.id
                   WHERE p.price_type='guia_comercial' GROUP BY v.id ORDER BY v.id LIMIT 60""")]
            for vid in sample:
                a = estimate_vehicle(old, vid, 75000)
                b = estimate_vehicle(self.conn, vid, 75000)
                self.assertEqual(a.get("estimable"), b.get("estimable"))
                if a.get("valuation"):
                    self.assertEqual(a["valuation"]["value_cop"], b["valuation"]["value_cop"])
                    self.assertEqual(a["valuation"]["km_adjustment_pct"], b["valuation"]["km_adjustment_pct"])
        finally:
            old.close()


if __name__ == "__main__":
    unittest.main()
