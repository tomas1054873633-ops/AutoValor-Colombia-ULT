(() => {
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- Almacenamiento local: solo la preferencia de modo claro u oscuro ---------- */
  const storeSet = (key, value) => { try { window.localStorage.setItem(key, value); } catch (e) { /* sin almacenamiento */ } };

  /* ---------- Modo claro / oscuro ---------- */
  const themeToggle = document.getElementById('theme-toggle');
  const prefersDark = () => window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const effectiveTheme = () => document.documentElement.getAttribute('data-theme') || (prefersDark() ? 'dark' : 'light');
  function syncThemeButton() {
    if (!themeToggle) return;
    themeToggle.setAttribute('aria-label', effectiveTheme() === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
  }
  if (themeToggle) {
    syncThemeButton();
    themeToggle.addEventListener('click', () => {
      const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      storeSet('av-theme', next);
      syncThemeButton();
    });
  }

  /* ---------- Los números de la placa giran como un odómetro ---------- */
  function rollIn(container, baseDelay = 0) {
    if (!container) return;
    container.querySelectorAll('.odo').forEach((cell, i) => {
      const strip = cell.firstElementChild;
      const target = `translateY(${-1.04 * Number(cell.dataset.d)}em)`;
      if (reduceMotion) { strip.style.transform = target; return; }
      strip.style.transitionDelay = `${baseDelay + i * 55}ms`;
      requestAnimationFrame(() => requestAnimationFrame(() => { strip.style.transform = target; }));
    });
  }


  const form = document.getElementById('calculator-form');
  if (!form) return;

  const brandSelect = document.getElementById('brand-select');
  const modelSelect = document.getElementById('model-select');
  const yearSelect = document.getElementById('year-select');
  const engineSelect = document.getElementById('engine-select');
  const transmissionSelect = document.getElementById('transmission-select');
  const versionSelect = document.getElementById('version-select');
  const mileageInput = document.getElementById('mileage-input');
  const formMessage = document.getElementById('form-message');
  const resultSection = document.getElementById('result-section');
  const resultPanel = document.getElementById('result-panel');
  const submitButton = document.getElementById('btn-estimate');
  const submitLabel = submitButton.querySelector('span');

  const SUBMIT_TEXT = 'Consultar valor de mi vehículo';

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (value) => Math.round(Number(value)).toLocaleString('es-CO');
  const plain = (value) => String(value ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  // Texto de un motor: etiqueta, combustible y cilindrada. El combustible no se repite si la
  // etiqueta ya lo dice («Eléctrico», «1.6 L turbo híbrido»).
  const engineText = (e) => {
    const fuelWord = plain(e.fuel).split(/\s+/)[0];
    const fuel = e.fuel && !(fuelWord && plain(e.engine_label).includes(fuelWord)) ? e.fuel : '';
    return [e.engine_label, fuel, e.displacement_cc ? `${number(e.displacement_cc)} cc` : ''].filter(Boolean).join(', ');
  };
  const money = (value) => value == null ? 'No disponible' : '$ ' + number(value);
  const km = (value) => value == null ? 'No documentado' : number(value) + ' km';
  const signedPct = (value) => (value > 0 ? '+' : value < 0 ? '−' : '') + Math.abs(Number(value)).toLocaleString('es-CO', {minimumFractionDigits: 1, maximumFractionDigits: 2}) + ' %';
  const signedMoney = (value) => (value > 0 ? '+' : value < 0 ? '−' : '') + '⁠$ ' + number(Math.abs(Number(value)));
  const pctAbs = (value) => Math.abs(Number(value)).toLocaleString('es-CO', {minimumFractionDigits: 1, maximumFractionDigits: 2}) + ' %';
  // Valores grandes en ejes y etiquetas: $ 53,2 M
  const compactMoney = (value) => '$ ' + (Number(value) / 1e6).toLocaleString('es-CO', {maximumFractionDigits: Number(value) >= 1e8 ? 0 : 1}) + ' M';
  const longToday = () => new Date().toLocaleDateString('es-CO', {day: 'numeric', month: 'long', year: 'numeric'});

  async function getJson(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'No fue posible completar la consulta. Revisa la conexión e inténtalo de nuevo.');
    return data;
  }

  function showMessage(message = '', type = '') {
    formMessage.textContent = message;
    formMessage.className = 'inline-message' + (type ? ` ${type}` : '');
  }

  function setOptions(select, values, placeholder, getValue = (v) => v, getLabel = (v) => v) {
    select.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = placeholder;
    select.appendChild(first);
    values.forEach((item) => {
      const option = document.createElement('option');
      option.value = String(getValue(item));
      option.textContent = String(getLabel(item));
      select.appendChild(option);
    });
    select.disabled = values.length === 0;
  }

  let lastResult = null;

  // Cada cambio en una casilla invalida las respuestas que aún no han llegado: así una
  // transmisión o versión de la selección anterior nunca reaparece en una combinación nueva.
  let selectionSeq = 0;
  const nextSelection = () => { selectionSeq += 1; return selectionSeq; };
  const isStale = (seq) => seq !== selectionSeq;

  function hideResult() {
    resultSection.hidden = true;
    resultPanel.innerHTML = '';
    lastResult = null;
  }

  function clearUrl() {
    if (window.location.search) window.history.replaceState(null, '', window.location.pathname);
  }

  function resetAfterBrand() {
    setOptions(modelSelect, [], 'Primero elige una marca');
    resetAfterModel();
  }

  function resetAfterModel() {
    setOptions(yearSelect, [], 'Elige el año');
    setOptions(engineSelect, [], 'Elige el motor');
    setOptions(transmissionSelect, [], 'Elige la transmisión');
    setOptions(versionSelect, [], 'Elige la versión');
    hideResult();
  }

  async function loadBrands() {
    try {
      const brands = await getJson('/api/brands');
      setOptions(brandSelect, brands, brands.length ? 'Elige una marca' : 'No hay marcas en el catálogo', (b) => b.id, (b) => b.name);
      if (!brands.length) showMessage('El catálogo está vacío. Un administrador debe importar vehículos.', 'error');
    } catch (error) {
      setOptions(brandSelect, [], 'No se pudieron cargar las marcas');
      showMessage(error.message, 'error');
    }
  }

  async function handleBrandChange() {
    const seq = nextSelection();
    resetAfterBrand();
    if (!brandSelect.value) {
      showMessage('Elige una marca para continuar.');
      return;
    }
    const models = await getJson(`/api/models?brand_id=${encodeURIComponent(brandSelect.value)}`);
    if (isStale(seq)) return;
    setOptions(modelSelect, models, models.length ? 'Elige la línea o modelo' : 'Sin líneas en el catálogo', (m) => m.id, (m) => m.name);
    if (!models.length) showMessage('Esta marca no tiene líneas en el catálogo.', 'error');
    else showMessage(`${models.length} ${models.length === 1 ? 'línea disponible' : 'líneas disponibles'}.`);
  }

  async function handleModelChange() {
    const seq = nextSelection();
    resetAfterModel();
    if (!brandSelect.value || !modelSelect.value) {
      showMessage('Elige primero una marca y una línea.');
      return;
    }
    const years = await getJson(`/api/years?model_id=${encodeURIComponent(modelSelect.value)}`);
    if (isStale(seq)) return;
    setOptions(yearSelect, years, years.length ? 'Elige el año modelo' : 'Sin años en el catálogo');
    if (!years.length) showMessage('Esta línea no tiene años modelo en el catálogo.', 'error');
    else showMessage('');
  }

  async function handleYearChange() {
    const seq = nextSelection();
    setOptions(engineSelect, [], 'Elige el motor');
    setOptions(transmissionSelect, [], 'Elige la transmisión');
    setOptions(versionSelect, [], 'Elige la versión');
    hideResult();
    showMessage('');
    if (!modelSelect.value || !yearSelect.value) return;
    const engines = await getJson(`/api/engines?model_id=${encodeURIComponent(modelSelect.value)}&year=${encodeURIComponent(yearSelect.value)}`);
    if (isStale(seq)) return;
    setOptions(
      engineSelect, engines, engines.length ? 'Elige el motor' : 'Sin motores para ese año',
      (e) => e.engine_id,
      (e) => engineText(e)
    );
    if (!engines.length) showMessage('No hay motores para esa línea y año.', 'error');
  }

  // Cajas del motor elegido: «Mecánica» y «Automática» (CVT, doble embrague y demás automáticas se
  // agrupan en «Automática»). Las versiones cuya caja no se ha podido confirmar van aparte, en su
  // propio grupo, y nunca se presentan como una caja válida.
  function setTransmissionOptions(list) {
    transmissionSelect.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = list.length ? 'Elige la transmisión' : 'Sin transmisiones para ese motor';
    transmissionSelect.appendChild(first);
    list.filter((t) => t.confirmed).forEach((t) => {
      const option = document.createElement('option');
      option.value = String(t.transmission_id);
      option.textContent = t.label;
      transmissionSelect.appendChild(option);
    });
    const pending = list.filter((t) => !t.confirmed);
    if (pending.length) {
      const group = document.createElement('optgroup');
      group.label = 'Caja por confirmar';
      pending.forEach((t) => {
        const option = document.createElement('option');
        option.value = String(t.transmission_id);
        option.dataset.pending = '1';
        option.textContent = t.label;
        group.appendChild(option);
      });
      transmissionSelect.appendChild(group);
    }
    transmissionSelect.disabled = list.length === 0;
  }

  async function handleEngineChange() {
    const seq = nextSelection();
    setOptions(transmissionSelect, [], 'Elige la transmisión');
    setOptions(versionSelect, [], 'Elige la versión');
    hideResult();
    showMessage('');
    if (!modelSelect.value || !yearSelect.value || !engineSelect.value) return;
    const transmissions = await getJson(
      `/api/transmissions?model_id=${encodeURIComponent(modelSelect.value)}&year=${encodeURIComponent(yearSelect.value)}&engine_id=${encodeURIComponent(engineSelect.value)}`
    );
    if (isStale(seq)) return;
    setTransmissionOptions(transmissions);
    if (!transmissions.length) showMessage('No hay transmisiones para ese motor.', 'error');
  }

  // «Mecánica de 5 velocidades» → «5 velocidades»; «Automática · CVT» → «CVT».
  function transmissionExtra(v) {
    return String(v.transmission_label || '')
      .replace(/^(Mecánica|Automática)( · )?/, '').trim().replace(/^de /, '');
  }

  function versionLabel(v) {
    let label = v.version_name;
    const extra = v.transmission_confirmed ? transmissionExtra(v) : '';
    if (extra) label += ` · ${extra}`;
    if (!v.valuation_available) label += ' (sin valoración disponible)';
    return label;
  }

  async function handleTransmissionChange() {
    const seq = nextSelection();
    setOptions(versionSelect, [], 'Elige la versión');
    hideResult();
    showMessage('');
    if (!modelSelect.value || !yearSelect.value || !engineSelect.value || !transmissionSelect.value) return;
    const versions = await getJson(
      `/api/versions?model_id=${encodeURIComponent(modelSelect.value)}&year=${encodeURIComponent(yearSelect.value)}&engine_id=${encodeURIComponent(engineSelect.value)}&transmission_id=${encodeURIComponent(transmissionSelect.value)}`
    );
    if (isStale(seq)) return;
    setOptions(versionSelect, versions, versions.length ? 'Elige la versión' : 'Sin versiones para esta combinación', (v) => v.id, versionLabel);
    if (!versions.length) showMessage('No hay versiones para esta combinación.', 'error');
    else if (versions.every((v) => !v.transmission_confirmed)) {
      showMessage('La caja de estas versiones no está confirmada: la valoración será una referencia general de la versión, no de una caja específica.');
    }
  }

  /* ---------- Enlaces compartidos: rellenan las casillas con la versión indicada ---------- */
  const brandsReady = loadBrands();

  async function applyVersionPath(it) {
    brandSelect.value = String(it.brand_id);
    await handleBrandChange();
    modelSelect.value = String(it.model_id);
    await handleModelChange();
    yearSelect.value = String(it.model_year);
    await handleYearChange();
    engineSelect.value = String(it.engine_id);
    await handleEngineChange();
    transmissionSelect.value = String(it.transmission_id);
    await handleTransmissionChange();
    versionSelect.value = String(it.version_id);
  }

  async function openVersion(versionId, kmValue) {
    try {
      await brandsReady;
      const item = await getJson(`/api/version/${encodeURIComponent(versionId)}`);
      await applyVersionPath(item);
      mileageInput.value = String(kmValue);
      submitForm();
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  /* ---------- Silueta ilustrativa por tipo de carrocería (dibujo propio, sin marcas) ---------- */
  const CAR_SPECS = {
    sedan:     { x0: 12, xF: 308, rocker: 94, belt: 64, roof: 34, roofR: 104, roofF: 178, cBase: 66, aPil: 214, deck: 60, hood: 68, nose: 78, r: 17, wR: 70, wF: 250 },
    auto:      { x0: 20, xF: 300, rocker: 94, belt: 64, roof: 34, roofR: 94, roofF: 176, cBase: 50, aPil: 210, deck: 56, hood: 68, nose: 78, r: 17, wR: 74, wF: 246 },
    hatchback: { x0: 40, xF: 290, rocker: 94, belt: 63, roof: 33, roofR: 80, roofF: 170, cBase: 44, aPil: 204, deck: 44, hood: 68, nose: 78, r: 17, wR: 88, wF: 238 },
    coupe:     { x0: 12, xF: 308, rocker: 95, belt: 66, roof: 40, roofR: 116, roofF: 172, cBase: 52, aPil: 214, deck: 62, hood: 71, nose: 80, r: 17, wR: 70, wF: 250 },
    suv:       { x0: 18, xF: 302, rocker: 89, belt: 57, roof: 22, roofR: 46, roofF: 188, cBase: 22, aPil: 222, deck: 30, hood: 60, nose: 70, r: 20, wR: 78, wF: 242, rails: true },
    van:       { x0: 14, xF: 306, rocker: 89, belt: 56, roof: 14, roofR: 24, roofF: 214, cBase: 16, aPil: 254, deck: 18, hood: 64, nose: 74, r: 18, wR: 70, wF: 254 },
    pickup:    { x0: 10, xF: 310, rocker: 87, belt: 56, roof: 22, roofR: 132, roofF: 196, cBase: 124, aPil: 230, deck: 56, hood: 60, nose: 70, r: 20, wR: 74, wF: 252, bed: true },
  };

  function roundedPath(points, radii, close = true) {
    const n = points.length;
    const seg = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
    const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    const f = (q) => `${q[0].toFixed(1)} ${q[1].toFixed(1)}`;
    let d = '';
    for (let i = 0; i < n; i++) {
      const prev = points[(i - 1 + n) % n], cur = points[i], next = points[(i + 1) % n];
      const r = radii[i] || 0;
      const a = lerp(cur, prev, r ? Math.min(r / seg(cur, prev), 0.5) : 0);
      const b = lerp(cur, next, r ? Math.min(r / seg(cur, next), 0.5) : 0);
      d += (i === 0 ? `M ${f(a)}` : ` L ${f(a)}`) + (r ? ` Q ${f(cur)} ${f(b)}` : '');
    }
    return d + (close ? ' Z' : '');
  }

  function carSvg(style, id) {
    const p = CAR_SPECS[style] || CAR_SPECS.auto;
    const G = 112;
    const ar = p.r + 4.5;
    const pts = [];
    const rad = [];
    const add = (x, y, r) => { pts.push([x, y]); rad.push(r); };
    add(p.x0 + 4, p.rocker, 6);
    add(p.x0, p.rocker - 12, 6);
    if (p.bed) {
      add(p.x0, p.belt, 3); add(p.cBase - 4, p.belt, 2); add(p.cBase, p.belt - 4, 2); add(p.cBase + 2, p.roof + 4, 8);
    } else {
      add(p.x0 + 1, p.deck + 8, 10); add(p.x0 + 6, p.deck, 8); add(p.cBase, p.deck - 2, 10);
    }
    add(p.roofR, p.roof, 22); add(p.roofF, p.roof + 1, 18); add(p.aPil, p.belt, 10);
    add(p.xF - 18, p.hood, 24); add(p.xF, p.nose, 10); add(p.xF - 2, p.rocker - 8, 6); add(p.xF - 10, p.rocker, 4);
    const by = p.rocker;
    const body = roundedPath(pts, rad, false)
      + ` L ${p.wF + ar} ${by} A ${ar} ${ar} 0 0 0 ${p.wF - ar} ${by} L ${p.wR + ar} ${by} A ${ar} ${ar} 0 0 0 ${p.wR - ar} ${by} Z`;
    const gx0 = p.bed ? p.cBase + 7 : p.cBase + 9;
    const glassPts = [[gx0, p.belt - 3], [p.bed ? p.cBase + 9 : (p.roofR + gx0) / 2 - 2, p.bed ? p.roof + 7 : (p.roof + p.belt) / 2 - 1],
      [p.roofR + 3, p.roof + 5], [p.roofF - 2, p.roof + 6], [p.aPil - 11, p.belt - 3]];
    const glass = roundedPath(glassPts, [3, 6, 12, 10, 3]);
    const bx = p.bed ? (p.cBase + p.aPil) / 2 - 6 : (p.roofR + p.roofF) / 2 + 2;
    const cy = G - p.r;
    const wheel = (cx) => `<circle cx="${cx}" cy="${cy}" r="${p.r}" class="car-tire"/><circle cx="${cx}" cy="${cy}" r="${(p.r * 0.6).toFixed(1)}" fill="url(#${id}-r)"/><circle cx="${cx}" cy="${cy}" r="${(p.r * 0.17).toFixed(1)}" class="car-hub"/>`;
    const rails = p.rails ? `<rect x="${p.roofR + 14}" y="${p.roof - 4}" width="${p.roofF - p.roofR - 30}" height="3" rx="1.5" class="car-rail"/>` : '';
    return `<svg viewBox="0 0 320 122" class="car-svg" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="${id}-b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="car-top"/><stop offset="0.55" class="car-mid"/><stop offset="1" class="car-bottom"/></linearGradient>
        <linearGradient id="${id}-g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" class="car-glass1"/><stop offset="1" class="car-glass2"/></linearGradient>
        <radialGradient id="${id}-r" cx="0.42" cy="0.38" r="0.7"><stop offset="0" class="car-rim1"/><stop offset="1" class="car-rim2"/></radialGradient>
        <radialGradient id="${id}-s" cx="0.5" cy="0.5" r="0.5"><stop offset="0" class="car-shadow1"/><stop offset="1" class="car-shadow2"/></radialGradient>
        <clipPath id="${id}-c"><rect x="0" y="0" width="320" height="${by}"/></clipPath>
      </defs>
      <ellipse cx="160" cy="${G + 1}" rx="152" ry="7" fill="url(#${id}-s)"/>
      <g clip-path="url(#${id}-c)"><circle cx="${p.wR}" cy="${cy}" r="${ar + 1}" class="car-well"/><circle cx="${p.wF}" cy="${cy}" r="${ar + 1}" class="car-well"/></g>
      ${rails}
      <path d="${body}" fill="url(#${id}-b)"/>
      <path d="${glass}" fill="url(#${id}-g)"/>
      <rect x="${bx.toFixed(1)}" y="${p.roof + 4}" width="5" height="${p.belt - p.roof - 6}" class="car-pillar"/>
      <path d="M ${p.x0 + 6} ${p.belt + 6} L ${p.aPil + 2} ${p.belt + 5}" class="car-crease"/>
      <path d="M ${(bx + 2.5).toFixed(1)} ${p.belt} L ${(bx + 2.5).toFixed(1)} ${p.rocker - 9}" class="car-line"/>
      <rect x="${(bx - 14).toFixed(1)}" y="${p.belt + 9}" width="9" height="2.6" rx="1.3" class="car-handle"/>
      <path d="M ${p.xF - 15} ${p.nose - 7} Q ${p.xF - 3} ${p.nose - 7} ${p.xF - 1} ${p.nose - 2} L ${p.xF - 13} ${p.nose - 2} Z" class="car-light"/>
      <rect x="${p.x0 - 0.5}" y="${(p.bed ? p.belt : p.deck + 9) + 2}" width="4.5" height="9" rx="2" class="car-tail"/>
      ${wheel(p.wR)}${wheel(p.wF)}
    </svg>`;
  }

  /* ---------- Resultado ---------- */
  function plateHtml(value) {
    const text = number(value);
    const cells = [...text].map((ch) => {
      if (/\d/.test(ch)) {
        const strip = Array.from({length: 10}, (_, i) => `<span>${i}</span>`).join('');
        return `<span class="odo" data-d="${ch}"><span class="odo-strip">${strip}</span></span>`;
      }
      return `<span class="sep">${escapeHtml(ch)}</span>`;
    }).join('');
    return `<div class="plate" role="img" aria-label="${escapeHtml(text)} pesos colombianos">
      <div class="plate-value" aria-hidden="true"><span class="sym">$</span>${cells}</div>
      <p class="plate-caption" aria-hidden="true">Valoración orientativa en pesos</p>
    </div>`;
  }

  function niceMax(value) {
    const step = value > 200000 ? 50000 : 10000;
    return Math.ceil(value / step) * step;
  }

  function ageText(years) {
    const y = Number(years) || 0;
    if (y < 1) return 'menos de un año';
    return `${y} ${y === 1 ? 'año' : 'años'}`;
  }

  // Frase que responde por qué y cuánto cambia el valor por el kilometraje.
  function kmReason(k, val) {
    const diff = number(Math.abs(Number(k.difference_km) || 0));
    const age = ageText(k.age_years);
    if (k.status === 'esperado') {
      const base = `Tu kilometraje está dentro de lo esperado para un carro de ${age}`;
      return val ? `${base}, así que el valor no cambia por kilometraje.` : `${base}.`;
    }
    const more = k.status === 'superior';
    const base = `Tu carro tiene ${diff} km ${more ? 'más' : 'menos'} de lo esperado para un carro de ${age}.`;
    if (!val || !val.km_adjustment_cop) return base;
    return `${base} Por eso el valor ${more ? 'baja' : 'sube'} ${pctAbs(val.km_adjustment_pct)}: ${signedMoney(val.km_adjustment_cop)}.`;
  }

  function renderKm(k, val) {
    const zone = Number(k.no_adjustment_zone_km) || 0;
    const expected = Number(k.expected_km) || 0;
    const mileage = Number(k.mileage_km) || 0;
    const max = niceMax(Math.max(expected * 2, mileage * 1.15, expected + zone * 2, 20000));
    const pos = (v) => Math.min(100, Math.max(0, (v / max) * 100));
    const zoneFrom = Math.max(0, expected - zone);
    const zoneTo = expected + zone;
    const adj = Number(k.adjustment_pct) || 0;
    const adjClass = adj < 0 ? 'negative' : (adj > 0 ? 'positive' : '');
    return `<section class="km-block" aria-labelledby="km-title">
      <h3 id="km-title">${val ? 'Por qué cambia el valor' : 'Tu kilometraje frente al esperado'}</h3>
      <p class="km-reason">${escapeHtml(kmReason(k, val))}</p>
      <div class="km-ruler" role="img" aria-label="Tu carro tiene ${escapeHtml(km(mileage))}; lo esperado para su edad es ${escapeHtml(km(expected))}; no hay ajuste entre ${escapeHtml(km(zoneFrom))} y ${escapeHtml(km(zoneTo))}.">
        <span class="km-zone" style="left:${pos(zoneFrom)}%;width:${pos(zoneTo) - pos(zoneFrom)}%"></span>
        <span class="km-expected" style="left:${pos(expected)}%"></span>
        <span class="km-marker" style="left:${pos(mileage)}%"><span class="km-flag">Tu carro</span></span>
      </div>
      <div class="km-scale" aria-hidden="true"><span>0 km</span><span>${escapeHtml(km(max))}</span></div>
      <ul class="km-legend">
        <li><span class="sw sw-car" aria-hidden="true"></span>Tu carro: ${escapeHtml(km(mileage))}</li>
        <li><span class="sw sw-exp" aria-hidden="true"></span>Esperado: ${escapeHtml(km(expected))}</li>
        <li><span class="sw sw-zone" aria-hidden="true"></span>Sin ajuste entre ${escapeHtml(number(zoneFrom))} y ${escapeHtml(km(zoneTo))}</li>
      </ul>
      <dl class="figures">
        <div><dt>Diferencia</dt><dd>${k.difference_km === 0 ? 'Ninguna' : escapeHtml((k.difference_km > 0 ? '+' : '−') + km(Math.abs(k.difference_km)))}</dd></div>
        <div><dt>Edad del vehículo</dt><dd>${escapeHtml(ageText(k.age_years))}, a ${escapeHtml(number(k.expected_km_per_year))} km por año</dd></div>
        <div><dt>Ajuste por kilometraje</dt><dd class="${adjClass}">${adj === 0 ? 'Sin ajuste' : escapeHtml(signedPct(adj))}</dd></div>
      </dl>
    </section>`;
  }

  function renderNewContext(np, val, v) {
    if (!np) return '';
    if (np.same_version_cop && val && val.value_cop <= np.same_version_cop) {
      const pct = Math.round((100 * val.value_cop) / np.same_version_cop);
      return `<p class="new-context">La misma versión nueva cuesta <strong>${escapeHtml(money(np.same_version_cop))}</strong>. Tu carro conserva cerca del <strong>${pct} %</strong> de ese valor.</p>`;
    }
    const range = np.model_min_cop === np.model_max_cop
      ? escapeHtml(money(np.model_min_cop))
      : `entre ${escapeHtml(money(np.model_min_cop))} y ${escapeHtml(money(np.model_max_cop))}`;
    return `<p class="new-context">Un ${escapeHtml(v.brand_name)} ${escapeHtml(v.model_name)} nuevo cuesta ${range}, según la versión.</p>`;
  }

  function renderValuation(data) {
    const val = data.valuation;
    if (!data.estimable || !val) {
      return `<div class="no-value">
        <h3>No hay base suficiente para valorar esta versión</h3>
        <p>${escapeHtml(data.reason || 'No hay un valor de referencia para esta versión y año.')}</p>
        <p>Prueba con un año o una versión cercana. No rellenamos datos faltantes con cifras inventadas.</p>
      </div>`;
    }
    const adj = Number(val.km_adjustment_cop) || 0;
    const adjClass = adj < 0 ? 'negative' : (adj > 0 ? 'positive' : '');
    const adjLabel = adj === 0 ? 'Ajuste por kilometraje' : `Ajuste por kilometraje (${signedPct(val.km_adjustment_pct)})`;
    return `<p class="valuation-label">Valoración AutoValor</p>
      <div class="plate-wrap">${plateHtml(val.value_cop)}</div>
      <dl class="breakdown">
        <div><dt>Valor de referencia para esta versión y año</dt><dd>${escapeHtml(money(val.reference_price_cop))}</dd></div>
        <div><dt>${escapeHtml(adjLabel)}</dt><dd class="${adjClass}">${adj === 0 ? 'Sin ajuste' : escapeHtml(signedMoney(adj))}</dd></div>
        <div class="breakdown-total"><dt>Valoración, redondeada a ${escapeHtml(money(val.rounding_cop))}</dt><dd>${escapeHtml(money(val.value_cop))}</dd></div>
      </dl>
      ${renderNewContext(data.new_price, val, data.vehicle || {})}`;
  }

  function renderSteps(val) {
    if (!val || !(val.steps || []).length) return '';
    return `<section class="block" aria-labelledby="steps-title">
      <h3 class="block-title" id="steps-title">Cómo se calculó</h3>
      <ol class="steps">${val.steps.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ol>
    </section>`;
  }

  function renderWarnings(warnings, estimable) {
    if (!warnings || !warnings.length) return '';
    const title = estimable ? 'Antes de usar esta cifra' : 'Ten en cuenta';
    return `<aside class="callout" aria-labelledby="warn-title"><h3 id="warn-title">${title}</h3><ul>${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul></aside>`;
  }

  /* ---------- Simulador: misma fórmula del servidor ---------- */
  function roundHalfEven(x) {
    const r = Math.round(x);
    return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
  }
  function kmAdjustmentPct(mileage, expected, curve) {
    const d = mileage - expected;
    const excess = Math.abs(d) - curve.deadband_km;
    if (excess <= 0) return 0;
    const r = excess / curve.scale_km;
    return d > 0 ? -curve.penalty_cap_pct * Math.tanh(curve.steepness * r) : curve.bonus_cap_pct * Math.tanh(curve.steepness * r);
  }
  function simulatedValue(data, mileage) {
    const val = data.valuation;
    const curve = data.km.curve;
    const pct = kmAdjustmentPct(mileage, Number(data.km.expected_km), curve);
    const raw = val.reference_price_cop * (1 + pct / 100);
    const step = curve.rounding_cop > 1 ? curve.rounding_cop : 1;
    return { pct, raw, value: roundHalfEven(raw / step) * step };
  }

  function odometerHtml() {
    const strip = Array.from({length: 10}, (_, i) => `<span>${i}</span>`).join('');
    const cell = `<span class="odo" data-d="0"><span class="odo-strip">${strip}</span></span>`;
    return `${cell}${cell}${cell}<span class="sep">.</span>${cell}${cell}${cell}<span class="unit">km</span>`;
  }
  function setOdometer(el, mileage) {
    const digits = String(Math.max(0, Math.min(999999, Math.round(mileage)))).padStart(6, '0');
    el.querySelectorAll('.odo').forEach((cell, i) => {
      cell.dataset.d = digits[i];
      cell.firstElementChild.style.transform = `translateY(${-1.04 * Number(digits[i])}em)`;
    });
  }

  function renderSimulator(data) {
    if (!data.estimable || !data.valuation || !data.km || !data.km.curve) return '';
    return `<section class="block simulator" aria-labelledby="sim-title">
      <h3 class="block-title" id="sim-title">Simula otro kilometraje</h3>
      <p class="block-intro">Mueve el control o la gráfica para ver cómo cambiaría la valoración con otro kilometraje. Tu consulta no cambia.</p>
      <div class="sim-top">
        <div><span class="sim-label">Kilometraje simulado</span><div class="odometer" id="sim-odo" aria-hidden="true">${odometerHtml()}</div></div>
        <div class="sim-value"><span class="sim-label">Valoración simulada</span><strong id="sim-value"></strong><span id="sim-adj" class="sim-adj"></span></div>
        <button type="button" class="button-line sim-reset" id="sim-reset">Volver a mi kilometraje</button>
      </div>
      <div class="sim-range-wrap">
        <input type="range" class="sim-range" id="sim-range" min="0" step="1000" aria-label="Kilometraje simulado">
        <div class="sim-scale" aria-hidden="true"><span>0 km</span><span id="sim-max"></span></div>
      </div>
      <figure class="chart" id="sim-chart" aria-label="Gráfica de la valoración según el kilometraje"></figure>
      <details class="table-view"><summary>Ver como tabla</summary><div id="sim-table"></div></details>
    </section>`;
  }

  function svgEl(width, height, inner) {
    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="presentation">${inner}</svg>`;
  }

  function niceStep(span, maxTicks) {
    const raw = span / maxTicks;
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const steps = [1, 2, 2.5, 5, 10];
    for (const s of steps) if (s * pow >= raw) return s * pow;
    return 10 * pow;
  }

  function setupSimulator(data) {
    const section = resultPanel.querySelector('.simulator');
    if (!section) return;
    const range = section.querySelector('#sim-range');
    const odo = section.querySelector('#sim-odo');
    const valueEl = section.querySelector('#sim-value');
    const adjEl = section.querySelector('#sim-adj');
    const chart = section.querySelector('#sim-chart');
    const actual = Number(data.mileage_km);
    const expected = Number(data.km.expected_km);
    const maxKm = niceMax(Math.max(expected * 2.5, actual * 1.4, 60000));
    range.max = String(maxKm);
    range.value = String(Math.min(maxKm, Math.round(actual / 1000) * 1000));
    section.querySelector('#sim-max').textContent = km(maxKm);

    // Tabla equivalente a la gráfica
    const stepKm = niceStep(maxKm, 10);
    const rows = [];
    for (let m = 0; m <= maxKm; m += stepKm) {
      const s = simulatedValue(data, m);
      rows.push(`<tr${Math.abs(m - expected) < stepKm / 2 ? ' class="is-current"' : ''}><td>${escapeHtml(km(m))}</td><td>${escapeHtml(signedPct(s.pct))}</td><td>${escapeHtml(money(s.value))}</td></tr>`);
    }
    section.querySelector('#sim-table').innerHTML = `<table><thead><tr><th scope="col">Kilometraje</th><th scope="col">Ajuste</th><th scope="col">Valoración</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;

    let geom = null;
    function draw() {
      const width = Math.max(280, Math.round(chart.clientWidth || 600));
      const height = width < 480 ? 220 : 260;
      const m = { l: 62, r: 14, t: 26, b: 34 };
      const pw = width - m.l - m.r;
      const ph = height - m.t - m.b;
      const R = data.valuation.reference_price_cop;
      const yMin = R * (1 - data.km.curve.penalty_cap_pct / 100) * 0.985;
      const yMax = R * (1 + data.km.curve.bonus_cap_pct / 100) * 1.015;
      const x = (v) => m.l + (v / maxKm) * pw;
      const y = (v) => m.t + (1 - (v - yMin) / (yMax - yMin)) * ph;
      const step = niceStep(yMax - yMin, 4);
      let grid = '';
      for (let t = Math.ceil(yMin / step) * step; t <= yMax; t += step) {
        grid += `<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/><text x="${m.l - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${escapeHtml(compactMoney(t))}</text>`;
      }
      const xStep = niceStep(maxKm, width < 480 ? 3 : 6);
      let xAxis = '';
      for (let t = 0; t <= maxKm; t += xStep) {
        xAxis += `<text x="${x(t).toFixed(1)}" y="${height - 10}" text-anchor="middle">${escapeHtml(t === 0 ? '0' : number(t / 1000) + ' mil')}</text>`;
      }
      const zone = Number(data.km.curve.deadband_km);
      const zFrom = Math.max(0, expected - zone), zTo = Math.min(maxKm, expected + zone);
      let line = '';
      let area = '';
      const n = 160;
      for (let i = 0; i <= n; i++) {
        const mk = (maxKm * i) / n;
        const v = simulatedValue(data, mk).raw;
        line += `${i === 0 ? 'M' : 'L'} ${x(mk).toFixed(1)} ${y(v).toFixed(1)} `;
      }
      area = `${line} L ${x(maxKm).toFixed(1)} ${m.t + ph} L ${x(0).toFixed(1)} ${m.t + ph} Z`;
      const realY = y(simulatedValue(data, actual).raw);
      const inner = `
        <rect class="zone" x="${x(zFrom).toFixed(1)}" y="${m.t}" width="${(x(zTo) - x(zFrom)).toFixed(1)}" height="${ph}"/>
        ${grid}
        <line class="baseline" x1="${m.l}" x2="${width - m.r}" y1="${m.t + ph}" y2="${m.t + ph}"/>
        <line class="expected" x1="${x(expected).toFixed(1)}" x2="${x(expected).toFixed(1)}" y1="${m.t}" y2="${m.t + ph}"/>
        <text x="${x(expected).toFixed(1)}" y="${m.t - 8}" text-anchor="middle">Esperado</text>
        <path class="area" d="${area}"/>
        <path class="curve" d="${line}"/>
        ${actual <= maxKm ? `<circle class="marker-real" cx="${x(actual).toFixed(1)}" cy="${realY.toFixed(1)}" r="6"/>` : ''}
        <line class="marker-line" id="sim-vline" x1="0" x2="0" y1="${m.t}" y2="${m.t + ph}"/>
        <circle class="marker" id="sim-dot" r="5.5" cx="0" cy="0"/>
        <rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent" id="sim-hit" style="cursor:crosshair"/>
        ${xAxis}`;
      chart.innerHTML = svgEl(width, height, inner);
      geom = { x, y, m, pw, width };
      const hit = chart.querySelector('#sim-hit');
      const fromPointer = (event) => {
        const rect = chart.querySelector('svg').getBoundingClientRect();
        const px = (event.clientX - rect.left) * (width / rect.width);
        const mk = Math.round(Math.min(maxKm, Math.max(0, ((px - m.l) / pw) * maxKm)) / 1000) * 1000;
        range.value = String(mk);
        update();
      };
      hit.addEventListener('pointermove', fromPointer);
      hit.addEventListener('pointerdown', fromPointer);
      update();
    }

    function update() {
      const mk = Number(range.value);
      const s = simulatedValue(data, mk);
      setOdometer(odo, mk);
      valueEl.textContent = money(s.value);
      adjEl.textContent = s.pct === 0 ? 'Sin ajuste por kilometraje' : `${signedPct(s.pct)} por kilometraje`;
      adjEl.className = 'sim-adj ' + (s.pct < 0 ? 'negative' : s.pct > 0 ? 'positive' : '');
      range.style.setProperty('--fill', `${(mk / maxKm) * 100}%`);
      range.setAttribute('aria-valuetext', `${km(mk)}: valoración ${money(s.value)}`);
      if (geom) {
        const cx = geom.x(mk).toFixed(1);
        const cy = geom.y(s.raw).toFixed(1);
        const vline = chart.querySelector('#sim-vline');
        const dot = chart.querySelector('#sim-dot');
        if (vline) { vline.setAttribute('x1', cx); vline.setAttribute('x2', cx); }
        if (dot) { dot.setAttribute('cx', cx); dot.setAttribute('cy', cy); }
      }
    }

    range.addEventListener('input', update);
    section.querySelector('#sim-reset').addEventListener('click', () => { range.value = String(Math.min(maxKm, Math.round(actual / 1000) * 1000)); update(); range.focus(); });
    draw();
    return draw;
  }

  /* ---------- Gráfica: valor por año modelo ---------- */
  function renderHistory(data) {
    const points = data.history || [];
    if (points.length < 2) return '';
    return `<section class="block" aria-labelledby="hist-title">
      <h3 class="block-title" id="hist-title">Cómo cambia el valor según el año</h3>
      <p class="block-intro">Valor de referencia de la misma versión en cada año modelo, sin ajuste por kilometraje.</p>
      <figure class="chart" id="hist-chart" aria-label="Gráfica de columnas del valor de referencia por año modelo"></figure>
      <details class="table-view"><summary>Ver como tabla</summary><div id="hist-table"></div></details>
    </section>`;
  }

  function setupHistory(data) {
    const chart = resultPanel.querySelector('#hist-chart');
    if (!chart) return null;
    const points = data.history;
    const current = Number((data.vehicle || {}).model_year);
    const newRef = data.new_price && data.new_price.same_version_cop ? Number(data.new_price.same_version_cop) : null;
    resultPanel.querySelector('#hist-table').innerHTML = `<table><thead><tr><th scope="col">Año modelo</th><th scope="col">Valor de referencia</th></tr></thead><tbody>${
      points.map((p) => `<tr${p.model_year === current ? ' class="is-current"' : ''}><td>${p.model_year}</td><td>${escapeHtml(money(p.value_cop))}</td></tr>`).join('')
    }${newRef ? `<tr><td>Nueva (misma versión)</td><td>${escapeHtml(money(newRef))}</td></tr>` : ''}</tbody></table>`;

    const tooltip = document.createElement('div');
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;

    function draw() {
      const width = Math.max(280, Math.round(chart.clientWidth || 600));
      const height = width < 480 ? 220 : 250;
      const m = { l: 62, r: 14, t: 28, b: 30 };
      const pw = width - m.l - m.r;
      const ph = height - m.t - m.b;
      const maxVal = Math.max(...points.map((p) => p.value_cop), newRef || 0);
      const step = niceStep(maxVal, 4);
      const yMax = Math.ceil((maxVal * 1.08) / step) * step;
      const y = (v) => m.t + (1 - v / yMax) * ph;
      const band = pw / points.length;
      const barW = Math.max(6, Math.min(24, band * 0.62));
      const labelEvery = Math.max(1, Math.ceil(38 / band));
      let grid = '';
      for (let t = 0; t <= yMax; t += step) {
        grid += `<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/><text x="${m.l - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${escapeHtml(t === 0 ? '$ 0' : compactMoney(t))}</text>`;
      }
      let bars = '';
      points.forEach((p, i) => {
        const cx = m.l + band * (i + 0.5);
        const x0 = cx - barW / 2, x1 = cx + barW / 2;
        const top = y(p.value_cop), base = m.t + ph;
        const r = Math.min(4, (base - top) / 2, barW / 2);
        const isCurrent = p.model_year === current;
        const d = `M ${x0.toFixed(1)} ${base} L ${x0.toFixed(1)} ${(top + r).toFixed(1)} Q ${x0.toFixed(1)} ${top.toFixed(1)} ${(x0 + r).toFixed(1)} ${top.toFixed(1)} L ${(x1 - r).toFixed(1)} ${top.toFixed(1)} Q ${x1.toFixed(1)} ${top.toFixed(1)} ${x1.toFixed(1)} ${(top + r).toFixed(1)} L ${x1.toFixed(1)} ${base} Z`;
        const showLabel = isCurrent || i % labelEvery === 0 || i === points.length - 1;
        bars += `<g class="bar-group" data-i="${i}">
          <path class="bar${isCurrent ? ' is-current' : ''}" d="${d}"/>
          <rect class="bar-hit" x="${(cx - band / 2).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}" height="${ph}" tabindex="0" aria-label="${p.model_year}: ${escapeHtml(money(p.value_cop))}"/>
          ${isCurrent ? `<text class="label-strong" x="${cx.toFixed(1)}" y="${(top - 8).toFixed(1)}" text-anchor="middle">${escapeHtml(compactMoney(p.value_cop))}</text>` : ''}
          ${showLabel ? `<text ${isCurrent ? 'class="label-strong" ' : ''}x="${cx.toFixed(1)}" y="${height - 9}" text-anchor="middle">${p.model_year}</text>` : ''}
        </g>`;
      });
      const ref = newRef ? `<line class="ref-line" x1="${m.l}" x2="${width - m.r}" y1="${y(newRef).toFixed(1)}" y2="${y(newRef).toFixed(1)}"/>
        <text x="${width - m.r}" y="${(y(newRef) - 6).toFixed(1)}" text-anchor="end">Nueva: ${escapeHtml(compactMoney(newRef))}</text>` : '';
      chart.innerHTML = svgEl(width, height, `${grid}<line class="baseline" x1="${m.l}" x2="${width - m.r}" y1="${m.t + ph}" y2="${m.t + ph}"/>${ref}${bars}`);
      chart.appendChild(tooltip);
      chart.querySelectorAll('.bar-group').forEach((g) => {
        const p = points[Number(g.dataset.i)];
        const show = () => {
          const svg = chart.querySelector('svg');
          const rect = svg.getBoundingClientRect();
          const scale = rect.width / width;
          const i = Number(g.dataset.i);
          tooltip.innerHTML = `<strong>${escapeHtml(money(p.value_cop))}</strong>Año modelo ${p.model_year}`;
          tooltip.style.left = `${(m.l + band * (i + 0.5)) * scale}px`;
          tooltip.style.top = `${(y(p.value_cop) - (p.model_year === current ? 20 : 6)) * scale}px`;
          tooltip.hidden = false;
        };
        const hide = () => { tooltip.hidden = true; };
        g.addEventListener('pointerenter', show);
        g.addEventListener('pointerleave', hide);
        g.querySelector('.bar-hit').addEventListener('focus', show);
        g.querySelector('.bar-hit').addEventListener('blur', hide);
      });
    }
    draw();
    return draw;
  }

  /* ---------- Compartir ---------- */
  function shareText(data) {
    const v = data.vehicle || {};
    const name = [v.brand_name, v.model_name, v.model_year, v.version_name].filter(Boolean).join(' ');
    const value = data.valuation ? money(data.valuation.value_cop) : 'sin valoración disponible';
    return `${name} con ${km(data.mileage_km)}: ${value} (valoración orientativa de AutoValor Pro).`;
  }

  function renderActions(data) {
    const text = `${shareText(data)} ${window.location.href}`;
    const icon = (path) => `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    return `<div class="result-actions">
      <a class="button-line is-strong" href="https://wa.me/?text=${encodeURIComponent(text)}" target="_blank" rel="noopener noreferrer">${icon('<path d="M4 20l1.3-3.9A8 8 0 1 1 8 19z"/>')}Compartir por WhatsApp</a>
      <button type="button" class="button-line" id="copy-summary">${icon('<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>')}Copiar resumen</button>
      <button type="button" class="button-line" id="print-result">${icon('<path d="M7 9V4h10v5M7 17H5a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-2"/><rect x="7" y="14" width="10" height="6" rx="1"/>')}Imprimir o guardar PDF</button>
      <button type="button" class="button-line" id="new-query">Hacer otra consulta</button>
      <span class="share-status" id="share-status" role="status" aria-live="polite"></span>
    </div>`;
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'absolute';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }

  /* ---------- Pintar el resultado ---------- */
  let redrawers = [];

  function renderResult(data) {
    const v = data.vehicle || {};
    const vehicleName = [v.brand_name, v.model_name, v.model_year, v.version_name].filter(Boolean).join(' ');
    const tech = [engineText({engine_label: v.engine_label, fuel: v.fuel}), v.transmission_label || v.transmission, v.traction].filter(Boolean).join(', ');
    const verified = v.verification_status === 'verificado';
    const gearboxChip = v.transmission_verified ? ' chip-verificado' : (v.transmission_confirmed ? '' : ' chip-pendiente');
    const fichaText = verified ? 'Ficha técnica verificada' : (v.transmission_verified ? 'Resto de la ficha sin verificar' : 'Ficha técnica sin verificar');
    const style = v.body_style || 'auto';

    resultPanel.innerHTML = `
      <p class="print-only">AutoValor Pro Colombia. Valoración orientativa consultada el ${escapeHtml(longToday())}.</p>
      <header class="result-head">
        <div>
          <h2>${escapeHtml(vehicleName)}</h2>
          <p class="result-tech">${escapeHtml(tech || 'Características técnicas parciales')}</p>
          <span class="chip${gearboxChip}">${escapeHtml(v.transmission_status_label || 'Caja sin confirmar')}</span>
          <span class="chip${verified ? ' chip-verificado' : ''}">${fichaText}</span>
        </div>
        <figure class="result-car">${carSvg(style, 'rc')}<figcaption>Silueta ilustrativa: ${escapeHtml(v.body_style_label || 'Automóvil')}</figcaption></figure>
      </header>
      <div class="result-main">
        <div class="valuation">${renderValuation(data)}</div>
        ${renderKm(data.km || {}, data.estimable ? data.valuation : null)}
      </div>
      ${renderSimulator(data)}
      ${renderHistory(data)}
      ${data.estimable ? renderSteps(data.valuation) : ''}
      ${renderWarnings(data.warnings, data.estimable)}
      ${renderActions(data)}`;
    resultSection.hidden = false;
    lastResult = data;
    rollIn(resultPanel.querySelector('.plate'));
    redrawers = [setupSimulator(data), setupHistory(data)].filter(Boolean);

    const status = resultPanel.querySelector('#share-status');
    resultPanel.querySelector('#copy-summary').addEventListener('click', () => {
      copyText(`${shareText(data)}\n${window.location.href}`)
        .then(() => { status.textContent = 'Resumen copiado.'; })
        .catch(() => { status.textContent = 'No se pudo copiar. Selecciona y copia el texto manualmente.'; });
    });
    resultPanel.querySelector('#print-result').addEventListener('click', () => window.print());
    resultPanel.querySelector('#new-query').addEventListener('click', () => {
      hideResult();
      clearUrl();
      form.scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
      brandSelect.focus({preventScroll: true});
    });

    resultSection.scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
    resultPanel.focus({preventScroll: true});
  }

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (lastResult) redrawers.forEach((fn) => fn()); }, 150);
  });

  brandSelect.addEventListener('change', () => { handleBrandChange().catch((e) => showMessage(e.message, 'error')); });
  modelSelect.addEventListener('change', () => { handleModelChange().catch((e) => showMessage(e.message, 'error')); });
  yearSelect.addEventListener('change', () => { handleYearChange().catch((e) => showMessage(e.message, 'error')); });
  engineSelect.addEventListener('change', () => { handleEngineChange().catch((e) => showMessage(e.message, 'error')); });
  transmissionSelect.addEventListener('change', () => { handleTransmissionChange().catch((e) => showMessage(e.message, 'error')); });
  versionSelect.addEventListener('change', hideResult);

  async function submitForm() {
    hideResult();
    const mileageText = mileageInput.value.trim();
    const mileage = Number(mileageText);
    if (!brandSelect.value || !modelSelect.value || !yearSelect.value || !engineSelect.value || !transmissionSelect.value || !versionSelect.value) {
      showMessage('Completa marca, línea, año, motor, transmisión y versión.', 'error');
      return;
    }
    if (!mileageText || !Number.isInteger(mileage) || mileage < 0) {
      showMessage('Escribe el kilometraje como un número entero, por ejemplo 85000.', 'error');
      mileageInput.focus();
      return;
    }
    submitButton.disabled = true;
    submitButton.classList.add('is-loading');
    submitButton.setAttribute('aria-busy', 'true');
    submitLabel.textContent = 'Consultando';
    showMessage('');
    try {
      const result = await getJson('/api/estimate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          version_id: Number(versionSelect.value), mileage_km: mileage,
          brand_id: Number(brandSelect.value), model_id: Number(modelSelect.value),
          model_year: Number(yearSelect.value), engine_id: Number(engineSelect.value),
          transmission_id: Number(transmissionSelect.value)
        })
      });
      showMessage(result.estimable ? 'Consulta lista.' : 'Consulta lista. Esta versión no tiene base suficiente para una valoración.', result.estimable ? 'success' : 'error');
      window.history.replaceState(null, '', `${window.location.pathname}?v=${encodeURIComponent(versionSelect.value)}&km=${mileage}`);
      renderResult(result);
    } catch (error) {
      showMessage(error.message, 'error');
    } finally {
      submitButton.disabled = false;
      submitButton.classList.remove('is-loading');
      submitButton.removeAttribute('aria-busy');
      submitLabel.textContent = SUBMIT_TEXT;
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submitForm();
  });

  // Enlaces compartidos: /?v=<versión>&km=<kilometraje> abre directamente el resultado.
  const params = new URLSearchParams(window.location.search);
  const sharedVersion = Number.parseInt(params.get('v') || '', 10);
  const sharedKm = Number.parseInt(params.get('km') || '', 10);
  if (Number.isInteger(sharedVersion) && sharedVersion > 0 && Number.isInteger(sharedKm) && sharedKm >= 0) {
    openVersion(sharedVersion, sharedKm);
  }

  /* ---------- Publicidad: la cinta se puede pausar y la valla se enciende al verla ---------- */
  const cinta = document.getElementById('cinta');
  const cintaPausa = document.getElementById('cinta-pausa');
  if (cinta && cintaPausa) {
    cintaPausa.addEventListener('click', () => {
      const paused = cinta.classList.toggle('is-paused');
      cintaPausa.setAttribute('aria-pressed', paused ? 'true' : 'false');
    });
  }

  const valla = document.getElementById('valla');
  if (valla) {
    const carro = document.getElementById('valla-carro');
    if (carro) carro.innerHTML = carSvg('suv', 'valla');
    const encender = () => valla.classList.add('is-on');
    if (reduceMotion || !('IntersectionObserver' in window)) {
      encender();
    } else {
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          encender();
          observer.disconnect();
        }
      }, {threshold: 0.3});
      observer.observe(valla);
    }
    const cta = valla.querySelector('.valla-cta');
    if (cta) {
      cta.addEventListener('click', (event) => {
        event.preventDefault();
        (form.closest('.form-card') || form).scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
        brandSelect.focus({preventScroll: true});
      });
    }
  }
})();
