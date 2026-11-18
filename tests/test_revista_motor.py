"""Pruebas del parser, el importador y el motor de valoración con registros reales extraídos de la revista."""
import csv
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from db import connect_db
from importers.revista_import import run_import
from importers.revista_parser import parse_description
from migrations import apply_migrations
import valuation

ROOT = Path(__file__).resolve().parent.parent
USED = ROOT / "data/revista_motor/revista_motor_2026-10-14_usados_extraido.csv"
NEW = ROOT / "data/revista_motor/revista_motor_2026-10-14_nuevos_extraido.csv"


def to_cop(raw):
    return int(round(float(raw.replace(",", ".")) * 1_000_000))


class ParserTests(unittest.TestCase):
    def test_basic_parse(self):
        p = parse_description("MAZDA", "3 2.0 SEDÁN TOURING AUT.", "AUTOMÓVILES")
        self.assertEqual(p.brand, "Mazda")
        self.assertEqual(p.model, "3")
        self.assertEqual(p.engine_label, "2.0 L")
        self.assertEqual(p.transmission, "Automática")

    def test_unspecified_not_invented(self):
        p = parse_description("RENAULT", "LOGAN 1.6 EXPRESSION", "AUTOMÓVILES")
        # Sin marca de caja la transmisión no se supone: queda «Sin confirmar» (nunca «No especificada»
        # presentada como caja, ni mecánica por descarte).
        self.assertEqual(p.transmission, "Sin confirmar")
        p2 = parse_description("AUDI", "RS3 SB ST QUATTRO", "AUTOMÓVILES")
        self.assertEqual(p2.engine_label, "No especificado")

    def test_aliases_and_multiword(self):
        self.assertEqual(parse_description("HYUNDAI", "SANTAFE 2.5 AUT.", "CAMIONETAS").model, "Santa Fe")
        self.assertEqual(parse_description("MAZDA", "CX9 2.5 T AUT.", "CAMIONETAS").model, "CX-9")


class RealImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "t.db"
        cls.app = create_app({"TESTING": True, "DATABASE_PATH": str(cls.db_path), "SECRET_KEY": "x" * 24,
                              "ADMIN_PASSWORD": "pw-test-admin", "SEED_DATA": True, "SESSION_COOKIE_SECURE": False})
        cls.client = TestClient(cls.app)
        conn = connect_db(cls.db_path)
        cls.before = {"v": conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0],
                      "p": conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0]}
        cls.legacy_ids = {r[0]: r[1] for r in conn.execute(
            "SELECT id, version_name||'|'||model_year||'|'||transmission FROM vehicle_versions")}
        cls.legacy_prices = [tuple(r) for r in conn.execute(
            "SELECT id, version_id, price_cop FROM price_references ORDER BY id")]
        cls.summary = run_import(conn, USED, NEW)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.conn = connect_db(self.db_path)

    def tearDown(self):
        self.conn.close()

    def price(self, raw_like, year, price_type):
        rows = self.conn.execute(
            """SELECT p.price_cop FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id
               WHERE v.raw_description LIKE ? AND p.model_year=? AND p.price_type=? AND p.import_run_id IS NOT NULL""",
            (raw_like, year, price_type)).fetchall()
        return sorted({r[0] for r in rows})

    def test_legacy_data_unchanged(self):
        n = self.conn.execute("SELECT COUNT(*) FROM vehicle_versions WHERE id<=?", (max(self.legacy_ids),)).fetchone()[0]
        self.assertGreaterEqual(n, self.before["v"])
        now = [tuple(r) for r in self.conn.execute(
            "SELECT id, version_id, price_cop FROM price_references WHERE import_run_id IS NULL ORDER BY id")]
        self.assertEqual(now, self.legacy_prices)

    def test_import_adds_data(self):
        s = self.summary
        self.assertGreater(s["despues"]["versiones"], s["antes"]["versiones"])
        self.assertGreater(s["despues"]["precios"], s["antes"]["precios"])

    def test_pinned_used_values(self):
        self.assertEqual(self.price("%ONIX LTZ 1.0 T%AUT%", 2024, "guia_comercial"), [64_000_000])
        self.assertEqual(self.price("%ONIX LTZ 1.0 T%AUT%", 2021, "guia_comercial"), [53_200_000])
        self.assertEqual(self.price("LOGAN 1.6 EXPRESSION", 2015, "guia_comercial"), [23_000_000])
        self.assertEqual(self.price("LOGAN 1.6 EXPRESSION", 2009, "guia_comercial"), [15_900_000])
        self.assertEqual(self.price("%HILUX 2.8 176 HP 4X4 DOB.%AUT%", 2026, "guia_comercial"), [247_000_000])
        self.assertEqual(self.price("3 2.0 SEDÁN TOURING AUT.", 2020, "guia_comercial"), [57_000_000])

    def test_new_prices_not_multiplied(self):
        self.assertEqual(self.price("RS3 SB ST QUATTRO", 2026, "precio_nuevo"), [350_000_000])

    def test_decimal_conversion_all_rows(self):
        bad = 0
        with open(USED, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["price_cop"] and "confirmar" not in (r["flag"] or ""):
                    self.assertEqual(to_cop(r["raw_value"]), int(r["price_cop"]), r)
                    bad += 0
        with open(NEW, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["price_cop"]:
                    self.assertEqual(int(re.sub(r"\D", "", r["raw_price"])), int(r["price_cop"]), r)

    def test_year_association(self):
        n = self.conn.execute(
            """SELECT COUNT(*) FROM price_references p JOIN vehicle_versions v ON v.id=p.version_id
               WHERE p.import_run_id IS NOT NULL AND p.column_year IS NOT NULL AND p.column_year<>v.model_year""").fetchone()[0]
        self.assertEqual(n, 0)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM price_references WHERE column_year IS NOT NULL").fetchone()[0], 1000)

    def test_traceability_present(self):
        n = self.conn.execute("SELECT COUNT(*) FROM price_references WHERE import_run_id IS NOT NULL AND (source_ref IS NULL OR source_page IS NULL OR raw_value IS NULL)").fetchone()[0]
        self.assertEqual(n, 0)

    def test_idempotent(self):
        before = self.conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0], \
            self.conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
        s2 = run_import(self.conn, USED, NEW)
        after = self.conn.execute("SELECT COUNT(*) FROM price_references").fetchone()[0], \
            self.conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(s2["despues"]["precios"], s2["antes"]["precios"])

    def test_no_duplicate_canonical_keys(self):
        d = self.conn.execute("SELECT canonical_key, COUNT(*) c FROM vehicle_versions WHERE canonical_key IS NOT NULL GROUP BY 1 HAVING c>1").fetchall()
        self.assertEqual(d, [])

    def test_pending_values_not_in_db(self):
        pend = self.conn.execute("SELECT COUNT(*) FROM import_issues WHERE severity='pendiente'").fetchone()[0]
        self.assertGreater(pend, 0)
        self.assertEqual(self.price("%X5%", 2024, "guia_comercial").count(86_300_000), 0)
        self.assertNotIn(1_100_000, [r[0] for r in self.conn.execute("SELECT price_cop FROM price_references WHERE import_run_id IS NOT NULL")])

    def test_migrations_idempotent(self):
        self.assertEqual(apply_migrations(self.conn), [])

    def test_api_search_and_flags(self):
        r = self.client.get("/api/search", params={"q": "onix", "limit": 5})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        items = data if isinstance(data, list) else data.get("results", [])
        self.assertTrue(items)
        self.assertIn("version_id", items[0])

    def test_estimate_with_guide(self):
        vid = self.conn.execute(
            """SELECT v.id FROM vehicle_versions v JOIN price_references p ON p.version_id=v.id
               WHERE v.raw_description LIKE '%ONIX LTZ 1.0 T%AUT%' AND v.model_year=2021 AND p.price_cop=53200000""").fetchone()[0]
        r = self.client.post("/api/estimate", json={"version_id": vid, "mileage_km": 70000})
        d = r.json()
        self.assertTrue(d["estimable"])
        self.assertEqual(d["valuation"]["reference_price_cop"], 53_200_000)
        self.assertEqual(d["valuation"]["value_cop"] % 100_000, 0)
        # Internamente la base sigue siendo la guía, con su trazabilidad.
        internal = valuation.estimate_vehicle(self.conn, vid, 70000)
        self.assertEqual(internal["valuation"]["basis"], "guia")
        self.assertEqual(internal["references"]["guide"]["price_cop"], 53_200_000)

    def test_homepage_keeps_fields_and_shows_live_data(self):
        html = self.client.get("/").text
        for element_id in ("brand-select", "model-select", "year-select", "engine-select",
                           "transmission-select", "version-select", "mileage-input", "btn-estimate"):
            self.assertIn(f'id="{element_id}"', html)
        # Solo selección paso a paso: sin búsqueda rápida ni accesos de populares o recientes.
        for removed in ('id="vehicle-search"', "Búsqueda rápida", "Populares", "consultas recientes"):
            self.assertNotIn(removed, html)
        # Fecha de la guía vigente y km/año salen de la base, no del código.
        self.assertIn("14 de octubre de 2026", html)
        self.assertIn("14.000 km por año", html)
        self.assertIn("/static/css/home.css", html)
        font = self.client.get("/static/fonts/big-shoulders-700.woff")
        self.assertEqual(font.status_code, 200)
        self.assertTrue(font.headers["content-type"].startswith("font/woff"))

    def test_logo_and_publicity_use_live_data(self):
        html = self.client.get("/").text
        # Logo de placa en encabezado y pie, dibujado con trazos: no depende de imágenes ni fuentes.
        self.assertIn('aria-label="AutoValor Colombia, inicio"', html)
        self.assertEqual(html.count('id="av-letras"'), 1)
        self.assertGreaterEqual(html.count('class="logo-placa'), 3)
        self.assertIn('role="img" aria-label="AutoValor Colombia"', html)
        # Cinta: el texto se lee una vez; las copias del bucle quedan ocultas, y se puede pausar.
        self.assertIn("Consulta gratis", html)
        self.assertEqual(html.count('<ul class="cinta-lista" aria-hidden="true">'), 3)
        self.assertIn('id="cinta-pausa"', html)
        # Valla: las cifras salen de la base en cada visita.
        brands, models, year_min, year_max = self.conn.execute(
            "SELECT COUNT(DISTINCT m.brand_id), COUNT(DISTINCT v.model_id), MIN(v.model_year), MAX(v.model_year) "
            "FROM vehicle_versions v JOIN models m ON m.id = v.model_id").fetchone()
        self.assertIn(f"<dt>marcas</dt><dd>{brands:,}</dd>".replace(",", "."), html)
        self.assertIn(f"<dt>líneas</dt><dd>{models:,}</dd>".replace(",", "."), html)
        self.assertIn(f"<dd>{year_min}\u2013{year_max}</dd>", html)
        # Sin testimonios ni cifras de usuarios inventadas.
        for invented in ("clientes satisfechos", "testimonio", "★", "usuarios felices", "miles de colombianos"):
            self.assertNotIn(invented, html.lower())
        for path in ("/static/img/logo-autovalor.svg", "/static/img/icono-autovalor.svg", "/static/img/ciudad-noche.svg"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("image/svg+xml", response.headers["content-type"])

    def test_valuation_steps_use_colombian_format(self):
        vid = self.conn.execute(
            """SELECT v.id FROM vehicle_versions v JOIN price_references p ON p.version_id=v.id
               WHERE v.raw_description LIKE '%ONIX LTZ 1.0 T%AUT%' AND v.model_year=2021 AND p.price_cop=53200000""").fetchone()[0]
        d = self.client.post("/api/estimate", json={"version_id": vid, "mileage_km": 120000}).json()
        steps = " ".join(d["valuation"]["steps"])
        self.assertIn("Se parte del valor de referencia", steps)
        self.assertIn("$\u00a053.200.000", steps)
        self.assertRegex(steps, r"−\d+,\d{2}\u00a0%")
        self.assertNotIn("2026-10-14", steps)

    def test_public_page_and_api_do_not_reveal_sources(self):
        """La página y la API pública no dicen de dónde salen los valores; la base sí lo conserva."""
        html = self.client.get("/").text.lower()
        js = (ROOT / "static/js/app.js").read_text(encoding="utf-8").lower()
        for text in (html, js):
            self.assertNotIn("revista", text)
            self.assertNotIn("guía de usados", text)
        item = self.client.get("/api/search", params={"q": "onix 2021 ltz", "limit": 5}).json()[0]
        responses = [
            self.client.get("/api/search", params={"q": "onix 2021", "limit": 20}).text,
            self.client.get("/api/engines", params={"model_id": item["model_id"], "year": item["model_year"]}).text,
            self.client.get("/api/versions", params={"model_id": item["model_id"], "year": item["model_year"],
                                                     "engine_id": item["engine_id"], "transmission_id": item["transmission_id"]}).text,
            self.client.post("/api/estimate", json={"version_id": item["version_id"], "mileage_km": 95000}).text,
        ]
        # Versión sin valor de referencia (solo lista de nuevos) y versión híbrida.
        no_guide = self.conn.execute(
            """SELECT v.id FROM vehicle_versions v WHERE v.data_origin='revista_motor_nuevos'
               AND NOT EXISTS (SELECT 1 FROM price_references p WHERE p.version_id=v.id AND p.price_type IN ('guia_comercial','precio_solicitado'))
               LIMIT 1""").fetchone()[0]
        responses.append(self.client.post("/api/estimate", json={"version_id": no_guide, "mileage_km": 1000}).text)
        hybrid = self.conn.execute("SELECT model_id, model_year FROM vehicle_versions WHERE fuel LIKE '%según categoría%' LIMIT 1").fetchone()
        engines = self.client.get("/api/engines", params={"model_id": hybrid[0], "year": hybrid[1]}).json()
        self.assertIn("Híbrido", [e["fuel"] for e in engines])
        responses.append(str(engines))
        for text in responses:
            low = text.lower()
            self.assertNotIn("revista", low)
            self.assertNotIn("guía", low)
            self.assertNotIn("source", low)
        # La trazabilidad interna sigue completa.
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) FROM price_references WHERE source_ref LIKE 'revista-motor:%'").fetchone()[0], 5000)

    def test_mileage_explanation_is_public(self):
        item = self.client.get("/api/search", params={"q": "onix 2021 ltz", "limit": 5}).json()[0]
        d = self.client.post("/api/estimate", json={"version_id": item["version_id"], "mileage_km": 95000}).json()
        val = d["valuation"]
        self.assertLess(val["km_adjustment_cop"], 0)
        self.assertLess(val["km_adjustment_pct"], 0)
        self.assertEqual(val["value_before_rounding_cop"], val["reference_price_cop"] + val["km_adjustment_cop"])
        self.assertEqual(d["km"]["status"], "superior")
        self.assertEqual(d["km"]["expected_km"], 70000)

    def test_result_extras_history_body_style_and_new_price(self):
        item = self.client.get("/api/search", params={"q": "onix 2021 ltz", "limit": 5}).json()[0]
        d = self.client.post("/api/estimate", json={"version_id": item["version_id"], "mileage_km": 95000}).json()
        years = [p["model_year"] for p in d["history"]]
        self.assertEqual(years, sorted(years))
        by_year = {p["model_year"]: p["value_cop"] for p in d["history"]}
        self.assertEqual(by_year[2021], 53_200_000)
        self.assertEqual(by_year[2024], 64_000_000)
        self.assertIn(d["vehicle"]["body_style"], ("auto", "sedan", "hatchback", "suv", "pickup", "van", "coupe"))
        self.assertTrue(d["vehicle"]["body_style_label"])
        self.assertLessEqual(d["new_price"]["model_min_cop"], d["new_price"]["model_max_cop"])
        curve = d["km"]["curve"]
        for key in ("deadband_km", "scale_km", "penalty_cap_pct", "bonus_cap_pct", "steepness", "rounding_cop"):
            self.assertIn(key, curve)
        # Ruta para enlaces compartidos
        path = self.client.get(f"/api/version/{item['version_id']}").json()
        for key in ("brand_id", "model_id", "model_year", "engine_id", "transmission_id", "version_id"):
            self.assertIn(key, path)
        self.assertTrue(path["valuation_available"])
        self.assertEqual(self.client.get("/api/version/99999999").status_code, 404)
        for text in (str(d["history"]), str(d["new_price"]), str(path)):
            self.assertNotIn("revista", text.lower())

    def test_pickup_is_drawn_as_pickup(self):
        row = self.conn.execute(
            """SELECT v.* , m.name AS model_name FROM vehicle_versions v JOIN models m ON m.id=v.model_id
               WHERE v.raw_description LIKE '%HILUX%DOB%' LIMIT 1""").fetchone()
        self.assertEqual(valuation.body_style(self.conn, dict(row)), "pickup")

    def test_simulator_formula_in_browser_matches_server(self):
        """La fórmula del simulador (app.js) coincide con la del servidor (valuation.py)."""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js no está instalado")
        js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        start = js.index("  function kmAdjustmentPct(")
        end = js.index("\n  }\n", start) + 4
        fn = js[start:end]
        curve = {"deadband_km": 8400.0, "scale_km": 84000.0, "penalty_cap_pct": 15.0, "bonus_cap_pct": 5.0, "steepness": 1.0}
        cfg = {"km_deadband_pct": 10.0, "km_deadband_floor_km": 5000.0, "km_scale_floor": 10000.0,
               "km_penalty_cap_pct": 15.0, "km_bonus_cap_pct": 5.0, "km_curve_steepness": 1.0}
        samples = [0, 20000, 75600, 84000, 92400, 95000, 120000, 200000, 400000]
        script = fn + f"\nconst c={json.dumps(curve)};console.log(JSON.stringify({samples}.map(k=>kmAdjustmentPct(k,84000,c))));"
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        js_values = json.loads(out.stdout)
        for km_value, js_value in zip(samples, js_values):
            self.assertAlmostEqual(js_value, valuation.km_adjustment_pct(km_value, 84000, cfg), places=9)

    def test_no_forbidden_claims(self):
        pages = [self.client.get("/").text, (ROOT / "static/js/app.js").read_text(encoding="utf-8")]
        for t in pages:
            for w in ("garantizado", "valor exacto", "precio exacto"):
                self.assertNotIn(w, t.lower())
        home = pages[0].lower()
        self.assertIn("no está afiliada", home)
        self.assertNotIn("<img", home)


class KmCurveTests(unittest.TestCase):
    cfg = {"km_deadband_pct": 10, "km_deadband_floor_km": 5000, "km_scale_floor": 10000,
           "km_penalty_cap_pct": 15, "km_bonus_cap_pct": 5, "km_curve_steepness": 1.0}

    def test_zero_zone(self):
        for km in (80000, 84000, 88000):
            self.assertEqual(valuation.km_adjustment_pct(km, 84000, self.cfg), 0)

    def test_monotonic_bounded_continuous(self):
        prev = 1e9
        for km in range(0, 600000, 2000):
            v = valuation.km_adjustment_pct(km, 84000, self.cfg)
            self.assertLessEqual(v, prev + 1e-9)
            self.assertLessEqual(v, 5 + 1e-9)
            self.assertGreaterEqual(v, -15 - 1e-9)
            if km:
                self.assertLess(abs(prev - v), 1.0)
            prev = v

    def test_expected_km(self):
        self.assertEqual(valuation.expected_km(6, {"km_expected_per_year": 14000, "km_scale_floor": 10000}), 84000)
