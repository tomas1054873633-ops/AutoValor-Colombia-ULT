import tempfile
import re
import unittest
from pathlib import Path

from app import create_app
from fastapi.testclient import TestClient
from db import connect_db
from valuation import estimate_vehicle


class AutoValorAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_autovalor.db"
        self.app = create_app({
            "TESTING": True,
            "DATABASE_PATH": str(self.db_path),
            "SECRET_KEY": "test-secret-key-not-for-production",
            "ADMIN_PASSWORD": "test-admin-password",
            "SEED_DATA": True,
            "SESSION_COOKIE_SECURE": False,
        })
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _version_id(self, brand, model, year, version, transmission):
        conn = connect_db(self.db_path)
        try:
            row = conn.execute(
                """SELECT v.id FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
                   WHERE b.name=? AND m.name=? AND v.model_year=? AND v.version_name=?
                     AND (v.transmission=? OR v.transmission_original=?)""",
                (brand, model, year, version, transmission, transmission),
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def _estimate(self, version_id, mileage):
        """Consulta la API pública y devuelve el resultado interno completo (con referencias) para revisarlo."""
        response = self.client.post("/api/estimate", json={"version_id": version_id, "mileage_km": mileage})
        self.assertEqual(response.status_code, 200)
        public = response.json()
        # La respuesta pública no expone la procedencia de los valores de referencia.
        self.assertNotIn("references", public)
        conn = connect_db(self.db_path)
        try:
            internal = estimate_vehicle(conn, version_id, mileage)
        finally:
            conn.close()
        self.assertEqual(public["estimable"], internal["estimable"])
        if internal["valuation"]:
            self.assertEqual(public["valuation"]["value_cop"], internal["valuation"]["value_cop"])
        else:
            self.assertIsNone(public["valuation"])
        return internal

    def _login_and_csrf(self):
        response = self.client.post("/admin/login", data={"password": "test-admin-password"})
        self.assertEqual(response.status_code, 303)
        panel = self.client.get("/admin")
        self.assertEqual(panel.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', panel.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_health_and_stats(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["brands"], 9)
        self.assertEqual(stats["models"], 21)
        self.assertEqual(stats["versions"], 55)
        self.assertEqual(stats["price_references"], 58)

    def test_dynamic_catalog_filters(self):
        brands = self.client.get("/api/brands?q=Renault").json()
        self.assertEqual(len(brands), 1)
        renault_id = brands[0]["id"]
        models = self.client.get(f"/api/models?brand_id={renault_id}").json()
        # La consulta solo ofrece líneas con al menos una versión que se puede valorar: en este
        # catálogo de prueba la Duster no tiene ninguna referencia suficiente.
        self.assertEqual([m["name"] for m in models], ["Logan"])
        model_id = next(m["id"] for m in models if m["name"] == "Logan")
        years = self.client.get(f"/api/years?model_id={model_id}").json()
        self.assertEqual(years, [2020])
        engines = self.client.get(f"/api/engines?model_id={model_id}&year=2020").json()
        self.assertEqual([e["engine_label"] for e in engines], ["1.6 L gasolina"])
        self.assertTrue(engines[0]["engine_id"])
        engine_id = engines[0]["engine_id"]
        transmissions = self.client.get(f"/api/transmissions?model_id={model_id}&year=2020&engine_id={engine_id}").json()
        transmission_names = {t["transmission"] for t in transmissions}
        # El Logan automático no tiene referencias suficientes: no se ofrece en el formulario
        # (sí sigue en la base y su enlace responde que no hay valoración).
        self.assertEqual(transmission_names, {"Mecánica"})
        manual = next(t for t in transmissions if t["transmission"] == "Mecánica")
        versions = self.client.get(f"/api/versions?model_id={model_id}&year=2020&engine_id={engine_id}&transmission_id={manual['transmission_id']}").json()
        self.assertEqual([v["version_name"] for v in versions], ["1.6 Life"])

    def test_estimation_with_enough_references(self):
        version_id = self._version_id("Renault", "Logan", 2020, "1.6 Life", "Mecánica")
        data = self._estimate(version_id, 90000)
        self.assertTrue(data["estimable"])
        ads = data["references"]["ads"]
        # Solo las seis referencias verificadas y vigentes participan en el cálculo.
        self.assertEqual(ads["used_count"], 6)
        self.assertEqual(ads["excluded"]["no_verificadas"], 5)
        self.assertEqual(ads["raw_median_cop"], 44000000)
        self.assertEqual(data["valuation"]["basis"], "anuncios")
        # 2026 - 2020 = 6 años × 14.000 km/año
        self.assertEqual(data["km"]["expected_km"], 84000)
        self.assertTrue(any("no es una tasación oficial" in w for w in data["warnings"]))
        # La cifra final no es la mediana cruda ni lleva un COP/km fijo: sale de normalizar cada anuncio.
        self.assertNotEqual(data["valuation"]["reference_price_cop"], 44000000)
        self.assertNotIn("confidence", data)
        self.assertNotIn("km_slope_cop_per_km", data["valuation"])

    def test_insufficient_price_references_do_not_generate_price(self):
        version_id = self._version_id("Renault", "Logan", 2020, "1.6 Life Automático", "Automática")
        data = self._estimate(version_id, 60000)
        self.assertFalse(data["estimable"])
        self.assertIsNone(data["valuation"])
        self.assertIn("mínimo requerido: 3", data["reason"])

    def test_invalid_mileage_and_version(self):
        bad_mileage = self.client.post("/api/estimate", json={"version_id": 1, "mileage_km": -1})
        self.assertEqual(bad_mileage.status_code, 400)
        decimal_mileage = self.client.post("/api/estimate", json={"version_id": 1, "mileage_km": 85000.5})
        self.assertEqual(decimal_mileage.status_code, 400)
        invalid_version = self.client.post("/api/estimate", json={"version_id": 99999, "mileage_km": 1000})
        self.assertEqual(invalid_version.status_code, 404)

    def test_database_persists_after_app_reinitialization(self):
        before = self.client.get("/api/stats").json()
        second_app = create_app({
            "TESTING": True,
            "DATABASE_PATH": str(self.db_path),
            "SECRET_KEY": "another-test-secret",
            "ADMIN_PASSWORD": "test-admin-password",
            "SEED_DATA": True,
        })
        after = TestClient(second_app).get("/api/stats").json()
        self.assertEqual(before, after)

    def test_admin_requires_login_and_csrf(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 303)
        csrf = self._login_and_csrf()
        bad = self.client.post("/admin/verify", data={"version_id": "1", "verification_status": "verificado"})
        self.assertEqual(bad.status_code, 400)
        good = self.client.post("/admin/verify", data={"csrf_token": csrf, "version_id": "1", "verification_status": "parcial"})
        self.assertIn(good.status_code, (302, 303))

    def test_security_headers_and_hidden_api_docs(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response.headers.get("content-security-policy", ""))
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        # La documentación automática de la API está apagada por defecto.
        self.assertEqual(self.client.get("/api/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        # El pie de página público no enlaza al panel administrativo.
        self.assertNotIn('href="/admin/login"', response.text)

    def test_admin_login_is_locked_after_repeated_failures(self):
        for _ in range(5):
            self.client.post("/admin/login", data={"password": "incorrecta"})
        # Con el límite alcanzado, ni siquiera la contraseña correcta abre el panel.
        blocked = self.client.post("/admin/login", data={"password": "test-admin-password"})
        self.assertEqual(blocked.status_code, 303)
        self.assertEqual(blocked.headers["location"], "/admin/login")
        page = self.client.get("/admin/login")
        self.assertIn("Demasiados intentos fallidos", page.text)
        self.assertIn('class="logo-placa"', page.text)
        self.assertEqual(self.client.get("/admin").status_code, 303)

    def test_admin_login_with_accented_password_does_not_crash(self):
        # Una contraseña con tildes o eñe debe responder «Contraseña incorrecta», no un error 500.
        r = self.client.post("/admin/login", data={"password": "contraseña-incorrecta"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Contraseña incorrecta", self.client.get("/admin/login").text)

    def test_not_found_page_and_favicon(self):
        page = self.client.get("/esta-pagina-no-existe")
        self.assertEqual(page.status_code, 404)
        self.assertIn("Esta página no existe", page.text)
        # La página 404 conserva el logo de placa con su enlace a la portada.
        self.assertIn('aria-label="AutoValor Colombia, inicio"', page.text)
        self.assertIn('id="av-letras"', page.text)
        api = self.client.get("/api/no-existe")
        self.assertEqual(api.status_code, 404)
        self.assertIn("application/json", api.headers["content-type"])
        icon = self.client.get("/favicon.ico")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.headers["content-type"], "image/png")

    def test_vehicle_csv_import_adds_new_record_and_skips_duplicate(self):
        csrf = self._login_and_csrf()
        csv_content = (
            "brand,model,category,model_year,generation,version_name,engine_label,displacement_cc,fuel,transmission,body_type,traction,horsepower,doors,passengers,technical_source_url,verification_status,notes\n"
            "Honda,Civic,Automóvil,2019,10,2.0 EX,2.0 L gasolina,1997,Gasolina,Mecánica,Sedán,Delantera,158,4,5,https://example.com/ficha,pendiente,Prueba de importación\n"
        ).encode("utf-8")
        first = self.client.post("/admin/import/vehicles", data={"csrf_token": csrf}, files={"file": ("vehiculos.csv", csv_content, "text/csv")})
        self.assertIn(first.status_code, (302, 303))
        backups = list((self.db_path.parent / "backups").glob("test_autovalor_pre_import_*.db"))
        self.assertTrue(backups, "Debe existir una copia de seguridad antes de importar.")
        panel = self.client.get("/admin")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', panel.text).group(1)
        second = self.client.post("/admin/import/vehicles", data={"csrf_token": csrf}, files={"file": ("vehiculos.csv", csv_content, "text/csv")})
        self.assertIn(second.status_code, (302, 303))
        conn = connect_db(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM vehicle_versions v JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id WHERE b.name='Honda' AND m.name='Civic'").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_homepage_and_local_placeholder_render(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Consultar valor de mi vehículo", response.text)
        self.assertNotIn('name="first_registration"', response.text)
        self.assertNotIn('id="first-registration"', response.text)
        self.assertIn('id="brand-select"', response.text)
        self.assertIn('id="model-select"', response.text)
        self.assertNotIn("23 versiones documentadas", response.text)
        self.assertNotIn("Cobertura inicial limitada", response.text)
        self.assertNotIn('list="brand-options"', response.text)
        image = self.client.get("/static/img/vehicle-placeholder.svg")
        self.assertEqual(image.status_code, 200)
        self.assertIn("Imagen representativa", image.text)

    def test_price_csv_import_appends_and_skips_duplicate(self):
        csrf = self._login_and_csrf()
        csv_content = (
            "brand,model,model_year,version_name,engine_label,transmission,price_cop,price_type,source_name,source_url,observed_at,mileage_km,location,verification_status,notes\n"
            "Renault,Logan,2020,1.6 Life,1.6 L gasolina,Mecánica,44000000,precio_solicitado,Fuente de prueba,https://example.com/logan-observation,2026-10-26,80000,Bogotá,observado_no_verificado,Prueba automatizada\n"
        ).encode("utf-8")
        first = self.client.post("/admin/import/prices", data={"csrf_token": csrf}, files={"file": ("precios.csv", csv_content, "text/csv")})
        self.assertEqual(first.status_code, 303)
        second = self.client.post("/admin/import/prices", data={"csrf_token": csrf}, files={"file": ("precios.csv", csv_content, "text/csv")})
        self.assertEqual(second.status_code, 303)
        version_id = self._version_id("Renault", "Logan", 2020, "1.6 Life", "Mecánica")
        conn = connect_db(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM price_references WHERE version_id=?", (version_id,)).fetchone()[0]
            self.assertEqual(count, 12)
        finally:
            conn.close()

    def test_admin_config_is_validated_and_persisted(self):
        csrf = self._login_and_csrf()
        valid = {
            "csrf_token": csrf,
            "km_expected_per_year": "15000", "km_deadband_pct": "12", "km_deadband_floor_km": "4000",
            "km_scale_floor": "10000", "km_penalty_cap_pct": "18", "km_bonus_cap_pct": "6",
            "km_curve_steepness": "1.2", "km_atypical_high_factor": "3", "km_atypical_low_factor": "0.25",
            "minimum_prices": "3", "freshness_days": "90", "guide_max_age_days": "120",
            "guide_vs_ads_warning_pct": "25", "valuation_rounding_cop": "100000",
        }
        response = self.client.post("/admin/config", data=valid)
        self.assertEqual(response.status_code, 303)
        conn = connect_db(self.db_path)
        try:
            values = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM valuation_config").fetchall()}
            self.assertEqual(values["km_expected_per_year"], "15000")
            self.assertEqual(values["km_penalty_cap_pct"], "18.0")
        finally:
            conn.close()
        for field, bad in (("km_penalty_cap_pct", "90"), ("km_expected_per_year", "0"), ("km_curve_steepness", "abc")):
            invalid = dict(valid)
            invalid[field] = bad
            invalid["km_expected_per_year"] = invalid["km_expected_per_year"] if field != "km_expected_per_year" else bad
            self.assertEqual(self.client.post("/admin/config", data=invalid).status_code, 303)
        conn = connect_db(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT value FROM valuation_config WHERE key='km_penalty_cap_pct'").fetchone()[0], "18.0")
            self.assertEqual(conn.execute("SELECT value FROM valuation_config WHERE key='km_expected_per_year'").fetchone()[0], "15000")
            self.assertEqual(conn.execute("SELECT value FROM valuation_config WHERE key='km_curve_steepness'").fetchone()[0], "1.2")
        finally:
            conn.close()

    def test_stale_price_reference_is_excluded_from_estimate(self):
        version_id = self._version_id("Renault", "Logan", 2020, "1.6 Life", "Mecánica")
        conn = connect_db(self.db_path)
        try:
            source_id = conn.execute("SELECT id FROM sources WHERE name='Mercado Libre'").fetchone()[0]
            conn.execute(
                """INSERT INTO price_references(version_id,source_id,price_cop,price_type,model_year,mileage_km,
                   location,source_url,observed_at,verification_status,notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, source_id, 60000000, "precio_solicitado", 2020, 90000, "Bogotá",
                 "https://example.com/stale-logan", "2026-05-01", "verificado", "Referencia antigua de prueba"),
            )
            conn.commit()
        finally:
            conn.close()
        data = self._estimate(version_id, 90000)
        self.assertTrue(data["estimable"])
        self.assertEqual(data["references"]["ads"]["used_count"], 6)
        self.assertEqual(data["references"]["ads"]["excluded"]["antiguas_o_fecha_invalida"], 1)


    def test_seeded_catalog_has_expanded_coverage(self):
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats, {"brands": 9, "models": 21, "versions": 55, "price_references": 58})

    def test_recent_unverified_prices_are_not_used(self):
        # Spark GT LT tiene tres observaciones pendientes; no debe generar precio.
        version_id = self._version_id("Chevrolet", "Spark GT", 2018, "1.2 LT", "Mecánica")
        data = self._estimate(version_id, 80000)
        self.assertFalse(data["estimable"])
        self.assertEqual(data["references"]["ads"]["used_count"], 0)
        self.assertEqual(data["references"]["ads"]["excluded"]["no_verificadas"], 3)
        self.assertIsNone(data["valuation"])

    def test_expired_and_partial_references_do_not_change_price_base(self):
        version_id = self._version_id("Mazda", "3", 2020, "2.0 Touring", "Automática")
        data = self._estimate(version_id, 60000)
        self.assertTrue(data["estimable"])
        ads = data["references"]["ads"]
        self.assertEqual(ads["used_count"], 7)
        self.assertEqual(ads["excluded"]["no_verificadas"], 3)
        self.assertEqual(ads["excluded"]["antiguas_o_fecha_invalida"], 0)
        self.assertEqual(ads["raw_median_cop"], 76500000)

    def test_no_verified_price_for_automatic_logan(self):
        version_id = self._version_id("Renault", "Logan", 2020, "1.6 Life Automático", "Automática")
        data = self._estimate(version_id, 60000)
        self.assertFalse(data["estimable"])
        self.assertEqual(data["references"]["ads"]["used_count"], 0)
        self.assertEqual(data["references"]["ads"]["excluded"]["no_verificadas"], 1)

    def test_catalog_uses_internal_ids_and_rejects_incompatible_filters(self):
        brands = self.client.get("/api/brands").json()
        ford = next(b for b in brands if b["name"] == "Ford")
        models = self.client.get(f"/api/models?brand_id={ford['id']}").json()
        territory = next(m for m in models if m["name"] == "Territory")
        years = self.client.get(f"/api/years?model_id={territory['id']}").json()
        self.assertEqual(years, [2027])
        engines = self.client.get(f"/api/engines?model_id={territory['id']}&year=2027").json()
        self.assertTrue(engines)
        engine_id = engines[0]["engine_id"]
        transmissions = self.client.get(
            f"/api/transmissions?model_id={territory['id']}&year=2027&engine_id={engine_id}"
        ).json()
        self.assertTrue(transmissions)
        # Un ID de motor de otro modelo no puede abrir opciones incompatibles.
        other_brand = next(b for b in brands if b["name"] == "Renault")
        other_model = self.client.get(f"/api/models?brand_id={other_brand['id']}").json()[0]
        other_year = self.client.get(f"/api/years?model_id={other_model['id']}").json()[0]
        wrong = self.client.get(
            f"/api/transmissions?model_id={other_model['id']}&year={other_year}&engine_id={engine_id}"
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(wrong.json(), [])

    def test_vehicle_csv_dry_run_does_not_change_database(self):
        csrf = self._login_and_csrf()
        conn = connect_db(self.db_path)
        try:
            before = conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
        finally:
            conn.close()
        csv_content = (
            "brand,model,category,model_year,generation,version_name,engine_label,displacement_cc,fuel,transmission,body_type,traction,horsepower,doors,passengers,technical_source_url,technical_source_name,technical_source_checked_at,verification_status,notes\n"
            "Honda,Civic,Automóvil,2019,10,2.0 EX,2.0 L gasolina,1997,Gasolina,Mecánica,Sedán,Delantera,158,4,5,https://example.com/ficha,Honda ficha,2026-10-26,pendiente,Prueba de validación\n"
        ).encode("utf-8")
        response = self.client.post(
            "/admin/import/vehicles",
            data={"csrf_token": csrf, "dry_run": "1"},
            files={"file": ("vehiculos.csv", csv_content, "text/csv")},
        )
        self.assertEqual(response.status_code, 303)
        conn = connect_db(self.db_path)
        try:
            after = conn.execute("SELECT COUNT(*) FROM vehicle_versions").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            conn.close()

    def test_admin_quality_reports_require_login_and_download_csv(self):
        denied = self.client.get("/admin/report/coverage.csv")
        self.assertEqual(denied.status_code, 303)
        self._login_and_csrf()
        response = self.client.get("/admin/report/coverage.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("marca", response.text)

    def test_new_vehicle_prices_and_unverified_used_listings_do_not_fake_valuation(self):
        version_id = self._version_id("Kia", "Picanto", 2026, "Zenith", "Mecánica")
        data = self._estimate(version_id, 12000)
        self.assertFalse(data["estimable"])
        self.assertIsNone(data["valuation"])
        self.assertEqual(data["references"]["ads"]["used_count"], 0)
        conn = connect_db(self.db_path)
        try:
            refs = conn.execute("SELECT price_type,verification_status,COUNT(*) n FROM price_references WHERE version_id=? GROUP BY price_type,verification_status", (version_id,)).fetchall()
            self.assertEqual([(r["price_type"],r["verification_status"],r["n"]) for r in refs], [("precio_solicitado","observado_no_verificado",2)])
            ford_id = self._version_id("Ford", "Territory", 2027, "Platinum", "Automática DHT")
            new_prices = conn.execute("SELECT COUNT(*) FROM price_references WHERE version_id=? AND price_type='precio_nuevo'", (ford_id,)).fetchone()[0]
            self.assertEqual(new_prices, 1)
        finally:
            conn.close()

    def test_technical_source_dates_are_not_filled_without_evidence(self):
        conn = connect_db(self.db_path)
        try:
            original = conn.execute(
                """SELECT v.technical_source_checked_at FROM vehicle_versions v
                   JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
                   WHERE b.name='Renault' AND m.name='Logan' AND v.model_year=2020 LIMIT 1"""
            ).fetchone()
            expanded = conn.execute(
                """SELECT v.technical_source_checked_at FROM vehicle_versions v
                   JOIN models m ON m.id=v.model_id JOIN brands b ON b.id=m.brand_id
                   WHERE b.name='Chevrolet' AND m.name='Tracker' AND v.model_year=2027 LIMIT 1"""
            ).fetchone()
            self.assertIsNone(original["technical_source_checked_at"])
            self.assertEqual(expanded["technical_source_checked_at"], "2026-10-26")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
