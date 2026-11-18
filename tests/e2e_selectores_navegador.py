"""Prueba de extremo a extremo del formulario en un navegador real (Playwright + Chromium).

Uso: arranque la aplicación (p. ej. `AUTOVALOR_DB=<copia.db> python -m uvicorn app:app --port 8765`) y ejecute
`python tests/e2e_selectores_navegador.py http://127.0.0.1:8765 <carpeta_salida>`. No la ejecuta `unittest discover`.
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
results = []


def check(name, cond, detail=""):
    results.append({"prueba": name, "ok": bool(cond), "detalle": detail})
    print(("OK  " if cond else "FALLA ") + name + (f" — {detail}" if detail else ""))


def options(page, sel):
    return page.eval_on_selector_all(f"#{sel} option", "els => els.map(e => ({v: e.value, t: e.textContent, g: e.parentElement.tagName === 'OPTGROUP' ? e.parentElement.label : ''}))")


def wait_opts(page, sel, min_count=2):
    page.wait_for_function(f"document.querySelectorAll('#{sel} option').length >= {min_count} && !document.getElementById('{sel}').disabled", timeout=15000)


def choose(page, sel, label):
    wait_opts(page, sel)
    page.select_option(f"#{sel}", label=label)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True)  # solo para las esperas del arnés de prueba
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/", wait_until="networkidle")

    # Caso de la captura: Suzuki Vitara 2018 1.6 L
    choose(page, "brand-select", "Suzuki")
    choose(page, "model-select", "Vitara")
    choose(page, "year-select", "2018")
    wait_opts(page, "engine-select")
    engines = [o["t"] for o in options(page, "engine-select")][1:]
    check("Vitara 2018: motores ofrecidos", engines == ["1.6 L"], str(engines))
    page.select_option("#engine-select", label="1.6 L")
    wait_opts(page, "transmission-select")
    trans = options(page, "transmission-select")
    labels = [o["t"] for o in trans]
    check("Vitara 2018 1.6: cajas = Mecánica y Automática", labels == ["Elige la transmisión", "Mecánica", "Automática"], str(labels))
    check("Vitara 2018 1.6: no aparece «No especificada»", not any("No especificada" in l for l in labels))
    page.select_option("#transmission-select", label="Mecánica")
    wait_opts(page, "version-select")
    versions = [o["t"] for o in options(page, "version-select")][1:]
    check("Vitara 2018 mecánica: versiones con 5 velocidades", versions and all("5 velocidades" in v for v in versions), str(versions))
    page.select_option("#version-select", index=1)
    page.fill("#mileage-input", "60000")
    page.click("#btn-estimate")
    page.wait_for_selector("#result-panel .chip", timeout=15000)
    chips = page.eval_on_selector_all("#result-panel .chip", "els => els.map(e => e.textContent)")
    warns = page.inner_text("#result-panel")
    check("Resultado Vitara mecánica: chip de caja verificada", "Caja verificada con ficha técnica" in chips, str(chips))
    check("Resultado Vitara mecánica: sin aviso de caja no confirmada", "transmisión de esta versión no está confirmada" not in warns.lower())
    page.screenshot(path=f"{OUT}/vitara_2018_mecanica_resultado.png", full_page=False)
    page.locator("#transmission-select").scroll_into_view_if_needed()
    page.screenshot(path=f"{OUT}/vitara_2018_selector.png")

    # Cambiar de año no conserva la caja anterior
    page.select_option("#year-select", label="2020")
    page.wait_for_timeout(600)
    tval = page.eval_on_selector("#transmission-select", "e => e.value")
    tdis = page.eval_on_selector("#transmission-select", "e => e.disabled")
    vcount = len(options(page, "version-select"))
    check("Cambiar a 2020 limpia la caja y la versión", tval == "" and tdis and vcount == 1, f"valor={tval!r} deshabilitada={tdis} versiones={vcount}")
    wait_opts(page, "engine-select")
    eng2020 = [o["t"] for o in options(page, "engine-select")][1:]
    check("Vitara 2020: motores del año (todos con cilindrada)", set(eng2020) == {"1.6 L"}, str(eng2020))

    # Un eléctrico se muestra una sola vez como «Eléctrico» (sin repetir el combustible)
    choose(page, "brand-select", "Renault")
    choose(page, "model-select", "Kwid")
    choose(page, "year-select", "2026")
    wait_opts(page, "engine-select")
    kw = [o["t"] for o in options(page, "engine-select")][1:]
    check("Kwid 2026: «Eléctrico» sin repetir", "Eléctrico" in kw and not any("Eléctrico, Eléctrico" in t for t in kw), str(kw))

    # Cambiar de motor actualiza las cajas (Renault Duster 2013)
    choose(page, "brand-select", "Renault")
    choose(page, "model-select", "Duster")
    choose(page, "year-select", "2013")
    choose(page, "engine-select", "1.6 L")
    wait_opts(page, "transmission-select")
    t16 = [o["t"] for o in options(page, "transmission-select")][1:]
    page.select_option("#engine-select", label="2.0 L")
    page.wait_for_function("Array.from(document.querySelectorAll('#transmission-select option')).some(o => o.textContent === 'Automática')", timeout=15000)
    t20 = [o["t"] for o in options(page, "transmission-select")][1:]
    check("Duster 2013: 1.6 solo mecánica; 2.0 mecánica y automática", t16 == ["Mecánica"] and t20 == ["Mecánica", "Automática"], f"1.6={t16} 2.0={t20}")

    # Ya no quedan cajas sin confirmar: BMW 116i 2018 (antes pendiente) muestra solo su caja real
    choose(page, "brand-select", "BMW")
    choose(page, "model-select", "116I")
    choose(page, "year-select", "2018")
    wait_opts(page, "engine-select")
    eng = [o["t"] for o in options(page, "engine-select")][1:]
    page.select_option("#engine-select", label=eng[-1])
    wait_opts(page, "transmission-select")
    tl = options(page, "transmission-select")
    check("BMW 116i 2018: solo Automática y sin grupo «Caja por confirmar»",
          [o["t"] for o in tl][1:] == ["Automática"] and not any(o["g"] == "Caja por confirmar" for o in tl),
          json.dumps(tl, ensure_ascii=False))
    page.locator("#transmission-select").scroll_into_view_if_needed()
    page.screenshot(path=f"{OUT}/bmw_116i_2018_automatica.png")

    # Caso de la captura del usuario: Montero Hard Top 2009 2.4 ya tiene caja confirmada
    choose(page, "brand-select", "Mitsubishi")
    choose(page, "model-select", "Montero Hard Top")
    choose(page, "year-select", "2009")
    choose(page, "engine-select", "2.4 L")
    wait_opts(page, "transmission-select")
    mh = [o["t"] for o in options(page, "transmission-select")][1:]
    check("Montero Hard Top 2009 2.4: solo Mecánica (Fasecolda)", mh == ["Mecánica"], str(mh))

    # Respuesta tardía: cambiar rápido de marca no deja opciones viejas
    page.select_option("#brand-select", label="Suzuki")
    page.select_option("#brand-select", label="Mazda")
    page.wait_for_timeout(1500)
    models = [o["t"] for o in options(page, "model-select")][1:]
    check("Cambio rápido de marca: solo líneas de la última marca", "Vitara" not in models and "3" in models, str(models[:8]))

    check("Consola sin errores de JavaScript", not errors, "; ".join(errors[:3]))
    browser.close()

json.dump(results, open(f"{OUT}/e2e_resultados.json", "w"), ensure_ascii=False, indent=1)
print(f"{sum(r['ok'] for r in results)}/{len(results)} comprobaciones OK")
sys.exit(0 if all(r["ok"] for r in results) else 1)
