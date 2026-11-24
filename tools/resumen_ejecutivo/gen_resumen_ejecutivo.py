# -*- coding: utf-8 -*-
"""
gen_resumen_ejecutivo.py
=========================
Genera el "resumen ejecutivo de carga de producción" (HTML + PDF, carta
horizontal a 2 páginas) que se le manda a gerencia con las OPs abiertas.
 
Combina lo mejor de las 3 versiones históricas del reporte:
- V1 (may/2026): badge de antigüedad por OP (gris <30d / ámbar 30-89d / rojo
  ≥90d) + fila teñida de rosa si ≥90d.
- V2 (jul/2026, BASE principal): jala datos directo de index.html en GitHub,
  usa los íconos de subIcon() (mismos que el panel, así que se actualizan
  solos si cambian ahí), layout de 2 páginas horizontal partido en el borde
  Disco/Aros con el header de días repetido en la página 2, regla de
  "Carga/día" = Disco suma golpes, el resto suma piezas (todo junto).
- V3 (sep/2026): confirmó que el contenido (íconos + totales por área) es el
  mismo; su técnica de Gantt en tabla de celdas era para esquivar un bug de
  `window.print()` en Chrome real — no aplica aquí porque exportamos el PDF
  con Playwright (`page.pdf()`), no con window.print(), así que se mantienen
  las barras posicionadas de V2 (ya verificado que sí imprimen bien así).
 
Requiere Playwright (chromium) para: (a) evaluar subIcon() vía JS y (b)
exportar el PDF.
 
USO
---
1. TODAY se calcula solo (fecha actual en hora de México) — no hace falta
   tocarla; la ventana GRID/W0/W1 se recalcula sola a partir de ahí. Si
   alguna vez hace falta reproducir una corrida de un día específico
   (debug), se puede forzar con la variable de entorno RESUMEN_TODAY en
   formato AAAA-MM-DD.
2. Deja `index.html`, `cierres.json`, `overrides.json` (los 3 más recientes de
   GitHub, repo ccf4/Produccion) y `subicon_template.js` (del proyecto, es el
   MISMO archivo que usan index.html/matex.html — cópialo tal cual, no lo
   edites) en el mismo directorio que este script.
3. `python3 gen_resumen_ejecutivo.py`
4. Verifica visualmente `resumen_ejecutivo.png` (o el PDF) antes de entregar —
   con pocas OPs cabe holgado en 2 páginas, pero si la carga crece mucho
   puede desbordar; si eso pasa, lo primero que hay que achicar son las
   alturas de fila del Gantt (.grow/.garow/.gsrow) y el font-size de la tabla.
 
NOTA IMPORTANTE sobre "qué OP cuenta como abierta"
---------------------------------------------------
index.html embebe su RAW estático, pero el panel en vivo aplica encima
`cierres.json` (cierres hechos desde el editor en vivo, sin pasar por chat) y
`overrides.json` (correcciones de Fprod) ANTES de decidir qué es V/T — ver
`applyCierres()`/`applyOverrides()` dentro de index.html. Si no se replica ese
overlay aquí, el resumen puede mostrar como abiertas OPs que ya se entregaron
hoy mismo vía el editor en vivo (pasó el 23/sep/2026: 6 OPs estaban cerradas
en cierres.json pero el RAW estático de index.html todavía las marcaba V).
Por eso este script SIEMPRE aplica cierres.json/overrides.json antes de
filtrar STATUS=='V', igual que hace el panel.
 
OPs excluidas permanentemente de este resumen (casos especiales, llevan
meses/años abiertas a propósito): 9098, 29841, 30550. Ver overview.md /
tooling.md del proyecto si esa lista cambia.
 
Líneas de semana: el Gantt marca, con una línea punteada vertical (clase
.wksep, superpuesta encima de las barras para que no se tape), los dos
bordes que separan "esta semana" de la previa y de la próxima — se calculan
solos a partir de GRID, no hay que tocar nada.
"""
import json, os, re
from datetime import date, datetime, timedelta, timezone
 
# ============================================================
# CONFIG
# ============================================================
# "Hoy" se calcula solo, en hora de México (UTC-6 fijo — México eliminó el
# horario de verano en la mayor parte del país desde 2022, igual que el
# resto del panel/paneles ya lo asumen). Antes había que editar esta fecha
# a mano en cada corrida manual (el script se usaba solo desde chat); ahora
# que corre automático desde el Action, ya no hace falta tocarla.
# RESUMEN_TODAY (formato AAAA-MM-DD) permite forzarla para debug/pruebas.
_MX = timezone(timedelta(hours=-6))
_override = os.environ.get('RESUMEN_TODAY')
TODAY = date.fromisoformat(_override) if _override else datetime.now(_MX).date()
EXCLUDE = {"9098", "29841", "30550"}
 
# Ventana: semana previa (contexto gris) + esta semana + próxima semana,
# solo Lun-Sáb (18 columnas). Recalcula a partir de TODAY.
_this_mon = TODAY - timedelta(days=TODAY.weekday())
_prev_mon = _this_mon - timedelta(days=7)
_next_mon = _this_mon + timedelta(days=7)
GRID = [_prev_mon + timedelta(days=i) for i in range(6)] \
     + [_this_mon + timedelta(days=i) for i in range(6)] \
     + [_next_mon + timedelta(days=i) for i in range(6)]
W0, W1 = _this_mon, _next_mon + timedelta(days=5)
GIDX = {d: i for i, d in enumerate(GRID)}
N = len(GRID)
 
