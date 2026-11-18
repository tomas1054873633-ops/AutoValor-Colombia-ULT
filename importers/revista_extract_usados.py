"""Extractor por coordenadas de las tablas de USADOS (millones de COP) de Revista Motor.
Asocia cada precio con su año modelo usando las celdas de encabezado de cada bloque de marca.
"""
import pdfplumber, re, csv, sys, json, collections

PDF = sys.argv[1]; OUT = sys.argv[2]
NUM = re.compile(r'^(\d{1,3},\d|\d\.\d{3},?\d?)$')
pdf = pdfplumber.open(PDF)

def yr(s):
    n = int(s); return 2000 + n if n < 50 else 1900 + n

JOINS = []
def to_cop(raw):
    if re.fullmatch(r'\d\.\d{3}', raw): return int(raw.replace('.', '')) * 1_000_000   # 1.100 = 1.100 millones
    return int(round(float(raw.replace(',', '.')) * 1_000_000))

def clean_name(parts):
    out = []
    for i, ln in enumerate(parts):
        out.append(ln.strip())
    s = ''
    for ln in out:
        if s.endswith('-') and not s.endswith(' -'):
            JOINS.append((s.split(' ')[-1], ln.split(' ')[0])); s = s[:-1] + ln   # palabra partida por guion de fin de línea
        else:
            s = (s + ' ' + ln).strip()
    return re.sub(r'\s+', ' ', s)

