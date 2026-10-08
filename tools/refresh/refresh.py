#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh semanal de los paneles Matex (index.html / matex.html + historico.json).

Qué hace (mismas reglas que se aplicaban a mano, ver proceso_actualizacion_erp_prod.md):
  1. Hornea cierres.json y overrides.json en el RAW (los pendientes de consolidar).
  2. ERP primero: archivo de OPs a detalle -> OPs nuevas (clasificación por descripción),
     ARTICULOS / PEDIDOS / MNET_FECHAS / TOOLTIP_DATA / MISSING; archivo de Documentos ->
     VENDEDOR y CLIENTES.
  3. PROD (opcional): merge por (OP,PART), T/C se preservan.
  4. matex.html: desarchiva / re-archiva a historico.json (corte de 90 días).
  5. Valida y escribe todo en --out, junto con reporte.md y reporte.json.

NUNCA decide por su cuenta lo que antes se consultaba: prefijos no reconocidos y OPs
reestructuradas quedan como PENDIENTES (reporte.json -> "needs_review": true) y no se publica.
Las respuestas ya dadas viven en decisiones.json (junto a este script).
"""
import argparse, csv, glob, io, json, os, re, sys, zipfile, collections
from datetime import date, datetime, timedelta, timezone

MX = timezone(timedelta(hours=-6))
MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
RAW_KEYS = ['OP','STATUS','XSTATUS','FEMIT','FVALE','FProm','Fprod','FTERM','AREA','SUBAREA',
            'CANT','PART','TRAB','MESH','MAT','VENDEDOR','GOLPE','TROQUEL1']

# ----------------------------------------------------------------------------- HTML consts
def balanced_slice(text, i):
    op = text[i]; cl = ']' if op == '[' else '}'
    d = 0; ins = False; esc = False
    for j in range(i, len(text)):
        c = text[j]
        if ins:
            if esc: esc = False
            elif c == '\\': esc = True
            elif c == '"': ins = False
            continue
        if c == '"': ins = True; continue
        if c == op: d += 1
        elif c == cl:
            d -= 1
            if d == 0: return text[i:j+1]
    raise ValueError('constante sin cerrar (¿HTML truncado?)')

def get_const(html, name):
    k = 'const ' + name + '='
    i = html.find(k)
    if i < 0: return None
    i += len(k)
    return json.loads(balanced_slice(html, i))

def dumps(v):
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False)

def set_const(html, name, value):
    k = 'const ' + name + '='
    i = html.find(k)
    if i < 0: raise ValueError('no existe const ' + name)
    i += len(k)
    old = balanced_slice(html, i)
    return html[:i] + dumps(value) + html[i+len(old):]

def set_header(html, label, text):
    """Reemplaza la fecha de un renglón del encabezado ('PROD:', 'Artículos:')."""
    pat = re.compile(r'(' + re.escape(label) + r'</span><span[^>]*>)[^<]*(</span>)')
    if not pat.search(html): raise ValueError('no encontré el encabezado ' + label)
    return pat.sub(lambda m: m.group(1) + text + m.group(2), html, count=1)


ENT_PAT = re.compile(r'(class="entregas-row">Entregas:</span><span[^>]*class="entregas-row">)(\d{2})/(\w{3})/(\d{2})(</span><span[^>]*class="entregas-row">)(\d{2}):(\d{2})(</span>)')
def set_entregas(html, timestamps):
    """Encabezado 'Entregas: DD/mmm/AA HH:MM' (hora de México, UTC-6, en bloques de 15 min): solo avanza, nunca retrocede."""
    if not timestamps: return html, None
    from datetime import datetime, timedelta
    best = max(datetime.strptime(t[:19], '%Y-%m-%dT%H:%M:%S') for t in timestamps) - timedelta(hours=6)
    best = best.replace(minute=best.minute // 15 * 15, second=0, microsecond=0)
    m = ENT_PAT.search(html)
    if not m: return html, None
    try: cur = datetime(2000 + int(m.group(4)), MESES.index(m.group(3)) + 1, int(m.group(2)), int(m.group(6)), int(m.group(7)))
    except ValueError: cur = datetime(2000, 1, 1)
    if best <= cur: return html, None
    txt = ('%02d/%s/%02d' % (best.day, MESES[best.month-1], best.year % 100), '%02d:%02d' % (best.hour, best.minute))
    return ENT_PAT.sub(lambda mm: mm.group(1) + txt[0] + mm.group(5) + txt[1] + mm.group(8), html, count=1), txt

def fmt_hdr(d): return '%02d/%s/%02d' % (d.day, MESES[d.month-1], d.year % 100)
def fmt_md(d): return '%d/%d/%d' % (d.month, d.day, d.year)
def parse_md(s):
    if not s: return None
    try:
        m, d, y = [int(x) for x in str(s).split('/')]; return date(y, m, d)
    except Exception: return None

# ----------------------------------------------------------------------------- normalizaciones
def norm_sub(s):
    if not s: return s
    mp = {'FTUBO':'Tubular','TAPAS':'Tapas','MANUAL':'Manual','DISCO':'Disco','PACK':'Pack','OBLONGO':'Oblongo',
          'REMALLADO':'Remallado','CRIBA':'Criba','ELIMINADOR':'Eliminador','P.FILTRANTE':'Placa filtrante',
          'EMBOLSADOS':'Embolsados','TUBULARES':'Tubulares','ARMAR':'Armar','HELVEX':'Tubulares','NUEVO':'Nuevo',
          'RINON':'Riñon','RIÑON':'Riñon','TIRAS':'Tiras','PACK RECT / MANUAL':'Packs rectangulares',
          'PACK RECT':'Packs rectangulares'}
    s2 = s.strip()
    if s2[:1] in (':', '.'): s2 = s2[1:]
    return mp.get(s2.upper()) or (s2[:1].upper() + s2[1:].lower())

def norm_area(v):
    s = str(v or '').strip()
    if s == 'Var': return 'Varios'
    if s.upper() == 'TELAR': return 'Telar'
    return s

def norm_subimp(v):
    s = re.sub(r'^[.:;]+', '', str(v or '').strip()).strip()
    return 'Tubulares' if s.upper() == 'HELVEX' else s

def norm_vend_code(v):
    s = str(v or '').strip()
    if s.upper() == 'OIP': return 'OP'
    if s.upper() == 'STOCK': return 'JB'
    return s[:2] if len(s) == 3 else s

def vend_from_name(name):
    """'OSCAR PEREZ' -> 'OP' (inicial del nombre + inicial del apellido). Acepta 'NUM - NOMBRE APELLIDO'."""
    s = re.sub(r'^\s*\d+\s*-\s*', '', str(name or '')).strip().upper()
    parts = s.split()
    return (parts[0][0] + parts[1][0]) if len(parts) >= 2 else ''

def norm_pedido(v):
    s = str(v or '').strip()
    m = re.match(r'^P-0*(\d+)$', s)
    return 'P-' + m.group(1) if m else s

def fmt_qty(v):
    if v is None or v == '': return ''
    try:
        f = float(str(v).replace(',', ''))
        return str(int(f)) if f == int(f) else str(f)
    except Exception: return str(v)

def fmt_cell_date(v):
    if v is None or v == '': return ''
    if isinstance(v, (datetime, date)): return fmt_md(v if isinstance(v, date) and not isinstance(v, datetime) else v.date())
    s = str(v).strip()
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m: return '%d/%d/%s' % (int(m.group(2)), int(m.group(3)), m.group(1))
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)   # CSV: DD/MM/YY
    if m:
        y = m.group(3); y = ('20' + y) if len(y) == 2 else y
        return '%d/%d/%s' % (int(m.group(2)), int(m.group(1)), y)
    return s

# ----------------------------------------------------------------------------- lectura de archivos
def read_table(path):
    """-> (encabezados, filas) de .xlsx o .csv (latin1)."""
    if path.lower().endswith('.csv'):
        txt = open(path, encoding='latin-1').read()
        rows = list(csv.reader(io.StringIO(txt)))
    else:
        import openpyxl, warnings
        warnings.simplefilter('ignore')
        ws = openpyxl.load_workbook(path, data_only=True).active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hdr = [str(h or '').strip() for h in rows[0]]
    return hdr, rows[1:]

def colmap(hdr, aliases, required=()):
    up = {h.upper(): i for i, h in enumerate(hdr)}
    out = {}
    for key, names in aliases.items():
        for n in names:
            if n.upper() in up: out[key] = up[n.upper()]; break
    miss = [k for k in required if k not in out]
    if miss: raise ValueError('Faltan columnas %s en %s' % (miss, hdr))
    return out

def find_inputs(inbox):
    """Descomprime zips y reconoce los archivos por nombre."""
    work = os.path.join(inbox, '_unzipped')
    os.makedirs(work, exist_ok=True)
    for z in glob.glob(os.path.join(inbox, '*.zip')):
        with zipfile.ZipFile(z) as zf: zf.extractall(work)
    files = [f for f in glob.glob(os.path.join(inbox, '**', '*'), recursive=True)
             if os.path.isfile(f) and f.lower().endswith(('.xlsx', '.csv'))]
    pick = {'ops': [], 'docs': [], 'prod': []}
    for f in files:
        b = os.path.basename(f).upper()
        if 'ORDENES' in b and 'PRODUCCION' in b: pick['ops'].append(f)
        elif 'DOCUMENTOS' in b: pick['docs'].append(f)
        elif b.startswith('PROD'): pick['prod'].append(f)
    srt = lambda L: sorted(L, key=lambda p: (os.path.basename(p), os.path.getmtime(p)))
    out = {k: (srt(v)[-1] if v else None) for k, v in pick.items()}
    out['docs_all'] = srt(pick['docs'])   # todas las ventanas de Documentos (más antigua primero)
    return out

# ----------------------------------------------------------------------------- clasificación ERP
CLASIF = [  # (prefijo, AREA, SUBAREA) — el orden importa (más específico primero)
    ('ARO DE ALAMBRE','Varios','Arillos'), ('EMPAQUE DE HULE','Aros','Empaque'),
    ('ELIMINADOR DE NIEBLA','Varios','Eliminador'), ('FILTRO TUBULAR','Varios','Tubular'),
    ('REMALLADO','Aros','Remallado'), ('RECTANGULOS','Disco','Manual'), ('CUADROS','Disco','Manual'),
    ('OBLONGO','Disco','Oblongo'), ('DISCOS','Disco','Disco'), ('DISCO ','Disco','Disco'),
    ('PACKS','Disco','Pack'), ('PACK ','Disco','Pack'), ('CRIBA','Telar','Criba'),
    ('RUMBA','Varios','Rumba'), ('ARO ','Aros','Nuevo'),
]
EXTERNAS = ('LAMINA', 'SOLERA')   # producción externa: no entra al RAW (va a MISSING/ARTICULOS)

def clean_desc(d):
    d = re.sub(r'\(\s*N\s*O\s*T\s*O\s*M\s*A\s*R\s*\)', ' ', str(d or ''), flags=re.I)
    return re.sub(r'\s+', ' ', d).strip()

def classify(desc):
    u = clean_desc(desc).upper()
    for p in EXTERNAS:
        if u.startswith(p): return 'EXT', None
    for p, a, s in CLASIF:
        if u.startswith(p): return a, s
    return None, None

def parse_mat(desc, sub):
    u = clean_desc(desc).upper()
    if u.startswith('EMPAQUE DE HULE') or ' HULE' in u: return 'Hule'
    if 'POLIPROPILENO' in u: return 'Polipropileno'
    if 'INOX' in u: return '316' if re.search(r'\b316L?\b', u) else '304'
    if 'GALVANIZADO' in u: return 'Galvanizado'
    if 'NEGRO' in u or 'CARBON' in u or 'CARBÓN' in u: return 'Carbón'
    return ''

def _first_num(seg):
    m = re.search(r'\d+(?:\.\d+)?', seg); return m.group(0) if m else ''

def parse_mesh(desc, sub):
    """Devuelve (mesh, n_segmentos). 'A X B' -> A (una malla); 'A-B-C' -> 'A/B/C' (mallas apiladas)."""
    u = clean_desc(desc).upper()
    if sub == 'Manual':
        m = re.search(r'(?:MESH:?|MALLA)\s*(\d+(?:\.\d+)?\s*X\s*\d+(?:\.\d+)?)', u)
        if m: return re.sub(r'\s*X\s*', ' X ', m.group(1)), 1
    m = re.search(r'\(\s*(\d+(?:\s*-\s*\d+)+)\s*\)', u)               # FILTRO TUBULAR ( 40-60-80 )
    if not m:
        m = re.search(r'(?:MALLAS?|MESH)\s*:?\s*(\d+(?:\.\d+)?(?:\s*X\s*\d+(?:\.\d+)?)?(?:\s*-\s*\d+(?:\.\d+)?(?:\s*X\s*\d+(?:\.\d+)?)?)+)', u)
    if m:
        segs = [s for s in re.split(r'\s*-\s*', m.group(1)) if s.strip()]
        nums = [_first_num(s) for s in segs]
        nums = [n for n in nums if n]
        if len(nums) >= 2: return '/'.join(nums), len(nums)
    m = re.search(r'(?:MALLAS?|MESH)\s*:?\s*(\d+(?:\.\d+)?)', u)
    if m: return m.group(1), 1
    m = re.search(r'\b(\d+(?:\s*-\s*\d+)+)\b(?=\s*,?\s*DIAM)', u)                  # "PACKS TIPO DONA 60-120, DIAM. ..."
    if m:
        nums = re.split(r'\s*-\s*', m.group(1)); return '/'.join(nums), len(nums)
    return '', 1

def parse_troquel(desc, area, sub):
    u = clean_desc(desc).upper()
    if sub == 'Manual':
        m = re.search(r'(\d+(?:\.\d+)?)\s*MM\s*X\s*(\d+(?:\.\d+)?)\s*MM', u)
        if m: return '%s X %s mm' % (m.group(1), m.group(2))
    if area == 'Aros':
        m = re.search(r'(?:DIAM(?:ETRO)?\.?\s*(?:DE\s*)?)(\d+(?:\.\d+)?)\s*(?:"|”|\'\')', u) or \
            re.search(r'(\d+(?:\.\d+)?)\s*(?:"|”)\s*(?:DE\s*)?DIAM', u) or \
            re.search(r'DIAM(?:ETRO)?\.?\s*(?:DE\s*)?(\d+(?:\.\d+)?)\b(?!\s*(?:MM|CM))', u)   # "DIAM. 60 DE ACERO" (sin comilla)
        if m: return m.group(1) + '"'
        return ''
    m = re.search(r'DIAM(?:ETRO)?\.?\s*(?:DE\s*)?(\d+(?:\.\d+)?)\s*(MM|CMS?|CM\.)?', u)
    if m and m.group(2) and m.group(2).startswith('C'):
        v = float(m.group(1)) * 10; return fmt_qty(v)
    if m and (m.group(2) or '').startswith('MM'): return fmt_qty(m.group(1))
    if m and not m.group(2) and area == 'Disco' and sub in ('Disco', 'Pack') and not re.search(r'DIAM(?:ETRO)?\.?\s*(?:DE\s*)?\d+(?:\.\d+)?\s*"', u):
        return fmt_qty(m.group(1))                    # "DIAM. 250 DE ACERO" -> el ERP escribe mm sin unidad
    return ''

def build_part(desc, cant, area, sub):
    mesh, nseg = parse_mesh(desc, sub)
    cant_s = fmt_qty(cant)
    golpe = fmt_qty(float(cant_s) * nseg) if (nseg > 1 and cant_s != '') else cant_s
    return {'AREA': area, 'SUBAREA': sub, 'CANT': cant_s, 'MESH': mesh, 'MAT': parse_mat(desc, sub),
            'GOLPE': golpe, 'TROQUEL1': parse_troquel(desc, area, sub)}

# ----------------------------------------------------------------------------- lectura ERP
OP_ALIASES = {'folio':['FOLIO'], 'fecha':['FECHA'], 'entrega':['F. ENTREGA','FECHA ENTREGA','F ENTREGA'],
              'estatus':['ESTATUS'], 'desc':['DESCRIPCION','ARTICULO'], 'ref':['REFERENCIA'],
              'cant':['CANTIDAD']}
DOC_ALIASES = {'folio':['FOLIO'], 'fecha':['FECHA'], 'op':['OPERACION'], 'nombre':['NOMBRE','CLIENTE'],
               'estatus':['ESTATUS'], 'vendedor':['VENDEDOR']}

def read_erp_ops(path):
    hdr, rows = read_table(path)
    cm = colmap(hdr, OP_ALIASES, required=('folio','entrega','desc','cant'))
    ops = collections.OrderedDict()
    for r in rows:
        f = r[cm['folio']]
        m = re.match(r'^O-0*(\d+)$', str(f or '').strip())
        if not m: continue
        op = m.group(1)
        ops.setdefault(op, []).append({
            'fecha': fmt_cell_date(r[cm['fecha']]) if 'fecha' in cm else '',
            'entrega': fmt_cell_date(r[cm['entrega']]),
            'estatus': str(r[cm['estatus']] or 'ACTIVA').strip().upper() if 'estatus' in cm else 'ACTIVA',
            'desc': str(r[cm['desc']] or '').strip(),
            'ref': norm_pedido(r[cm['ref']]) if 'ref' in cm else '',
            'cant': r[cm['cant']]})
    return ops

def read_erp_docs(path):
    hdr, rows = read_table(path)
    cm = colmap(hdr, DOC_ALIASES, required=('folio','nombre'))
    docs = {}
    for r in rows:
        f = str(r[cm['folio']] or '').strip()
        if not re.match(r'^P-\d+$', f): continue
        docs[norm_pedido(f)] = {'nombre': re.sub(r'\s+', ' ', str(r[cm['nombre']] or '')).strip(),
                                'vendedor': vend_from_name(r[cm['vendedor']]) if 'vendedor' in cm else '',
                                'estatus': str(r[cm['estatus']] or '').strip() if 'estatus' in cm else ''}
    return docs, ('vendedor' in cm)

def read_prod(path):
    hdr, rows = read_table(path)
    cm = colmap(hdr, {k:[k] for k in ['OP','XSTATUS','FEMIT','FVALE','FProm','Fprod','FTERM','PART','AREA','SUBAREA',
                                      'CANT','TROQUEL1','MAT','MESH','TRAB','GOLPE','VENDEDOR']},
                required=('OP','PART','AREA','CANT'))
    out = []
    for r in rows:
        op = r[cm['OP']]
        if op is None or str(op).strip() == '': continue
        g = lambda k: r[cm[k]] if k in cm else ''
        row = {'OP': str(op).strip().split('.')[0], 'STATUS': 'V', 'XSTATUS': g('XSTATUS') or '',
               'FEMIT': fmt_cell_date(g('FEMIT')), 'FVALE': fmt_cell_date(g('FVALE')),
               'FProm': fmt_cell_date(g('FProm')), 'Fprod': fmt_cell_date(g('Fprod')),
               'FTERM': fmt_cell_date(g('FTERM')), 'AREA': norm_area(g('AREA')),
               'SUBAREA': norm_subimp(g('SUBAREA')), 'CANT': fmt_qty(g('CANT')),
               'PART': str(g('PART') or '').strip(), 'TRAB': g('TRAB') or '', 'MESH': g('MESH') if g('MESH') is not None else '',
               'MAT': g('MAT') if g('MAT') is not None else '', 'VENDEDOR': norm_vend_code(g('VENDEDOR')),
               'GOLPE': fmt_qty(g('GOLPE')), 'TROQUEL1': str(g('TROQUEL1')) if g('TROQUEL1') not in (None,) else ''}
        for k in ('MESH','MAT'): row[k] = '' if row[k] is None else str(row[k]) if not isinstance(row[k], str) else row[k]
        if row['OP'] == '31234': row['VENDEDOR'] = 'EM'
        out.append({k: row[k] for k in RAW_KEYS})
    # El ERP repite la partida con CANT=0 y GOLPE=0 (movimientos extra): se descartan si existe la fila con cantidad.
    kk = lambda r: (r['OP'], r['PART'], (norm_sub(r['SUBAREA']) or '').upper())
    con_cant = {kk(r) for r in out if str(r['CANT']) not in ('0', '', '0.0') or str(r['GOLPE']) not in ('0', '', '0.0')}
    limpio = [r for r in out if not (str(r['CANT']) in ('0', '', '0.0') and str(r['GOLPE']) in ('0', '', '0.0') and kk(r) in con_cant)]
    READ_PROD_INFO['fantasmas'] = len(out) - len(limpio)
    return limpio

READ_PROD_INFO = {}

# ----------------------------------------------------------------------------- merge PROD
def merge_prod(current, xrows, restruct_dec):
    """Merge documentado: T/C se preservan; V toma el xlsx si (OP,PART) existe, si no se preserva."""
    by_op = lambda rows: collections.defaultdict(list, {})
    cur_by = collections.defaultdict(list); x_by = collections.defaultdict(list)
    for r in current: cur_by[r['OP']].append(r)
    for r in xrows: x_by[r['OP']].append(r)
    restructured = []
    for op, cr in cur_by.items():
        if op in x_by:
            parts = {r['PART'] for r in cr}
            if not any(r['PART'] in parts for r in x_by[op]): restructured.append(op)
    comp = set()
    seen = collections.Counter()
    for r in current + xrows: seen[(r['OP'], r['PART'])] += 1
    comp = {k[0] for k, c in seen.items() if c > 1}
    key = lambda r: (r['OP'], r['PART'], (norm_sub(r['SUBAREA']) or '').upper()) if r['OP'] in comp else (r['OP'], r['PART'])
    rx_xlsx = {o for o in restructured if restruct_dec.get(o) == 'xlsx'}
    rx_keep = {o for o in restructured if restruct_dec.get(o) != 'xlsx'}
    eff_cur = [r for r in current if not (r['OP'] in rx_xlsx and r['STATUS'] == 'V')]
    eff_x = [r for r in xrows if r['OP'] not in rx_keep]
    groups = collections.defaultdict(list)
    for r in eff_x: groups[key(r)].append(r)
    used = collections.Counter(); result = []; stats = collections.Counter(); changes = []
    for r in eff_cur:                                   # 1) T/C primero, consumen su clave
        if r['STATUS'] in ('T', 'C'):
            result.append(r); stats['T/C preservadas'] += 1
            if key(r) in groups: used[key(r)] = len(groups[key(r)])
    for r in eff_cur:                                   # 2) V: xlsx fresco si existe
        if r['STATUS'] in ('T', 'C'): continue
        k = key(r); g = groups.get(k, []); p = used[k]
        if p < len(g):
            fresh = dict(g[p]); used[k] += 1
            fresh['TROQUEL1'] = troquel_cosmetic(r['TROQUEL1'], fresh['TROQUEL1'])
            diff = {f: (r[f], fresh[f]) for f in RAW_KEYS if str(r[f]) != str(fresh[f])}
            if diff: changes.append({'op': r['OP'], 'part': r['PART'], 'diff': diff})
            result.append(fresh); stats['V refrescadas'] += 1
        else:
            result.append(r); stats['V preservadas (xlsx no las trae)'] += 1
    for k, g in groups.items():                         # 3) nuevas
        for r in g[used[k]:]: result.append(r); stats['partidas nuevas'] += 1
    return result, stats, changes, restructured

def troquel_cosmetic(cur, new):
    def p(s):
        m = re.match(r'^([\d.]+)(.*)$', str(s));  return (float(m.group(1)), m.group(2).strip()) if m else None
    a, b = p(cur), p(new)
    return cur if (a and b and a == b) else new

# ----------------------------------------------------------------------------- split matex (activo / histórico)
def split_matex(raw_all_ops, today, days=90):
    """OP activa = alguna fila V, o cerrada hace <= days días. -> set de OPs que deben archivarse."""
    arch = set()
    by = collections.defaultdict(list)
    for r in raw_all_ops: by[r['OP']].append(r)
    for op, rows in by.items():
        if any(r['STATUS'] == 'V' for r in rows): continue
        fts = [parse_md(r['FTERM']) for r in rows if r['STATUS'] in ('T', 'C')]
        fts = [f for f in fts if f]
        if fts and (today - max(fts)).days > days: arch.add(op)
    return arch

# ----------------------------------------------------------------------------- derivar matex (activo / histórico) desde el RAW completo
def derive_matex(full, today, days=90):
    """full: dict de constantes del index (RAW completo). -> (activo, historico) con la misma forma que usan matex.html / historico.json."""
    arch = split_matex(full['RAW'], today, days)
    in_arch = lambda op: op in arch
    act = {'RAW': [r for r in full['RAW'] if not in_arch(r['OP'])],
           'RAW_h': [r for r in full['RAW'] if in_arch(r['OP'])]}
    by_op = lambda d: ({k: v for k, v in d.items() if not in_arch(k)}, {k: v for k, v in d.items() if in_arch(k)})
    out_a, out_h = {'RAW': act['RAW']}, {'RAW': act['RAW_h']}
    for name in ('ARTICULOS', 'TOOLTIP_DATA', 'PEDIDOS', 'MNET_FECHAS'):
        out_a[name], out_h[name] = by_op(full[name])
    used = set(out_a['PEDIDOS'].values())                  # un pedido se queda activo si alguna OP activa lo usa
    out_a['CLIENTES'] = {p: v for p, v in full['CLIENTES'].items() if p in used}
    out_h['CLIENTES'] = {p: v for p, v in full['CLIENTES'].items() if p not in used}
    return out_a, out_h, arch

# ----------------------------------------------------------------------------- bake cierres / overrides
BAKE_INFO = []   # entregas parciales horneadas en esta corrida (para avisos y para revisar el PROD después)

def letra_part(raw, op, part):
    """'1/1' -> '1a/1' (primera letra libre dentro de la OP). Igual que el panel."""
    m = re.match(r'^(\d+)([a-z]*)/(.+)$', str(part), re.I)
    usadas = {r['PART'] for r in raw if r['OP'] == op}
    if not m: return str(part) + 'a'
    for i in range(26):
        p = '%s%s/%s' % (m.group(1), chr(97 + i), m.group(3))
        if p not in usadas: return p
    return str(part) + 'x'

def _entrega_partida(raw, c, notes):
    """Entrega de una partida: completa (cant vacía o >= pendiente) o parcial (se parte en T entregada + V pendiente)."""
    op, part = str(c['op']), c['part']
    idx = next((i for i, r in enumerate(raw) if r['OP'] == op and r['PART'] == part and r['STATUS'] == 'V'), None)
    if idx is None:
        notes.append('Entrega de OP %s partida %s: no encontré esa partida pendiente en el panel (¿ya estaba cerrada?); se ignora.' % (op, part)); return 0
    r = raw[idx]
    cant = float(r['CANT'] or 0); q = c.get('cant')
    q = cant if q in (None, '') else float(q)
    if q <= 0: return 0
    if q >= cant:
        r['STATUS'] = 'T'; r['FTERM'] = c['fecha']; return 1
    golpe = float(r['GOLPE'] or cant)
    t = dict(r); t['STATUS'] = 'T'; t['FTERM'] = c['fecha']
    t['CANT'] = fmt_qty(q); t['GOLPE'] = fmt_qty(round(golpe * q / cant)); t['PART'] = letra_part(raw, op, part)
    r['CANT'] = fmt_qty(cant - q); r['GOLPE'] = fmt_qty(round(golpe * (cant - q) / cant))
    raw.insert(idx, t)
    BAKE_INFO.append({'op': op, 'part': part, 'tpart': t['PART'], 'q': q, 'c': cant, 'golpe_c': golpe})
    notes.append('Entrega parcial OP %s partida %s: %s de %s entregadas el %s (quedan %s pendientes; la entregada queda como %s).' % (op, part, fmt_qty(q), fmt_qty(cant), c['fecha'], fmt_qty(cant - q), t['PART']))
    return 1

def bake(raw, cierres, overrides, parciales=True, notes=None):
    n_c = n_o = 0
    notes = notes if notes is not None else []
    for c in sorted(cierres, key=lambda e: e.get('timestamp') or ''):
        if c.get('part'):
            if parciales: n_c += _entrega_partida(raw, c, notes)
            continue
        for r in raw:
            if r['OP'] == str(c['op']) and r['STATUS'] == 'V':
                r['STATUS'] = 'T'; r['FTERM'] = c['fecha']; n_c += 1
    for o in overrides:
        for r in raw:
            if r['OP'] == str(o['op']) and r['STATUS'] == 'V':
                r['Fprod'] = o['fecha']; r['FTERM'] = o['fecha']; n_o += 1
    return n_c, n_o

def revisar_parciales(raw, notes):
    """Si el PROD vuelve a traer la partida con la cantidad completa, se descuenta lo ya entregado."""
    for b in BAKE_INFO:
        v = next((r for r in raw if r['OP'] == b['op'] and r['PART'] == b['part'] and r['STATUS'] == 'V'), None)
        if v is not None and float(v['CANT'] or 0) == b['c']:
            v['CANT'] = fmt_qty(b['c'] - b['q']); v['GOLPE'] = fmt_qty(round(b['golpe_c'] * (b['c'] - b['q']) / b['c']))
            notes.append('OP %s partida %s: el PROD aún trae la cantidad completa (%s); se descontó la entrega parcial de %s (quedan %s).' % (b['op'], b['part'], fmt_qty(b['c']), fmt_qty(b['q']), v['CANT']))

# ----------------------------------------------------------------------------- ERP -> RAW
def erp_to_rows(op, lines, decisions, docs, vend_by_pedido, pending):
    """Convierte las líneas de una OP del ERP en filas RAW. -> (filas, descripciones_para_ARTICULOS, externas)"""
    kept, externas = [], []
    for ln in lines:
        area, sub = classify(ln['desc'])
        if area == 'EXT': externas.append(ln); continue
        dec = decisions.get('clasificacion', {}).get(op)
        if area is None and dec: area, sub = dec['AREA'], dec['SUBAREA']
        if area is None:
            pending.append({'tipo': 'prefijo no reconocido', 'op': op, 'detalle': ln['desc']})
            area, sub = '', ''
        kept.append((ln, area, sub))
    rows, descs = [], []
    n = len(kept)
    for i, (ln, area, sub) in enumerate(kept, 1):
        p = build_part(ln['desc'], ln['cant'], area, sub) if area else \
            {'AREA':'','SUBAREA':'','CANT':fmt_qty(ln['cant']),'MESH':'','MAT':'','GOLPE':fmt_qty(ln['cant']),'TROQUEL1':''}
        vend = (decisions.get('vendedor_pedido', {}).get(ln['ref']) or docs.get(ln['ref'], {}).get('vendedor') or vend_by_pedido.get(ln['ref'], '')) if ln['ref'] else ''
        row = {'OP': op, 'STATUS': 'V', 'XSTATUS': '', 'FEMIT': ln['fecha'], 'FVALE': ln['fecha'],
               'FProm': ln['entrega'], 'Fprod': ln['entrega'], 'FTERM': ln['entrega'],
               'AREA': p['AREA'], 'SUBAREA': p['SUBAREA'], 'CANT': p['CANT'], 'PART': '%d/%d' % (i, n),
               'TRAB': '', 'MESH': p['MESH'], 'MAT': p['MAT'], 'VENDEDOR': vend, 'GOLPE': p['GOLPE'],
               'TROQUEL1': p['TROQUEL1']}
        rows.append({k: row[k] for k in RAW_KEYS}); descs.append(ln['desc'])
    return rows, descs, externas

def date_from_name(path, pattern):
    m = re.search(pattern, os.path.basename(path or ''))
    return m

# ----------------------------------------------------------------------------- orquestación
def run(a):
    log = []; pending = []; notes = []
    today = date.fromisoformat(a.today) if a.today else datetime.now(MX).date()
    rd = lambda p: open(p, encoding='utf-8').read()
    idx_html, mtx_html = rd(a.index), rd(a.matex)
    hist = json.load(open(a.historico, encoding='utf-8')) if a.historico and os.path.exists(a.historico) else {}
    cierres = json.load(open(a.cierres, encoding='utf-8')) if a.cierres and os.path.exists(a.cierres) else []
    overrides = json.load(open(a.overrides, encoding='utf-8')) if a.overrides and os.path.exists(a.overrides) else []
    decisions = json.load(open(a.decisiones, encoding='utf-8')) if a.decisiones and os.path.exists(a.decisiones) else {}

    names = ['RAW','PEDIDOS','ARTICULOS','MNET_FECHAS','MISSING','TOOLTIP_DATA','CLIENTES']
    full = {n: get_const(idx_html, n) for n in names}
    for n in names:
        if full[n] is None: raise SystemExit('index.html no trae const ' + n)
    raw0_ops = {r['OP'] for r in full['RAW']}

    # integridad de entrada: index == matex activo + histórico (total de OPs)
    m_raw = get_const(mtx_html, 'RAW') or []; h_raw = hist.get('RAW', [])
    if raw0_ops != ({r['OP'] for r in m_raw} | {r['OP'] for r in h_raw}):
        d1 = raw0_ops - ({r['OP'] for r in m_raw} | {r['OP'] for r in h_raw})
        d2 = ({r['OP'] for r in m_raw} | {r['OP'] for r in h_raw}) - raw0_ops
        notes.append('Aviso: index y matex+histórico no tenían el mismo conjunto de OPs (solo en index: %s; solo en matex/hist: %s). Se re-derivó matex desde el index.' % (sorted(d1)[:10], sorted(d2)[:10]))

    inputs = find_inputs(a.inbox) if a.inbox else {'ops': None, 'docs': None, 'prod': None, 'docs_all': []}
    raw = full['RAW']

    # 1) hornear cierres / overrides pendientes
    v_ops = {r['OP'] for r in raw if r['STATUS'] == 'V'}
    for c in cierres:
        if str(c['op']) not in raw0_ops: pending.append({'tipo': 'cierre de una OP que no existe en el panel', 'op': c['op'], 'detalle': ''})
        elif str(c['op']) not in v_ops: notes.append('cierre OP %s: ya estaba cerrada en el panel (no cambia nada).' % c['op'])
    n_c, n_o = bake(raw, cierres, overrides, notes=notes)
    log.append('Cierres horneados: %d fila(s) de %d entrada(s); overrides: %d fila(s) de %d entrada(s).' % (n_c, len(cierres), n_o, len(overrides)))

    # 2) ERP
    docs, has_vend = ({}, False)
    if inputs['docs']:
        has_vend = False
        for dp in inputs['docs_all']:      # semana anterior primero, la más reciente pisa
            d1, hv = read_erp_docs(dp)
            has_vend = has_vend or hv
            for k, v in d1.items():
                old = docs.get(k)
                if old and old.get('vendedor') and not v.get('vendedor'): v = dict(v, vendedor=old['vendedor'])
                docs[k] = v
        if len(inputs['docs_all']) > 1: notes.append('Documentos combinados de %d archivos (ventanas semanales): %s' % (len(inputs['docs_all']), ', '.join(os.path.basename(x) for x in inputs['docs_all'])))
        if not has_vend: notes.append('El archivo de Documentos NO trae columna VENDEDOR: el vendedor de OPs nuevas queda vacío hasta que llegue el PROD.')
    vend_by_pedido = {}
    tmp = collections.defaultdict(collections.Counter)
    for r in raw:
        p = full['PEDIDOS'].get(r['OP'])
        if p and r['VENDEDOR']: tmp[p][r['VENDEDOR']] += 1
    for p, c in tmp.items(): vend_by_pedido[p] = c.most_common(1)[0][0]

    prod_rows, prod_info = None, None
    if inputs['prod']:
        m = re.search(r'PROD(\d{2})(\d{2})(\d{2})', os.path.basename(inputs['prod']).upper())
        pdate = date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
        mh = re.search(r'PROD:</span><span[^>]*>(\d{2})/(\w{3})/(\d{2})</span>', idx_html)
        hdate = date(2000 + int(mh.group(3)), MESES.index(mh.group(2)) + 1, int(mh.group(1))) if mh else None
        prod_rows = read_prod(inputs['prod'])
        for r in prod_rows:
            al = decisions.get('alias_part', {}).get(r['OP'], {})
            nuevo = al.get('%s|%s' % (r['PART'], r['CANT'])) or al.get(r['PART'])   # "2/2|2500" (por cantidad) o "2/2" (todas)
            if nuevo: r['PART'] = nuevo
        desc = decisions.get('descartar_prod', {})   # {"31456": ["SUSTITUCION"]}: filas del PROD que no deben existir en el panel
        if desc:
            antes = len(prod_rows)
            prod_rows = [r for r in prod_rows if (norm_sub(r['SUBAREA']) or '').upper() not in {x.upper() for x in desc.get(r['OP'], []) if not str(x).startswith('_')}]
            if len(prod_rows) != antes: notes.append('PROD: se descartaron %d fila(s) según decisiones.json (descartar_prod): %s.' % (antes - len(prod_rows), ', '.join(sorted(k for k in desc if not k.startswith('_')))))
        if READ_PROD_INFO.get('fantasmas'): notes.append('PROD: se descartaron %d fila(s) repetidas con CANT=0 y GOLPE=0 (el ERP repite la partida; se queda la que trae cantidad).' % READ_PROD_INFO['fantasmas'])
        prod_info = {'archivo': os.path.basename(inputs['prod']), 'fecha': pdate, 'header': hdate,
                     'aplicar': bool(a.force_prod or (pdate and (not hdate or pdate > hdate)))}
    prod_ops = {r['OP'] for r in (prod_rows or [])}

    new_ops, added_rows, upd_dates, vend_fill, completadas, externas_new = [], [], [], [], [], []
    erp = collections.OrderedDict()
    art_date = None
    if inputs['ops']:
        erp = read_erp_ops(inputs['ops'])
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', os.path.basename(inputs['ops']))
        art_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else today
        raw_ops_now = {r['OP'] for r in raw}
        for op, lines in erp.items():
            rows, descs, ext = erp_to_rows(op, lines, decisions, docs, vend_by_pedido, pending)
            est = lines[0]['estatus']
            ref = lines[0]['ref']
            if ref: full['PEDIDOS'][op] = ref
            if lines[0]['entrega']: full['MNET_FECHAS'][op] = lines[0]['entrega']
            if op in raw_ops_now:                       # OP existente: solo se agregan descripciones que falten
                art = full['ARTICULOS'].setdefault(op, [])
                for d in descs:
                    if d not in art: art.append(d)
            if op not in raw_ops_now:
                full['ARTICULOS'][op] = descs if rows else [l['desc'] for l in lines]
                if rows:
                    raw.extend(rows); new_ops.append(op); added_rows += rows
                    if est == 'COMPLETADA': completadas.append(op)
                    first = rows[0]
                    full['TOOLTIP_DATA'][op] = {'tq': first['TROQUEL1'], 'ms': first['MESH'], 'mt': first['MAT'], 'sub': norm_sub(first['SUBAREA'])}
                if ext and est == 'ACTIVA' and not rows:
                    if not any(m['OP'] == op for m in full['MISSING']):
                        full['MISSING'].append({'OP': op, 'PEDIDO': ref, 'ARTICULO': ext[0]['desc'], 'CANT': int(float(fmt_qty(ext[0]['cant']) or 0)),
                                                'FECHA': ext[0]['entrega'], 'ESTATUS': 'ACTIVA'}); externas_new.append(op)
            else:
                # OP ya existente: si el PROD no la trae, manda el ERP (fechas) y se completa lo que falte
                cur = [r for r in raw if r['OP'] == op]
                curV = [r for r in cur if r['STATUS'] == 'V']
                if est == 'COMPLETADA' and curV: completadas.append(op)
                if curV and op not in prod_ops and est == 'ACTIVA':
                    if len(curV) != len(rows):
                        pending.append({'tipo': 'estructura distinta ERP vs panel', 'op': op, 'detalle': '%d partida(s) V en el panel, %d en el ERP' % (len(curV), len(rows))})
                    else:
                        for r, n in zip(curV, rows):
                            ch = {f: (r[f], n[f]) for f in ('FEMIT','FVALE','FProm','Fprod','FTERM') if r[f] != n[f]}
                            for f in ch: r[f] = n[f]
                            if ch: upd_dates.append({'op': op, 'part': r['PART'], 'cambios': ch})
                for r in curV:
                    if not r['VENDEDOR'] and ref:
                        v = decisions.get('vendedor_pedido', {}).get(ref) or docs.get(ref, {}).get('vendedor') or vend_by_pedido.get(ref, '')
                        if v: r['VENDEDOR'] = v; vend_fill.append((op, r['PART'], v))
        # MISSING: quitar los que ya están en RAW o quedaron COMPLETADA en el ERP
        keep = []
        for m in full['MISSING']:
            if m['OP'] in {r['OP'] for r in raw} or (m['OP'] in erp and erp[m['OP']][0]['estatus'] == 'COMPLETADA'): notes.append('MISSING: se retira OP %s (ya en RAW o COMPLETADA).' % m['OP'])
            else: keep.append(m)
        full['MISSING'] = keep
        # CLIENTES / vendedor desde Documentos
        by_name = collections.defaultdict(collections.Counter)
        for p, v in full['CLIENTES'].items(): by_name[re.sub(r'\s+', ' ', v['nombre']).upper()][v['num']] += 1
        nuevos_cli = 0
        for p, d in docs.items():
            if p not in full['CLIENTES']:
                num = by_name[d['nombre'].upper()].most_common(1)[0][0] if by_name.get(d['nombre'].upper()) else ''
                full['CLIENTES'][p] = {'num': num, 'nombre': d['nombre']}; nuevos_cli += 1
        log.append('CLIENTES: %d pedido(s) nuevo(s).' % nuevos_cli)

    # 2b) Lote con solo Documentos (sin archivo de OPs): también se completan vendedores y clientes
    if docs and not inputs['ops']:
        ya = {(o, p) for o, p, v in vend_fill}
        for r in raw:
            if r['VENDEDOR']: continue
            ref = full['PEDIDOS'].get(r['OP'])
            v = (decisions.get('vendedor_pedido', {}).get(ref) or docs.get(ref, {}).get('vendedor')) if ref else ''
            if v and (r['OP'], r['PART']) not in ya:
                r['VENDEDOR'] = v; vend_fill.append((r['OP'], r['PART'], v))
        by_name = collections.defaultdict(collections.Counter)
        for p, v in full['CLIENTES'].items(): by_name[re.sub(r'\s+', ' ', v['nombre']).upper()][v['num']] += 1
        nuevos_cli = 0
        for p, d in docs.items():
            if p not in full['CLIENTES']:
                num = by_name[d['nombre'].upper()].most_common(1)[0][0] if by_name.get(d['nombre'].upper()) else ''
                full['CLIENTES'][p] = {'num': num, 'nombre': d['nombre']}; nuevos_cli += 1
        log.append('CLIENTES: %d pedido(s) nuevo(s).' % nuevos_cli)

    # 3) PROD
    prod_stats = prod_changes = restr = None
    if prod_info and prod_info['aplicar']:
        dec = decisions.get('reestructuradas', {})
        merged, prod_stats, prod_changes, restr = merge_prod(raw, prod_rows, dec)
        for o in restr:
            if o not in dec: pending.append({'tipo': 'OP reestructurada (decidir: reemplazar V con PROD o mantener)', 'op': o, 'detalle': ''})
        raw[:] = merged
        for op_f, campos in decisions.get('forzar', {}).items():       # valores que el PROD trae mal y se corrigen a mano
            if op_f.startswith('_'): continue
            for r in raw:
                if r['OP'] == op_f and r['STATUS'] == 'V':
                    for f, v in campos.items():
                        if f != 'nota' and str(r[f]) != str(v):
                            notes.append('OP %s (%s): %s del PROD (%s) reemplazado por %s según decisiones.json.' % (op_f, r['PART'], f, r[f], v)); r[f] = str(v)
        for r in raw:
            if r['OP'] in {x['OP'] for x in prod_rows} and r['OP'] not in full['TOOLTIP_DATA']:
                full['TOOLTIP_DATA'][r['OP']] = {'tq': r['TROQUEL1'], 'ms': r['MESH'], 'mt': r['MAT'], 'sub': norm_sub(r['SUBAREA'])}
    bake(raw, cierres, overrides, parciales=False)       # por si una OP nueva ya estaba cerrada en cierres.json
    revisar_parciales(raw, notes)

    # 4) encabezado (solo index)
    out_idx = idx_html
    if art_date: out_idx = set_header(out_idx, 'Artículos:', fmt_hdr(art_date))
    if prod_info and prod_info['aplicar'] and prod_info['fecha']: out_idx = set_header(out_idx, 'PROD:', fmt_hdr(prod_info['fecha']))

    # 5) escribir index (constantes) + derivar matex / histórico
    for n in names: out_idx = set_const(out_idx, n, full[n])
    act, his, arch = derive_matex(full, today, a.dias)
    out_mtx = mtx_html
    for n in ('RAW','PEDIDOS','ARTICULOS','MNET_FECHAS','TOOLTIP_DATA','CLIENTES'): out_mtx = set_const(out_mtx, n, act[n])
    out_mtx = set_const(out_mtx, 'MISSING', full['MISSING'])
    ts_c = [c['timestamp'] for c in cierres if c.get('timestamp')]
    out_idx, ent1 = set_entregas(out_idx, ts_c); out_mtx, ent2 = set_entregas(out_mtx, ts_c)
    if ts_c:
        for _nm in ('out_idx', 'out_mtx'):
            _h = out_idx if _nm == 'out_idx' else out_mtx
            _old = (get_const(_h, 'CIERRES_META') or {}).get('hasta', '') if 'const CIERRES_META=' in _h else None
            if _old is not None:
                _h = set_const(_h, 'CIERRES_META', {'hasta': max([_old] + ts_c)})
                if _nm == 'out_idx': out_idx = _h
                else: out_mtx = _h
    if ent1 or ent2: notes.append('Encabezado "Entregas" actualizado a %s %s (último cierre horneado).' % (ent1 or ent2))
    out_hist = {k: his[k] for k in ('RAW','ARTICULOS','TOOLTIP_DATA','PEDIDOS','CLIENTES','MNET_FECHAS')}

    # 6) validaciones automáticas
    checks = []
    def chk(ok, msg): checks.append((bool(ok), msg))
    chk(all(list(r.keys()) == RAW_KEYS for r in raw), 'Todas las filas RAW tienen exactamente las 18 columnas esperadas, en orden')
    fut = [r['OP'] for r in added_rows if (parse_md(r['FEMIT']) or today) > today or (parse_md(r['FVALE']) or today) > today]
    chk(not fut, 'Ninguna OP nueva tiene FEMIT/FVALE en el futuro' + (' (%s)' % fut if fut else ''))
    chk({r['OP'] for r in act['RAW']} | {r['OP'] for r in his['RAW']} == {r['OP'] for r in raw}, 'matex activo + histórico = mismas OPs que el index')
    chk(not ({r['OP'] for r in act['RAW']} & {r['OP'] for r in his['RAW']}), 'Ninguna OP está a la vez en matex activo y en histórico')
    chk(len(act['RAW']) + len(his['RAW']) == len(raw), 'Filas matex activo + histórico = filas del index (%d)' % len(raw))
    dup = collections.Counter((r['OP'], r['PART'], (norm_sub(r['SUBAREA']) or '').upper()) for r in raw if r['STATUS'] == 'V')
    dups = [k for k, c in dup.items() if c > 1]
    chk(not dups, 'Sin partidas V duplicadas (OP,PART,SUBAREA)' + (' -> %s' % dups[:5] if dups else ''))
    for html, nm in ((out_idx, 'index.html'), (out_mtx, 'matex.html')):
        chk(all(get_const(html, n) is not None for n in names if not (nm == 'matex.html' and False)), nm + ': todas las constantes se leen de vuelta')
    chk(dumps(get_const(out_idx, 'RAW')) == dumps(raw), 'index.html: RAW se lee de vuelta idéntico')

    os.makedirs(a.out, exist_ok=True)
    open(os.path.join(a.out, 'index.html'), 'w', encoding='utf-8').write(out_idx)
    open(os.path.join(a.out, 'matex.html'), 'w', encoding='utf-8').write(out_mtx)
    open(os.path.join(a.out, 'historico.json'), 'w', encoding='utf-8').write(dumps(out_hist))
    open(os.path.join(a.out, 'cierres.json'), 'w', encoding='utf-8').write('[]')
    open(os.path.join(a.out, 'overrides.json'), 'w', encoding='utf-8').write('[]')

    # JS syntax (si hay node)
    import shutil, subprocess, tempfile
    if shutil.which('node'):
        for nm, html in (('index.html', out_idx), ('matex.html', out_mtx)):
            ok = True
            for i, s in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)):
                if not s.strip(): continue
                tf = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8'); tf.write(s); tf.close()
                r = subprocess.run(['node', '--check', tf.name], capture_output=True, text=True); os.unlink(tf.name)
                if r.returncode: ok = False; checks.append((False, '%s: error de sintaxis JS en bloque %d: %s' % (nm, i, r.stderr[:200])))
            if ok: checks.append((True, nm + ': sintaxis JS válida en todos los bloques <script>'))

    # 7) reporte
    return write_report(a, today, locals())

def write_report(a, today, L):
    raw, new_ops, added_rows = L['raw'], L['new_ops'], L['added_rows']
    pend, checks, notes, log = L['pending'], L['checks'], L['notes'], L['log']
    erp, docs = L['erp'], L['docs']
    needs = bool(pend) or any(not ok for ok, _ in checks)
    ln = []
    w = ln.append
    w('# Reporte de refresh — %s\n' % today.strftime('%d/%m/%Y'))
    w('**Estado: %s**\n' % ('REQUIERE REVISIÓN — no publicar todavía' if needs else 'listo para publicar'))
    inp = L['inputs']
    w('Archivos: OPs `%s` · Documentos `%s` · PROD `%s`\n' % tuple(os.path.basename(inp[k]) if inp[k] else '—' for k in ('ops','docs','prod')))
    w('## Resumen')
    w('- OPs nuevas en el panel: **%d** (%d partidas)' % (len(new_ops), len(added_rows)))
    w('- Externas (LAMINA/SOLERA) enviadas a MISSING: %s' % (', '.join(L['externas_new']) or 'ninguna'))
    for l in log: w('- ' + l)
    pi = L['prod_info']
    if pi: w('- PROD `%s`: %s' % (pi['archivo'], 'aplicado' if pi['aplicar'] else 'NO aplicado (no es más nuevo que el "PROD:" del encabezado, %s)' % (pi['header'] or '—')))
    else: w('- PROD: no se recibió')
    w('- Matex: %d OPs activas / %d en histórico (corte %d días)\n' % (len({r['OP'] for r in L['act']['RAW']}), len({r['OP'] for r in L['his']['RAW']}), a.dias))
    if pend:
        w('## Pendientes (el script NO decide estos)')
        for p in pend: w('- **%s** — OP %s %s' % (p['tipo'], p['op'], p['detalle']))
        w('')
    if notes:
        w('## Avisos')
        for n in notes: w('- ' + n)
        w('')
    w('## OPs nuevas')
    w('| OP | Pedido | Vendedor | Partida | Área / Subárea | Cant | Golpe | Mesh | Mat | Troquel | Emisión | Entrega |')
    w('|---|---|---|---|---|---|---|---|---|---|---|---|')
    for r in added_rows:
        w('| %s | %s | %s | %s | %s / %s | %s | %s | %s | %s | %s | %s | %s |' % (r['OP'], L['full']['PEDIDOS'].get(r['OP'], ''), r['VENDEDOR'] or '—', r['PART'], r['AREA'], r['SUBAREA'],
          r['CANT'], r['GOLPE'], r['MESH'], r['MAT'], r['TROQUEL1'], r['FEMIT'], r['FProm']))
    w('\nMESH / GOLPE / TROQUEL1 vienen de leer la descripción: son provisionales y el PROD los corrige cuando llegue.\n')
    if L['completadas']:
        w('## ERP dice COMPLETADA pero el panel las tiene (o las crea) como V')
        w(', '.join(L['completadas']) + ' — el estatus del ERP no cierra OPs; ciérralas por WhatsApp/panel si ya se entregaron.\n')
    if L['upd_dates']:
        w('## OPs existentes sin PROD: fechas tomadas del ERP')
        for u in L['upd_dates']: w('- OP %s (%s): %s' % (u['op'], u['part'], '; '.join('%s %s → %s' % (f, o, n) for f, (o, n) in u['cambios'].items())))
        w('')
    if L['vend_fill']:
        w('## Vendedor completado en OPs existentes')
        for o, p, v in L['vend_fill']: w('- OP %s (%s) → %s' % (o, p, v))
        w('')
    sin_v = sorted({r['OP'] for r in added_rows if not r['VENDEDOR']})
    if sin_v: w('## Sin vendedor (el Documentos no trae vendedor para ese pedido, o el pedido no aparece, y no hay otra OP del mismo pedido)\n' + ', '.join(sin_v) + '\n')
    if L['prod_changes'] is not None:
        w('## Cambios por PROD (revisión exhaustiva, todas las filas)')
        w('%s' % dict(L['prod_stats']))
        for c in L['prod_changes']: w('- OP %s (%s): %s' % (c['op'], c['part'], '; '.join('%s %s → %s' % (f, o, n) for f, (o, n) in c['diff'].items())))
        w('')
    w('## Validaciones automáticas')
    for ok, msg in checks: w('- [%s] %s' % ('x' if ok else ' FALLÓ', msg))
    open(os.path.join(a.out, 'reporte.md'), 'w', encoding='utf-8').write('\n'.join(ln))
    json.dump({'needs_review': needs, 'pending': pend, 'new_ops': new_ops, 'checks': [{'ok': ok, 'msg': m} for ok, m in checks]},
              open(os.path.join(a.out, 'reporte.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n'.join(ln))
    return 2 if needs else 0

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--index', required=True); ap.add_argument('--matex', required=True)
    ap.add_argument('--historico'); ap.add_argument('--cierres'); ap.add_argument('--overrides')
    ap.add_argument('--inbox', help='carpeta con los zip/xlsx recibidos'); ap.add_argument('--out', required=True)
    ap.add_argument('--decisiones', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decisiones.json'))
    ap.add_argument('--today'); ap.add_argument('--dias', type=int, default=90)
    ap.add_argument('--force-prod', action='store_true')
    sys.exit(run(ap.parse_args()))

if __name__ == '__main__':
    main()
