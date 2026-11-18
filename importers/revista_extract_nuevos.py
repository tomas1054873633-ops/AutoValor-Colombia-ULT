"""Extractor de precios de vehículos NUEVOS (pesos completos) de Revista Motor, págs. 2-3 (pliegos).
Los precios ya vienen en COP: NO se multiplican."""
import pdfplumber, re, csv, sys, collections
PDF, OUT = sys.argv[1], sys.argv[2]
PRICE = re.compile(r'^\d{1,3}(\.\d{3})+$')
TAG = re.compile(r'^\((HL|HE|HC|HR)\)$')
pdf = pdfplumber.open(PDF)
rows = []; notes = []
COLX1 = {2: [175, 305, 436, 566, 770, 901, 1031, 1162], 3: [175, 305, 436, 566, 770, 901, 1031, 1161]}
# Anclas de categoría (pág, columna 1-8, y_pt): la categoría rige desde esa y hacia abajo y continúa en la columna siguiente
ANCHORS = {}
exec(open(sys.argv[3]).read()) if len(sys.argv) > 3 else None
def category_for(pn, col, y):
    cur = None
    for (p, c, yy, name) in sorted(ANCHORS_LIST, key=lambda a: (a[0], a[1], a[2])):
        if (p, c, yy) <= (pn, col, y): cur = name
    return cur
ANCHORS_LIST = ANCHORS.get('list', [])
for pn in (2, 3):
    page = pdf.pages[pn - 1]
    pg = page.dedupe_chars(tolerance=1.2)
    ws = pg.extract_words(extra_attrs=['size', 'fontname'])
    if pn == 2: brand = None; buf = []
    for ci, x1 in enumerate(COLX1[pn], start=1):
        left, right = x1 - 132, x1 + 3
        cw = [w for w in ws if left <= w['x0'] and w['x1'] <= right + 1 and 70 < w['top'] < 760]
        lines = collections.defaultdict(list)
        for w in cw: lines[round(w['top'] / 1.6)].append(w)
        for key in sorted(lines):
            L = sorted(lines[key], key=lambda w: w['x0']); top = min(w['top'] for w in L)
            heavy = [w for w in L if 'Heavy' in w['fontname'] and 8.9 < w['size'] < 9.2]
            if heavy and len(heavy) == len(L):
                t = ''
                for k, w in enumerate(L):
                    t += (' ' if k and w['x0'] - L[k - 1]['x1'] > 2.0 else '') + w['text']
                brand = {'MISTUBISHI': 'MITSUBISHI', 'LANDROVER': 'LAND ROVER', 'ROYALENFIELD': 'ROYAL ENFIELD', 'MOTOGUZZI': 'MOTO GUZZI'}.get(t, t); buf = []; continue
            if any(w['size'] > 7.3 for w in L): continue   # rótulos, texto de distribuidores, etc.
            price = [w for w in L if PRICE.match(w['text']) and w['x1'] >= x1 - 5][-1:]
            tags = [w['text'].strip('()') for w in L if TAG.match(w['text'])]
            nm = [w for w in L if w not in price and not TAG.match(w['text'])]
            text = ' '.join(w['text'] for w in nm).strip()
            if price:
                parts = []
                same_col = buf and buf[-1][3] == ci
                if not text and buf and (top - buf[-1][0] < 9.5 or not same_col): parts = [b for b in buf]
                elif text and buf and (same_col and top - buf[-1][0] < 9.5): parts = [b for b in buf]
                name = ' '.join([p[1] for p in parts] + ([text] if text else [])).strip()
                tg = tags or [t for p in parts for t in p[2]]
                rows.append({'page': pn, 'col': ci, 'category': category_for(pn, ci, top), 'brand': brand, 'description': re.sub(r'\s+', ' ', name),
                             'hybrid_type': ','.join(tg), 'raw_price': price[-1]['text'], 'price_cop': int(price[-1]['text'].replace('.', '')), 'y': round(top, 1),
                             'wrapped': bool(parts)})
                buf = []
            else:
                if text: buf = [b for b in buf if b[3] != ci or top - b[0] < 9.5] + [(top, text, tags, ci)]
with open(OUT, 'w', newline='', encoding='utf-8') as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)
print(len(rows), 'precios de nuevos')