MES = ['','ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
DOW = {0:'L',1:'M',2:'X',3:'J',4:'V',5:'S'}
COL = {'Disco':('#2563eb','#1e3a8a'), 'Aros':('#059669','#065f46'),
       'Varios':('#7c5cff','#4c2dad'), 'Telar':('#fb923c','#9a3412'),
       'ALMC':('#c77dff','#7c3aed')}
AREA_ORDER = ['Disco', 'Aros', 'Telar', 'Varios', 'ALMC']
PAGE2_AT = None   # página 1 = todo lo que va ANTES de esta área en AREA_ORDER
 
def usa_golpe(area, sub):
    """Dentro de Disco, solo las subáreas Disco y Pack se cuentan/muestran en
    golpes (g); el resto de Disco (Manual, Tapas, Oblongo/manual, Riñon,
    Tiras, etc.) son piezas (pz), igual que en las demás áreas."""
    return area == 'Disco' and sub in ('Disco', 'Pack')
 
NORM_MAP = {'FTUBO':'Tubular','TAPAS':'Tapas','MANUAL':'Manual','DISCO':'Disco',
    'PACK':'Pack','OBLONGO':'Oblongo','REMALLADO':'Remallado','CRIBA':'Criba',
    'ELIMINADOR':'Eliminador','P.FILTRANTE':'Placa filtrante','EMBOLSADOS':'Embolsados',
    'TUBULARES':'Tubulares','ARMAR':'Armar','HELVEX':'Tubulares','NUEVO':'Nuevo',
    'RINON':'Riñon','RIÑON':'Riñon','TIRAS':'Tiras',
    'PACK RECT / MANUAL':'Packs rectangulares','PACK RECT':'Packs rectangulares'}
# ⚠️ Si index.html cambia su normSub() (agrega/renombra subáreas), actualiza
# este dict a mano copiando la función real del HTML — este script no la lee
# solo, para no depender de que el archivo no haya cambiado de forma.
 
def pd(s):
    if not s or not s.strip(): return None
    m, d, y = s.split('/'); return date(int(y), int(m), int(d))
 
def normSub(s):
    if not s: return s
    s2 = s.strip()
    if s2[:1] in (':', '.'): s2 = s2[1:]
    return NORM_MAP.get(s2.upper(), s2[:1].upper() + s2[1:].lower())
 
def area_lbl(a):
    return 'Aro' if a == 'Aros' else a

def plural_sub(s):
    """Nombre de subárea en plural para los totales (Disco→Discos, Eliminador→Eliminadores)."""
    if not s or s[-1].lower() == 's': return s
    if s.lower() == 'pack': return s + 's'
    return s + ('s' if s[-1].lower() in 'aeiou' else 'es')

def nf(n):
    return f'{int(n):,}'.replace(',', ' ')
 
def fmt_dmy(s):
    """'9/23/2026' (M/D/YYYY, tal como llega de cierres.json) -> '23/sep/2026'."""
    if not s: return s
    m, d, y = s.split('/')
    return f'{int(d)}/{MES[int(m)]}/{y}'
 
# ============================================================
# PASO 1 — cargar RAW + overlay cierres.json/overrides.json + filtrar
# ============================================================
def load_and_filter(workdir):
    txt = open(os.path.join(workdir, 'index.html')).read()
    RAW = json.loads(re.search(r'const RAW=(\[.*?\]);', txt, re.S).group(1))
 
    overrides_path = os.path.join(workdir, 'overrides.json')
    if os.path.exists(overrides_path):
        for o in json.load(open(overrides_path)):
            op, fecha = str(o.get('op')), o.get('fecha')
            for r in RAW:
                if str(r['OP']) == op: r['Fprod'] = fecha
 
    cierres_path = os.path.join(workdir, 'cierres.json')
    cierres = {}
    if os.path.exists(cierres_path):
        for o in json.load(open(cierres_path)):
            cierres[str(o['op'])] = {'fecha': o['fecha'], 'timestamp': o.get('timestamp')}
    n_closed = 0
    for r in RAW:
        op = str(r['OP'])
        if op in cierres and r['STATUS'] == 'V':
            r['STATUS'] = 'T'; r['FTERM'] = cierres[op]['fecha']; n_closed += 1
    print(f'Cierres en vivo aplicados: {n_closed} filas -> OPs {sorted(cierres.keys(), key=int)}')
 
    entregas_dt = None
    if cierres:
        entregas_dt = max(cierres.values(), key=lambda x: x['timestamp'] or '')['fecha']
 
    # totales T+V por (OP, área, subárea) (excluye canceladas) — para detectar entregas parciales:
    # una OP es parcial en una subárea si el total de ESA subárea (T+V) es mayor que lo que sigue
    # pendiente (V). Si la OP tiene varias subáreas y en esta no se ha entregado nada, no es parcial.
    op_totals = {}
    for r in RAW:
        if r['STATUS'].strip().upper() not in ('T', 'V'): continue
        op = (str(r['OP']), r['AREA'].strip(), normSub(r['SUBAREA']))
        d = op_totals.setdefault(op, {'cant': 0, 'golpe': 0})
        d['cant'] += int(r['CANT'] or 0)
        d['golpe'] += int(r['GOLPE'] or 0)
 
    V, fuera_ventana = [], []
    for r in RAW:
        if r['STATUS'].strip().upper() != 'V': continue
        if str(r['OP']) in EXCLUDE: continue
        fp = pd(r['Fprod'])
        if not fp: continue
        if fp > W1:
            fuera_ventana.append((r['OP'], r['PART'], r['AREA'], r['SUBAREA'], r['Fprod']))
            continue
        V.append(r)
    print(f'Filas abiertas en ventana: {len(V)}  OPs distintas: {len(set(r["OP"] for r in V))}')
    if fuera_ventana:
        print('Fuera de ventana (no entran a este resumen):', fuera_ventana)
    return V, entregas_dt, fuera_ventana, op_totals
 
# ============================================================
# PASO 2 — agrupar (AREA, subárea normalizada) -> OP -> totales
# ============================================================
def group_rows(V, op_totals):
    groups, seen, dup = {}, set(), []
    for r in V:
        k = (r['OP'], r['PART'])
        if k in seen: dup.append(k); continue
        seen.add(k)
        area, sub = r['AREA'].strip(), normSub(r['SUBAREA'])
        g = groups.setdefault((area, sub), {})
        o = g.setdefault(r['OP'], {'cant':0,'golpe':0,'femit':None,'fprod':pd(r['Fprod']),'sub_raw':r['SUBAREA'],
                                    'mesh':[], 'tq':[], 'mat':[]})
        for key, val in (('mesh', r.get('MESH')), ('tq', r.get('TROQUEL1')), ('mat', r.get('MAT'))):
            val = (val or '').strip()
            if val and val not in o[key]: o[key].append(val)
        o['cant'] += int(r['CANT'] or 0)
        o['golpe'] += int(r['GOLPE'] or 0)
        fe = pd(r['FEMIT'])
        if o['femit'] is None or (fe and fe < o['femit']): o['femit'] = fe
    if dup: print('!! (OP,PART) duplicados ignorados:', dup)
    for (area_k, sub_k), ops in groups.items():
        for op, o in ops.items():
            tot = op_totals.get((op, area_k, sub_k), {'cant': o['cant'], 'golpe': o['golpe']})
            o['tot_cant'] = tot['cant']
            o['tot_golpe'] = tot['golpe']
            o['partial'] = tot['cant'] > o['cant'] or tot['golpe'] > o['golpe']
    return groups
 
def areas_present(groups):
    return [a for a in AREA_ORDER if any(k[0] == a for k in groups)]
 
def subs_of(groups, area):
    s = [k[1] for k in groups if k[0] == area]
    if area == 'Disco':
        fixed = [x for x in ['Disco', 'Pack'] if x in s]
        # todas las variantes "manual" (Manual, Oblongo / manual, ...) van juntas,
        # justo después de Pack — Manual primero, luego el resto por antigüedad de OP
        manual = sorted([x for x in s if x not in fixed and 'manual' in x.lower()],
                         key=lambda x: (x != 'Manual', min(int(o) for o in groups[(area, x)])))
        head = fixed + manual
        rest = sorted([x for x in s if x not in head], key=lambda x: min(int(o) for o in groups[(area,x)]))
        return head + rest
    return sorted(s, key=lambda x: min(int(o) for o in groups[(area, x)]))
 
def ops_of(groups, area, sub):
    return sorted(groups[(area, sub)].items(), key=lambda kv: (kv[1]['fprod'], int(kv[0])))
 
# ============================================================
# PASO 3 — resolver íconos subIcon() vía Playwright (paleta propia del reporte)
# ============================================================
SUBICON_HARNESS = '''<!DOCTYPE html><html><head><meta charset="utf-8"><script>
var AC = {{'Disco':'#2563eb','Aros':'#059669','Telar':'#fb923c','Varios':'#7c5cff','ALMC':'#c77dff'}};
function ac(a){{return AC[a]||'#888';}}
function normSub(s){{
  if(!s)return s;
  var map={norm_json};
  var s2=s.trim();
  if(s2[0]===':'||s2[0]==='.') s2=s2.slice(1);
  return map[s2.toUpperCase()]||(s2.charAt(0).toUpperCase()+s2.slice(1).toLowerCase());
}}
</script>
<script>{subicon_js}</script>
</head><body></body></html>'''
 
def resolve_icons(workdir, groups, palette=None, tweak=True):
    from playwright.sync_api import sync_playwright
    subicon_js = open(os.path.join(workdir, 'subicon_template.js')).read() \
        .replace('__PCT__', '12').replace('__BASE__', 'white')  # tema claro (config "matex")
    harness = SUBICON_HARNESS.format(norm_json=json.dumps(NORM_MAP, ensure_ascii=False), subicon_js=subicon_js)
    if palette:
        harness = harness.replace("var AC = {{'Disco':'#2563eb','Aros':'#059669','Telar':'#fb923c','Varios':'#7c5cff','ALMC':'#c77dff'}};".replace('{{','{').replace('}}','}'),
                                  'var AC = ' + json.dumps(palette) + ';')
    harness_path = os.path.join(workdir, '_harness.html')
    open(harness_path, 'w').write(harness)
 
    reqs = []
    for area in areas_present(groups):
        for sub in subs_of(groups, area):
            rep_raw = next(iter(groups[(area, sub)].values()))['sub_raw']
            reqs.append((area, sub, rep_raw))
 
    resolved = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto('file://' + os.path.abspath(harness_path))
        for area, sub, raw in reqs:
            html = pg.evaluate('([sub,area]) => subIcon(sub,area)', [raw, area])
            if html and not tweak:
                pass
            elif html:
                html = (html.replace('width:20px;height:20px', 'width:19px;height:19px')
                            .replace('margin-right:5px', 'margin-right:4px')
                            .replace('width="16" height="16"', 'width="15" height="15"'))
            else:
                html = fallback_icon(area, sub) or ''
            resolved[area + '||' + sub] = html
        b.close()
    os.remove(harness_path)
    return resolved
 
def fallback_icon(area, sub):
    """subIcon() (compartido con el panel en vivo) todavía no trae ícono para
    algunas subáreas nuevas — ej. Tapas. Mientras no se agregue allá, usamos
    aquí un círculo simple del mismo tamaño que el de Disco/Pack."""
    if sub != 'Tapas':
        return None
    c = COL.get(area, ('#475569', '#1f2937'))[0]
    bg = f'color-mix(in srgb,{c} 12%,white)'
    return (f'<span style="display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;'
            f'border-radius:4px;background:{bg};vertical-align:middle;margin-right:4px;flex-shrink:0">'
            f'<svg width="15" height="15" viewBox="0 0 32 32" fill="none">'
            f'<circle cx="16" cy="16" r="10" stroke="{c}" stroke-width="1.8" fill="none"/></svg></span>')
 
# ============================================================
# PASO 4 — armar tabla + Gantt (con badge de antigüedad) + carga/día
# ============================================================
def age_days(femit): return (TODAY - femit).days if femit else None
 
def age_badge(age):
    if age is None: return ''
    return f'<span class="agebadge ab-{age_class_m(age)}">{age}d</span>'
 
def build_table(groups, icons=None):
    icons = icons or {}
    tb = []; G_PZ = G_G = G_OP = 0
    for area in areas_present(groups):
        a_pz = a_g = 0; a_ops = set(); buf = []
        for sub in subs_of(groups, area):
            ops = dict(ops_of(groups, area, sub))
            s_pz = sum(o['cant'] for o in ops.values())
            s_g = sum((o['golpe'] if usa_golpe(area, sub) else o['cant']) for o in ops.values())
            a_pz += s_pz; a_g += s_g; a_ops |= set(ops)
            gc = nf(s_g) if s_g != s_pz else '—'
            buf.append(f'<tr><td class="sub"><span class="tn">{icon_m(icons, area, sub, 15)}{plural_sub(sub)}</span></td><td class="n">{nf(s_pz)}</td><td class="n">{gc}</td><td class="n">{len(ops)}</td></tr>')
        c0, _ = COL[area]
        tb.append(f'<tr class="ar" style="background:{c0}"><td><span class="tn">{area_icon_m(area, 18, c0)}{area_lbl(area)}</span></td><td class="n">{nf(a_pz)}</td><td class="n">{nf(a_g)}</td><td class="n">{len(a_ops)}</td></tr>')
        tb += buf; G_PZ += a_pz; G_G += a_g; G_OP += len(a_ops)
    return ('<table class="mx">\n<thead><tr><th>Área / Subárea</th><th class="n">Piezas</th><th class="n">Golpes</th><th class="n">OPs</th></tr></thead>\n'
            '<tbody>' + ''.join(tb) + f'\n<tr class="gt-grand"><td>TOTAL</td><td class="n">{nf(G_PZ)}</td><td class="n">{nf(G_G)}</td><td class="n">{G_OP}</td></tr>\n</tbody></table>')
 
def build_gantt(groups, icons):
    def strip():
        out = []
        for i, d in enumerate(GRID):
            c = ['gc']
            if i % 2 == 1: c.append('alt')
            if i > 0 and d.weekday() == 0: c.append('wk')
            if d < W0: c.append('past')
            out.append(f'<div class="{" ".join(c)}"></div>')
        return ''.join(out)
    STRIP = strip()
    def pct(x): return f'{x/N*100:.3f}%'
    # separa "esta semana" de la previa y de la próxima con una línea punteada,
    # superpuesta encima de las barras (misma posición que los bordes .wk)
    WEEK_BOUNDARIES = [i for i, d in enumerate(GRID) if i > 0 and d.weekday() == 0]
    WK_LINES = ''.join(f'<div class="wksep" style="left:{pct(i)}"></div>' for i in WEEK_BOUNDARIES)
    def bar(info, c0, c1, use_g, area='', sub=''):
        fe = info['femit'] or info['fprod']; fp = info['fprod']
        clamp = fe < GRID[0]
        start = 0 if clamp else GIDX.get(fe, 0)
        end = GIDX.get(fp, N-1)
        w = end + 1 - start
        if info.get('partial'):
            qty = (f'{nf(info["golpe"])} de {nf(info["tot_golpe"])} golpes' if use_g
                   else f'{nf(info["cant"])} de {nf(info["tot_cant"])} pzas')
        else:
            qty = f'{nf(info["golpe"])} golpes' if use_g else f'{nf(info["cant"])} ' + ('pza' if info["cant"] == 1 else 'pzas')
        det = detalle_m(area, sub, info)
        if det: qty = qty + ' · ' + html_esc(det)
        cap = f'<span class="cap{" clamp" if clamp else ""}" style="background:{c1}"></span>'
        return f'<div class="bar" style="left:{pct(start)};width:{pct(w)};background:{c0}">{cap}<span class="bq">{qty}</span></div>'
    def emit_lbl(info):
        if not info['femit']: return ''
        d = info['femit']; t = f'{d.day}/{MES[d.month]}'
        return ('●' if d >= GRID[0] else '◀') + ' ' + t
    def week_label_row():
        return ('<div class="wklrow"><div class="wkl-sp"></div><div class="wklcal">'
                f'<div class="wkl past" style="flex:6">sem previa · {GRID[0].day}–{GRID[5].day} {MES[GRID[5].month]}</div>'
                f'<div class="wkl" style="flex:6">ESTA semana · {GRID[6].day}–{GRID[11].day}</div>'
                f'<div class="wkl" style="flex:6">PRÓXIMA · {GRID[12].day} {MES[GRID[12].month]} – {GRID[17].day} {MES[GRID[17].month]}</div></div></div>')
    def day_header_row():
        dch = ['<div class="ghdr"><div class="hlab"></div>']
        for i, d in enumerate(GRID):
            c = ['dch']
            if i % 2 == 1: c.append('alt')
            if i > 0 and d.weekday() == 0: c.append('wk')
            if d < W0: c.append('past')
            dch.append(f'<div class="{" ".join(c)}">{DOW[d.weekday()]}<span class="dcn">{d.day}</span></div>')
        dch.append('</div>'); return ''.join(dch)
 
    areas = areas_present(groups)
    gz = ['<div class="gantt">', week_label_row(), day_header_row()]
    for area in areas:
        c0, c1 = COL.get(area, ('#475569', '#1f2937'))
        if area == PAGE2_AT and area != areas[0]:
            gz += ['<div class="page-break"></div>', week_label_row(), day_header_row()]
        gz.append(f'<div class="grow garow"><div class="glabel gareah" style="border-left:4px solid {c0};color:{c0}">{area_lbl(area)}</div><div class="gcal gareac">{STRIP}{WK_LINES}</div></div>')
        for sub in subs_of(groups, area):
            ops = dict(ops_of(groups, area, sub))
            ug = usa_golpe(area, sub)
            tot = f'{nf(sum(o["golpe"] for o in ops.values()))} g' if ug else f'{nf(sum(o["cant"] for o in ops.values()))} pz'
            icon = icons.get(area + '||' + sub, '') or ''
            gz.append(f'<div class="grow gsrow"><div class="glabel gsubh">{icon}{sub} · {len(ops)} OP · {tot}</div><div class="gcal gsubc">{STRIP}{WK_LINES}</div></div>')
            for op, info in ops_of(groups, area, sub):
                age = age_days(info['femit'])
                stale = ' stale' if (age is not None and age >= 90) else ''
                gz.append(f'<div class="grow{stale}"><div class="glabel"><span class="gop">{op}</span><span class="gemit">{emit_lbl(info)}</span>{age_badge(age)}</div><div class="gcal">{STRIP}{bar(info,c0,c1,ug,area,sub)}{WK_LINES}</div></div>')
    gz.append('</div>')
    return '\n'.join(gz)
 
def build_carga_dia(groups):
    day_load = {d: 0 for d in GRID}
    for (area, sub), ops in groups.items():
        ug = usa_golpe(area, sub)
        for info in ops.values():
            if info['fprod'] in day_load:
                day_load[info['fprod']] += info['golpe'] if ug else info['cant']
    dt = ['<div class="dtot"><div class="dtl">Carga / día</div>']
    for i, d in enumerate(GRID):
        c = ['dct']
        if i > 0 and d.weekday() == 0: c.append('wk')
        if d < W0: c.append('past')
        v = '·' if day_load[d] == 0 else nf(day_load[d])
        dt.append(f'<div class="{" ".join(c)}">{v}</div>')
    dt.append('</div>')
    return ''.join(dt)
 
# ============================================================
# PASO 5 — CSS + ensamblado final
# ============================================================
CSS = '''
@page{size:letter landscape;margin:7mm}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1e293b;padding:4px 10px;margin:0;background:#fff}
h1{font-size:15px;margin:0 0 1px 0;font-weight:700}
.subtitle{font-size:9.5px;margin:0 0 2px 0;color:#475569}
.fresh{font-size:7.8px;margin:0 0 4px 0;color:#94a3b8}
.sect{font-size:9.5px;font-weight:700;color:#334155;margin:5px 0 2px 0;text-transform:uppercase;letter-spacing:.03em}
table.mx{width:60%;border-collapse:collapse;font-size:9.8px}
table.mx th{text-align:left;font-size:8.4px;color:#64748b;font-weight:600;padding:1.3px 6px;border-bottom:1.5px solid #334155;text-transform:uppercase}
table.mx td{padding:1.1px 6px;border-bottom:1px solid #e2e8f0}
table.mx td.n{text-align:right;font-variant-numeric:tabular-nums}
table.mx tr.ar td{color:#fff;font-weight:700;padding:1.6px 6px}
.ai{display:inline-flex;align-items:center;justify-content:center;background:#fff;border-radius:4px;flex:none}.ai svg{display:block;flex:none}
table.mx tr.ar td.n{font-weight:700}
table.mx td.sub{padding-left:14px;color:#475569}
.tn{display:flex;align-items:center;gap:6px}
table.mx td{vertical-align:middle}
table.mx tr.ar td{height:25px;padding-top:0;padding-bottom:0}.tn .ai{margin-right:0}
table.mx td{letter-spacing:.015em}
table.mx tr.gt-grand td{border-top:2px solid #1e293b;border-bottom:none;font-weight:800;padding-top:2px}
.gantt{width:100%;margin-top:1px}
.wklrow{display:flex;height:10px;margin-bottom:1px}
.wkl-sp{width:176px;flex-shrink:0}
.wklcal{flex:1;display:flex}
.wkl{font-size:7.6px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.02em;display:flex;align-items:flex-end;padding-bottom:1px}
.wkl.past{color:#b6bfcc}
.ghdr{display:flex;height:12px;margin-bottom:1px}
.hlab{width:158px;flex-shrink:0}
.dch{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:6.9px;font-weight:700;color:#94a3b8;border-left:1px solid #f1f5f9}
.dch.alt{background:#f8fafc}
.dch.wk{border-left:1.5px dashed #94a3b8}
.dch.past{color:#cbd5e1}
.dcn{font-size:7.4px;font-weight:700;color:#334155}
.dch.past .dcn{color:#94a3b8}
.grow{display:flex;height:16px;align-items:center;position:relative}
.grow.stale{background:#fdf1f4}
.garow{height:18px}
.gsrow{height:23px}
.glabel{letter-spacing:.012em;width:176px;flex-shrink:0;font-size:8.8px;display:flex;align-items:center;gap:3px;padding-right:4px;overflow:hidden;white-space:nowrap}
.gareah{padding-left:4px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.03em}
.gsubh{font-size:8.6px;font-weight:700;color:#334155}
.gop{font-weight:700;color:#1e293b;font-size:8.8px}
.gemit{font-size:7.4px;color:#475569}
.agebadge{display:inline-flex;align-items:center;justify-content:center;height:10.5px;box-sizing:border-box;font-size:6.8px;font-weight:700;padding:1.5px 4px 0;border-radius:6px;line-height:1;letter-spacing:.02em}
.ab-g{background:#e2e8f0;color:#475569}.ab-y{background:#fdf0a6;color:#7a6200}.ab-o1{background:#fcd9a0;color:#8a4b00}.ab-o2{background:#f8b980;color:#7a3500}.ab-o3{background:#f29a6b;color:#6b2300}.ab-r{background:#e84a4a;color:#fff}
.gcal{flex:1;position:relative;height:100%}
.gareac,.gsubc{opacity:.55}
.gc{position:absolute;top:0;bottom:0;background:transparent}
.gc.alt{background:#f8fafc}
.gc.wk{border-left:1.5px dashed #cbd5e1}
.gc.past{background:#f1f5f9}
.bar{position:absolute;top:1.5px;bottom:1.5px;border-radius:3px;display:flex;align-items:center;min-width:3px}
.cap{position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:3px 0 0 3px}
.cap.clamp{width:5px}
.bq.out{position:absolute;left:calc(100% + 4px);margin:0;color:#334155;background:#fff}.bq.outl{position:absolute;right:calc(100% + 4px);margin:0;color:#334155;background:#fff}
.bq{position:relative;z-index:5;background:inherit;font-size:8.1px;line-height:1.25;letter-spacing:.025em;padding:0 3px;color:#fff;font-weight:700;margin-left:5px;white-space:nowrap}
.wksep{position:absolute;top:0;bottom:0;width:0;border-left:1.3px dashed #94a3b8;pointer-events:none;z-index:4}
.dtot{display:flex;height:17px;align-items:center;margin-top:2px;border-top:1.5px solid #1e293b}
.dtl{width:176px;flex-shrink:0;font-size:9.8px;font-weight:800;color:#1e293b}
.dct{flex:1;text-align:center;font-size:8.2px;font-weight:700;color:#1e293b;border-left:1px solid #f1f5f9}
.dct.wk{border-left:1.5px dashed #94a3b8}
.dct.past{color:#94a3b8;font-weight:600}
.lgnd{display:flex;gap:16px;font-size:8.2px;color:#64748b;margin-top:5px;flex-wrap:wrap}
.lgnd b{color:#334155}
.lgnd i{font-style:normal;font-weight:700;padding:0 4px;border-radius:5px;margin-left:2px}
.page-break{break-before:page;page-break-before:always;height:0}
'''
 
def assemble(table, gantt, carga_dia, entregas_dt):
    subtitle = (f'Semana (lun {GRID[6].day} – sáb {GRID[11].day} {MES[GRID[11].month]}) + '
                f'próxima (lun {GRID[12].day} – sáb {GRID[17].day} {MES[GRID[17].month]})')
    fresh = f'último cierre reflejado: {fmt_dmy(entregas_dt)}' if entregas_dt else ''
    today_disp = f'{TODAY.day}/{MES[TODAY.month]}/{str(TODAY.year)[-2:]}'
    grid_start = f'{GRID[0].day}/{MES[GRID[0].month]}'
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Resumen ejecutivo · carga de producción</title>
<style>{CSS}</style></head><body>
<h1>Resumen · Carga de producción pendiente</h1>
<div class="subtitle">{subtitle}</div>
<div class="fresh">{fresh} · generado {today_disp}</div>
<div class="sect">Totales por área y subárea</div>
{table}
<div class="sect">Gantt · OPs por área / subárea — barra de fecha de creación a fecha programada de entrega</div>
{gantt}
{carga_dia}
<div class="lgnd">
<span><b>●</b> fecha de emisión real (dentro de la línea)</span>
<span><b>◀</b> OP creada antes del {grid_start} — barra recortada al borde</span>
<span>tope oscuro = inicio de barra</span>
<span>días abierta: <i class="ab-g">&lt;15</i><i class="ab-y">15</i><i class="ab-o1">30</i><i class="ab-o2">45</i><i class="ab-o3">60</i><i class="ab-r">90+</i></span>
</div>
</body></html>'''
 

# ============================================================
# VERSIÓN PARA CELULAR — un solo PDF largo, ancho de pantalla de teléfono
# ============================================================
# Varios usa el mismo gris que sus íconos en el panel (en la versión carta se
# queda morado como siempre, para no cambiar lo que ya se manda a gerencia).
COL_M = {'Disco':'#2563eb', 'Aros':'#059669', 'Telar':'#fb923c', 'Varios':'#64748b', 'ALMC':'#c77dff'}
AC_M = {'Disco':'#2563eb', 'Aros':'#059669', 'Telar':'#fb923c', 'Varios':'#94a3b8', 'ALMC':'#c77dff'}
MOVIL_W = 390   # px CSS ≈ ancho de un celular

def age_class_m(age):
    if age is None: return 'g'
    return ('g' if age < 15 else 'y' if age < 30 else 'o1' if age < 45
            else 'o2' if age < 60 else 'o3' if age < 90 else 'r')

def icon_m(icons, area, sub, size):
    h = icons.get(area + '||' + sub, '') or ''
    if not h: return ''
    inner = round(size * 0.8)
    h = re.sub(r'width:\d+px;height:\d+px', f'width:{size}px;height:{size}px', h, count=1)
    h = re.sub(r'<svg width="\d+" height="\d+"', f'<svg width="{inner}" height="{inner}"', h, count=1)
    return h.replace('margin-right:5px', 'margin-right:0')

AREA_SVG = {
 'Disco':'<defs><clipPath id="ai-d"><circle cx="16" cy="16" r="10"/></clipPath></defs><circle cx="16" cy="16" r="10" stroke-width="2.4"/><g clip-path="url(#ai-d)" stroke-width="1.1"><line x1="6" y1="12" x2="26" y2="12"/><line x1="6" y1="16" x2="26" y2="16"/><line x1="6" y1="20" x2="26" y2="20"/><line x1="12" y1="6" x2="12" y2="26"/><line x1="16" y1="6" x2="16" y2="26"/><line x1="20" y1="6" x2="20" y2="26"/></g>',
 'Aros':'<circle cx="16" cy="16" r="13" stroke-width="2.4" fill="__C__" fill-opacity=".42"/><circle cx="16" cy="16" r="8.6" fill="#fff"/><circle cx="16" cy="16" r="8.6" stroke-width="2.4" fill="__C__" fill-opacity=".16"/>',
 'Telar':'<path d="M11 6v20M21 6v20M6 11h20M6 21h20" stroke-width="1.5"/><g fill="__C__" stroke="none"><circle cx="11" cy="11" r="2.1"/><circle cx="21" cy="11" r="2.1"/><circle cx="11" cy="21" r="2.1"/><circle cx="21" cy="21" r="2.1"/></g>',
 'Varios':'<rect x="6" y="6" width="8" height="8" rx="1.5"/><rect x="18" y="6" width="8" height="8" rx="1.5"/><rect x="6" y="18" width="8" height="8" rx="1.5"/><circle cx="22" cy="22" r="4.2"/>',
 'ALMC':'<path d="M5 10.5l11-6 11 6v12l-11 6-11-6z"/><path d="M5 10.5l11 6 11-6M16 16.5v12"/>'}

def area_icon_m(area, size=38, color=None):
    """Ícono de área (mismo dibujo que el panel) en cajita blanca sobre la barra de color."""
    c = color or AC_M.get(area, '#64748b'); P = AREA_SVG.get(area)
    if not P: return ''
    inner = 2 * round(size * (0.85 if area == 'Aros' else 0.74) / 2)
    return (f'<span class="ai" style="width:{size}px;height:{size}px"><svg viewBox="0 0 32 32" width="{inner}" height="{inner}" fill="none" '
            f'stroke="{c}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">{P.replace("__C__", c)}</svg></span>')

def fmt_dm(d): return f'{d.day} {MES[d.month]}'

def _first_plus(vals, fmt=lambda v: v):
    """'x(+n)': el primer valor y cuántos distintos más trae la OP (otras partidas)."""
    if not vals: return ''
    return fmt(vals[0]) + (f'(+{len(vals)-1})' if len(vals) > 1 else '')

def detalle_m(area, sub, o):
    """Línea chica bajo cada OP: malla x(+n) · tamaño (Ø en discos/packs; en aros, solo tamaño) · material si no es 304."""
    partes = []
    if area != 'Aros':
        m = _first_plus(o['mesh'])
        if m: partes.append('malla ' + m)
    t = _first_plus(o['tq'], (lambda v: 'Ø' + v) if (area == 'Disco' and sub in ('Disco', 'Pack', 'Tapas')) else (lambda v: v))
    if t: partes.append(t)
    mats = [m for m in o['mat'] if m.upper() != '304']
    if mats: partes.append(mats[0].capitalize() + ('(+%d)' % (len(mats)-1) if len(mats) > 1 else ''))
    return ' · '.join(partes)

def nfm(n): return f'{int(n):,}'

def html_esc(t): return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def build_movil(groups, icons, entregas_dt):
    areas = areas_present(groups)
    rows_t = []; G_PZ = G_G = 0; G_OPS = set()
    for area in areas:
        a_pz = a_g = 0; a_ops = set(); buf = []
        for sub in subs_of(groups, area):
            ops = dict(ops_of(groups, area, sub))
            s_pz = sum(o['cant'] for o in ops.values())
            s_g = sum((o['golpe'] if usa_golpe(area, sub) else o['cant']) for o in ops.values())
            a_pz += s_pz; a_g += s_g; a_ops |= set(ops)
            gc = nfm(s_g) if s_g != s_pz else '—'
            buf.append(f'<tr><td class="sn">{icon_m(icons, area, sub, 18)}{plural_sub(sub)}</td><td>{nfm(s_pz)}</td><td>{gc}</td><td>{len(ops)}</td></tr>')
        rows_t.append(f'<tr class="ta" style="background:{COL_M[area]}"><td>{area_icon_m(area, 24)}{area_lbl(area)}</td><td>{nfm(a_pz)}</td><td>{nfm(a_g)}</td><td>{len(a_ops)}</td></tr>')
        rows_t += buf; G_PZ += a_pz; G_G += a_g; G_OPS |= a_ops
    table = ('<table class="tt"><tr class="hd"><td></td><td>PIEZAS</td><td>GOLPES</td><td>OPS</td></tr>' + ''.join(rows_t)
             + f'<tr class="tot"><td>TOTAL</td><td>{nfm(G_PZ)}</td><td>{nfm(G_G)}</td><td>{len(G_OPS)}</td></tr></table>')

    body = []
    for area in areas:
        a_ops = set(); a_pz = 0
        for sub in subs_of(groups, area):
            a_ops |= set(groups[(area, sub)]); a_pz += sum(o['cant'] for o in groups[(area, sub)].values())
        body.append(f'<div class="ar" style="background:{COL_M[area]}"><b>{area_lbl(area)}</b><span>{len(a_ops)} OP{'s' if len(a_ops) != 1 else ''} · {nfm(a_pz)} pzas</span></div>')
        for sub in subs_of(groups, area):
            ops = ops_of(groups, area, sub)
            ug = usa_golpe(area, sub)
            tot = f'{nfm(sum(o["golpe"] for _, o in ops))} golpes' if ug else f'{nfm(sum(o["cant"] for _, o in ops))} pzas'
            body.append(f'<div class="sb">{icon_m(icons, area, sub, 32)}<b>{plural_sub(sub)}</b><span>{len(ops)} OP{"s" if len(ops) > 1 else ""} · {tot}</span></div>')
            for op, o in ops:
                age = age_days(o['femit']); late = o['fprod'] < TODAY
                ent = (f'<em class="late">venc. {fmt_dm(o["fprod"])}</em>' if late else fmt_dm(o['fprod']))
                if o.get('partial'):
                    q = f'{nfm(o["cant"])} <small>de {nfm(o["tot_cant"])}</small>'
                else:
                    q = f'{nfm(o["cant"])} <small>pzas</small>'
                gcell = ''
                if ug and o['golpe'] != o['cant']:
                    gcell = f'{nfm(o["golpe"])} g'
                chip = f'<u class="ag {age_class_m(age)}">{age} d</u>' if age is not None else '<u class="ag g">–</u>'
                det = detalle_m(area, sub, o)
                r2 = f'<div class="r2">{html_esc(det)}</div>' if det else ''
                body.append(f'<div class="op" style="border-left-color:{COL_M[area]}"><div class="r1"><b>{op}</b><span class="q">{q}</span><i>{gcell}</i>{chip}<s>{ent}</s></div>{r2}</div>')
    sub_t = (f'Semana (lun {GRID[6].day} – sáb {GRID[11].day} {MES[GRID[11].month]}) + '
             f'próxima (lun {GRID[12].day} – sáb {GRID[17].day} {MES[GRID[17].month]})')
    today_disp = f'{TODAY.day}/{MES[TODAY.month]}/{str(TODAY.year)[-2:]}'
    fresh = (f' · último cierre: {fmt_dmy(entregas_dt)}' if entregas_dt else '')
    legend = ('<div class="leg"><b>Días abierta:</b><u class="ag g">&lt;15</u><u class="ag y">15</u><u class="ag o1">30</u>'
              '<u class="ag o2">45</u><u class="ag o3">60</u><u class="ag r">90+</u></div>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Resumen ejecutivo · carga de producción (celular)</title>
<style>{CSS_M}</style></head><body><div class="pg">
<h1>Carga de producción pendiente</h1><div class="sub">{sub_t} · generado {today_disp}{fresh}</div>
<div class="th"><span>TOTALES POR ÁREA / SUBÁREA</span></div>{table}
{legend}<div class="th" style="margin-top:10px"><span>OPs POR ÁREA / SUBÁREA</span><em>días abierta</em></div>
{''.join(body)}</div></body></html>"""

CSS_M = """.pg{--icbase:#fff}*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif}
.pg{width:390px;background:#fff;padding:14px 12px 18px;color:#0f172a}
h1{font-size:17px;margin:0 0 2px;line-height:1.2}.sub{font-size:10.5px;color:#64748b;margin-bottom:10px}
.th{display:flex;justify-content:space-between;align-items:baseline;font-size:10.5px;font-weight:800;letter-spacing:.6px;color:#334155;margin-bottom:4px}.th em{font-style:normal;font-weight:500;letter-spacing:0;color:#94a3b8}
.tt{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}.tt td{padding:2.5px 6px;text-align:right}.tt td:first-child{text-align:left}
.tt .hd td{font-size:9.5px;color:#94a3b8;font-weight:700;padding-bottom:3px}.tt .ta td{color:#fff;font-weight:800;padding:4px 6px}.tt .sn{display:flex;align-items:center;gap:6px;padding-left:8px}.tt .tot td{border-top:2px solid #0f172a;font-weight:800;padding-top:4px}
.ar{display:flex;justify-content:space-between;align-items:center;color:#fff;border-radius:6px;padding:5px 9px;margin:12px 0 2px;font-size:13px}.ar span{font-size:11px;opacity:.95}.ai{display:inline-flex;align-items:center;justify-content:center;background:#fff;border-radius:5px;vertical-align:middle;margin-right:7px}.ai svg{display:block;flex:none}.ar span{font-size:11px;opacity:.95}
.sb{display:flex;align-items:center;gap:9px;padding:8px 2px 4px;font-size:12.5px;border-bottom:1px solid #cbd5e1;break-after:avoid}.sb b{flex:1}.sb span{font-size:10.5px;color:#64748b}
.op{padding:3px 2px 3px 7px;border-left:3px solid;border-bottom:1px solid #f1f5f9;font-size:12.5px;font-variant-numeric:tabular-nums;margin-left:2px}
.r1{display:flex;align-items:center;gap:6px}.r2{font-size:10px;color:#64748b;margin:0 0 1px 1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.op b{width:46px;font-size:13px}.op .q{width:78px;text-align:right}.op small{font-size:9.5px;color:#64748b}.op i{width:46px;text-align:right;font-style:normal;font-size:11px;color:#64748b}.op s{flex:1;text-align:right;text-decoration:none;font-size:11.5px;color:#334155}
.ag{font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;text-decoration:none;width:38px;text-align:center;display:inline-block}
.ag.g{background:#e2e8f0;color:#475569}.ag.y{background:#fdf0a6;color:#7a6200}.ag.o1{background:#fcd9a0;color:#8a4b00}.ag.o2{background:#f8b980;color:#7a3500}.ag.o3{background:#f29a6b;color:#6b2300}.ag.r{background:#e84a4a;color:#fff}
.late{color:#c62828;font-style:normal;font-weight:700}
.leg{display:flex;align-items:center;gap:5px;margin-top:12px;font-size:10.5px;color:#475569}.leg b{margin-right:2px}.leg .ag{width:auto;min-width:30px}
"""

def export_movil(workdir, groups, entregas_dt):
    icons = resolve_icons(workdir, groups, palette=AC_M, tweak=False)
    html = build_movil(groups, icons, entregas_dt)
    out_html = os.path.join(workdir, 'resumen_ejecutivo_movil.html')
    open(out_html, 'w', encoding='utf-8').write(html)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': MOVIL_W, 'height': 800})
        pg.goto('file://' + os.path.abspath(out_html))
        pg.wait_for_timeout(300)
        h = pg.evaluate("Math.ceil(document.querySelector('.pg').getBoundingClientRect().height)")
        out_pdf = os.path.join(workdir, 'resumen_ejecutivo_movil.pdf')
        # una sola página tan alta como el contenido (+margen): sin cortes al leerlo en el celular
        pg.pdf(path=out_pdf, width=f'{MOVIL_W}px', height=f'{h + 4}px', print_background=True,
               margin={'top': '0', 'bottom': '0', 'left': '0', 'right': '0'}, page_ranges='1')
        pg.screenshot(path=os.path.join(workdir, 'resumen_ejecutivo_movil_preview.png'), full_page=True)
        b.close()
    print('Escrito', out_pdf, f'(alto {h}px)')

# ============================================================
# MAIN
# ============================================================
def main(workdir='.'):
    V, entregas_dt, _, op_totals = load_and_filter(workdir)
    groups = group_rows(V, op_totals)
    icons = resolve_icons(workdir, groups)
    table = build_table(groups, icons)
    gantt = build_gantt(groups, icons)
    carga_dia = build_carga_dia(groups)
    html = assemble(table, gantt, carga_dia, entregas_dt)
    out_html = os.path.join(workdir, 'resumen_ejecutivo.html')
    open(out_html, 'w', encoding='utf-8').write(html)
    print('Escrito', out_html)
 
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': 1003, 'height': 763})   # = área imprimible Letter horizontal (márgenes 7mm)
        pg.emulate_media(media='print')
        pg.goto('file://' + os.path.abspath(out_html))
        pg.wait_for_timeout(300)
        pg.evaluate("""() => {
          // 1) texto de barra: si no cabe dentro, va fuera (derecha/izquierda); si tampoco, se recorta con …
          document.querySelectorAll('.bar').forEach(b => {
            const q = b.querySelector('.bq'); if (!q) return;
            const cal = b.parentElement.getBoundingClientRect(), br = b.getBoundingClientRect();
            const need = q.getBoundingClientRect().width + 6;
            if (need <= br.width) return;
            const right = cal.right - br.right, left = br.left - cal.left;
            if (right >= need) q.classList.add('out');
            else if (left >= need) q.classList.add('outl');
            else { q.style.maxWidth = (br.width - 8) + 'px'; q.style.overflow = 'hidden'; q.style.textOverflow = 'ellipsis'; }
          });
          // 2) paginado: corta antes de la fila que se pasa de la hoja (de preferencia antes del título de subárea/área) y repite encabezado de días
          const PAGE_H = 763 - 6, g = document.querySelector('.gantt');
          const y = el => el.getBoundingClientRect().top + window.scrollY;
          const hdr = [g.children[0], g.children[1]];
          const mkBreak = (before, contSub) => {
            const br = document.createElement('div'); br.className = 'page-break';
            g.insertBefore(br, before);
            hdr.forEach(h => g.insertBefore(h.cloneNode(true), before));
            if (contSub) g.insertBefore(contSub.cloneNode(true), before);   // repite el título de la subárea que continúa
          };
          let pageTop = 0;
          let rows = [...g.children].slice(2);
          for (let i = 0; i < rows.length; i++) {
            const r = rows[i];
            if (r.classList.contains('page-break') || r.classList.contains('wklrow') || r.classList.contains('ghdr')) continue;
            const bottom = y(r) + r.getBoundingClientRect().height;
            if (bottom - pageTop > PAGE_H) {
              let target = r, cont = null;
              const idx = [...g.children].indexOf(r);
              for (let k = idx; k >= Math.max(2, idx - 2); k--) {       // si hay un título a ≤2 filas, corta antes de él
                const c = g.children[k];
                if (c.classList.contains('gsrow') || c.classList.contains('garow')) { target = c; if (c.classList.contains('garow')) break; }
              }
              if (target === r && !r.classList.contains('gsrow') && !r.classList.contains('garow')) {
                for (let k = idx; k >= 2; k--) { const c = g.children[k]; if (c.classList.contains('gsrow')) { cont = c; break; } if (c.classList.contains('garow')) break; }
              }
              mkBreak(target, cont);
              const hh = hdr.reduce((a, h) => a + h.getBoundingClientRect().height, 0);
              pageTop = y(target) - hh - (cont ? cont.getBoundingClientRect().height : 0);
              i = rows.indexOf(target) - 1;
            }
          }
          // 3) carga/día + leyenda deben caber en la última página
          const dt = document.querySelector('.dtot'), lg = document.querySelector('.lgnd');
          if (lg && (y(lg) + lg.getBoundingClientRect().height - pageTop > PAGE_H)) {
            const br = document.createElement('div'); br.className = 'page-break'; dt.parentNode.insertBefore(br, dt);
            hdr.forEach(h => dt.parentNode.insertBefore(h.cloneNode(true), dt));
          }
        }""")
        out_pdf = os.path.join(workdir, 'resumen_ejecutivo.pdf')
        pg.pdf(path=out_pdf, format='Letter', landscape=True, print_background=True,
               margin={'top':'7mm','bottom':'7mm','left':'7mm','right':'7mm'})
        pg.screenshot(path=os.path.join(workdir, 'resumen_ejecutivo_preview.png'), full_page=True)
        b.close()
    print('Escrito', out_pdf, '— revisar pdfinfo (debe dar 2 páginas) antes de entregar.')
    export_movil(workdir, groups, entregas_dt)
 
if __name__ == '__main__':
    main('.')
 