rows_out = []; anomalies = []; heading_state = None; heading_state_brand = ['']
for pi in range(3, 12):
    page = pdf.pages[pi]
    W, H = page.width, page.height
    pg = page.crop((0, 0, W, H)).dedupe_chars(tolerance=1.2)
    ws = pg.extract_words(extra_attrs=['size'])
    origin = 'importado' if pi + 1 <= 9 else 'nacional'
    # encabezados de año
    yw = [w for w in ws if re.fullmatch(r'\d\d', w['text']) and w['top'] > 60]
    groups = collections.defaultdict(list)
    for w in yw: groups[round(w['top'] / 2)].append(w)
    headers = []
    for k, g in groups.items():
        g.sort(key=lambda w: w['x0'])
        cur = [g[0]]
        for w in g[1:]:
            if w['x0'] - cur[-1]['x1'] > 25 or int(w['text']) != int(cur[-1]['text']) - 1 and not (cur[-1]['text'] == '10' and w['text'] == '09'):
                headers.append(cur); cur = [w]
            else: cur.append(w)
        headers.append(cur)
    hdrs = []
    for h in headers:
        if len(h) < 5: continue
        top = h[0]['top']
        cols = []
        for w in h:
            cx = (w['x0'] + w['x1']) / 2
            rect = [r for r in pg.rects if r['x0'] - .5 <= cx <= r['x1'] + .5 and abs((r['top'] + r['bottom']) / 2 - (top + h[0]['bottom']) / 2) < 3 and 8 < (r['x1'] - r['x0']) < 36]
            if rect:
                r = min(rect, key=lambda r: abs(((r['x0'] + r['x1']) / 2) - cx))
                cols.append((yr(w['text']), r['x0'], r['x1']))
            else:
                cols.append((yr(w['text']), cx - 6, cx + 6))
        cols.sort(key=lambda c: c[1])
        hdrs.append({'top': top, 'bottom': h[0]['bottom'], 'cols': cols, 'x0': cols[0][1], 'x1': cols[-1][2]})
    # líneas horizontales
    hlines = [l for l in pg.lines if abs(l['top'] - l['bottom']) < 0.3]
    # marcas: palabras tamaño 10 con tipografía de marca
    brandw = [w for w in ws if 9.5 <= w['size'] <= 10.5 and w['top'] > 60]
    heads = [w for w in ws if w['size'] >= 20 and w['top'] > 60]
    headings = []
    hs = sorted(heads, key=lambda w: (round(w['top']), w['x0']))
    for w in hs:
        if headings and abs(headings[-1]['top'] - w['top']) < 3 and w['x0'] - headings[-1]['x1'] < 20:
            headings[-1]['text'] += ' ' + w['text']; headings[-1]['x1'] = w['x1']
        else: headings.append({'text': w['text'], 'x0': w['x0'], 'x1': w['x1'], 'top': w['top']})
    # columnas de página por borde izquierdo del bloque (x0 de columna nombre)
    for h in hdrs:
        fx = h['x0']
        cand = [l for l in hlines if abs(l['x1'] - fx) < 1.5 and l['top'] > h['bottom'] - 0.5]
        h['left'] = min([l['x0'] for l in cand]) if cand else fx - 56
    for h in list(hdrs):
        same = [g for g in hdrs if abs(g['left'] - h['left']) < 3]
        if h is min(same, key=lambda g: g['top']) and h['top'] > 100:
            above = [l for l in hlines if abs(l['x1'] - h['x0']) < 1.5 and 55 < l['top'] < h['top'] - 12]
            if len(above) >= 2:
                v = dict(h); v['top'] = v['bottom'] = min(l['top'] for l in above) - 0.5; v['virtual'] = True
                hdrs.append(v)
    lefts = sorted({round(h['left']) for h in hdrs})
    colidx = lambda x: max([i for i, l in enumerate(lefts) if l <= x + 45] or [0])
    for h in hdrs:
        h['col'] = colidx(h['left'])
        # marca: palabra(s) tamaño 10 sobre el encabezado
        if h.get('virtual'): h['brand'] = ''; continue
        b = [w for w in brandw if re.fullmatch(r"[A-ZÁÉÍÓÚÑ\-]{2,}", w['text']) and h['top'] - 18 < w['top'] < h['top'] and h['left'] - 5 < (w['x0'] + w['x1']) / 2 < h['x1'] + 5]
        b.sort(key=lambda w: w['x0'])
        h['brand'] = ' '.join(w['text'] for w in b)
        if not h['brand']:
            b = [c for c in pg.chars if abs(c['top'] - h['top']) < 6 and h['left'] - 5 < c['x0'] < h['x0'] - 2 and c['text'].strip() and c['size'] > 8]
            b.sort(key=lambda c: c['x0']); h['brand'] = ''.join(c['text'] for c in b).strip()
        if not h['brand']:
            # marca partida en letras espaciadas
            b = [c for c in pg.chars if 9.5 <= c['size'] <= 10.5 and h['top'] - 18 < c['top'] < h['top'] and h['left'] - 5 < c['x0'] < h['x1'] + 5]
            b.sort(key=lambda c: c['x0']); h['brand'] = ''.join(c['text'] for c in b).strip()
    for hd in headings: hd['col'] = colidx(((hd['x0'] + hd['x1']) / 2) - 70)
    # orden de lectura: columna, y
    events = [('H', hd['col'], hd['top'], hd) for hd in headings if not hd['text'].startswith(('USADOS', 'IMPORT', 'NACION'))] + [('B', h['col'], h['top'], h) for h in hdrs]
    events.sort(key=lambda e: (e[1], e[2]))
    for kind, c, t, obj in events:
        if kind == 'H': heading_state = obj['text']
        else: obj['category'] = heading_state
    # filas
    for h in sorted(hdrs, key=lambda g: (g['col'], g['top'])):
        if re.fullmatch(r'(\S )+\S', h['brand']): h['brand'] = h['brand'].replace(' ', '')
        if h['brand']: heading_state_brand[0] = h['brand']
        else: h['brand'] = heading_state_brand[0]; h['brand_continued'] = True
    for h in hdrs:
        h['brand'] = re.sub(r'\s+', ' ', h['brand']) if not re.fullmatch(r'(\S )+\S', h['brand']) else h['brand'].replace(' ', '')
        # siguiente cabecera/marca en la misma columna debajo
        below = [g for g in hdrs if g is not h and abs(g['left'] - h['left']) < 3 and g['top'] > h['top']]
        limit = min([g['top'] for g in below] + [H])
        c0 = h['cols'][0]
        cand = {round(l['top'], 1) for l in hlines if abs(l['x1'] - h['x0']) < 1.5 and l['x0'] >= h['left'] - 1.5 and h['bottom'] - 0.6 <= l['top'] <= limit + 0.1}
        for rc in pg.rects:
            if abs(rc['x0'] - c0[1]) < 1.2 and abs(rc['x1'] - c0[2]) < 1.2 and 4 < rc['bottom'] - rc['top'] < 40:
                for y in (rc['top'], rc['bottom']):
                    if h['bottom'] - 0.6 <= y <= limit + 0.1: cand.add(round(y, 1))
        tops = []
        for y in sorted(cand):
            if not tops or y - tops[-1] > 1.2: tops.append(y)
        diffs = [y - x for x, y in zip(tops, tops[1:]) if 4 < y - x < 40]
        if tops and diffs and not below:
            med = sorted(diffs)[len(diffs) // 2]
            if any(h['left'] - 1 <= w['x0'] < h['x1'] + 1 and tops[-1] < (w['top'] + w['bottom']) / 2 < tops[-1] + med + 1 for w in ws):
                tops.append(round(tops[-1] + med, 1))   # última fila de la página sin línea inferior
        # intervalos consecutivos
        for a, b in zip(tops, tops[1:]):
            if not (4 < b - a < 40): continue
            if below and pi + 1 <= 9 and b >= limit - 0.3: continue   # banda de marca antes de la siguiente cabecera
            inside = [w for w in ws if h['left'] - 1 <= w['x0'] < h['x1'] + 1 and a - 0.2 <= (w['top'] + w['bottom']) / 2 <= b + 0.2]
            if b <= h['bottom'] + 0.8 or any(g['top'] - 1 < (a + b) / 2 < g['bottom'] + 1 and g['left'] - 3 < h['left'] < g['left'] + 3 for g in hdrs): continue
            if any(w['size'] >= 9.5 and w['x1'] <= h['x0'] and w['x0'] < h['left'] + 120 and re.fullmatch(r'[A-ZÁÉÍÓÚ ]{3,}', w['text']) and False for w in inside): continue
            name_words = [w for w in inside if w['x1'] <= h['x0'] + 1]
            if not name_words: continue
            lines = collections.defaultdict(list)
            for w in name_words: lines[round(w['top'], 0)].append(w)
            name = clean_name([' '.join(w['text'] for w in sorted(v, key=lambda w: w['x0'])) for k, v in sorted(lines.items())])
            vals = {}; flags = {}; wpos = {}
            for w in inside:
                if w['x0'] >= h['x0'] - 1:
                    cx = (w['x0'] + w['x1']) / 2
                    colm = [c for c in h['cols'] if c[1] - 0.8 <= cx <= c[2] + 0.8]
                    tok = w['text']
                    if re.fullmatch(r'[\dO]{1,3},[\dO]', tok) and 'O' in tok and colm:
                        tok = tok.replace('O', '0'); flags[colm[0][0]] = 'caracter O leído como 0 (confirmar visualmente)'
                    if not NUM.match(tok) or not colm:
                        anomalies.append((pi + 1, h['brand'], name, w['text'], 'valor no interpretable o fuera de columna')); continue
                    vals[colm[0][0]] = tok; wpos[colm[0][0]] = (round(w['x0'],1), round(w['top'],1))
                    if re.fullmatch(r'\d\.\d{3}', tok): flags[colm[0][0]] = 'valor en miles de millones (formato 1.100) - confirmar'
            for y_, raw in vals.items():
                rows_out.append({'page': pi + 1, 'origin': origin, 'category': h.get('category'), 'brand': h['brand'], 'description': name,
                                 'model_year': y_, 'raw_value': raw, 'price_cop': to_cop(raw), 'flag': flags.get(y_, ''), 'x0': wpos[y_][0], 'top': wpos[y_][1],
                                 'brand_continued': h.get('brand_continued', False), 'col_x': round(h['left']), 'row_top': a, 'row_bottom': b})
            if not vals: anomalies.append((pi + 1, h['brand'], name, '', 'fila sin precios'))
with open(OUT, 'w', newline='', encoding='utf-8') as f:
    wr = csv.DictWriter(f, fieldnames=list(rows_out[0].keys())); wr.writeheader(); wr.writerows(rows_out)
with open(OUT.replace('.csv', '_anomalias.csv'), 'w', newline='', encoding='utf-8') as f:
    wr = csv.writer(f); wr.writerow(['page', 'brand', 'description', 'value', 'issue']); wr.writerows(anomalies)
print(len(rows_out), 'observaciones;', len({(r['page'], r['brand'], r['description'], r['col_x'], r['row_top']) for r in rows_out}), 'filas;', len(anomalies), 'anomalías')

import collections as _c
print('UNIONES POR GUION:', _c.Counter(f'{a}|{b}' for a,b in JOINS).most_common())
