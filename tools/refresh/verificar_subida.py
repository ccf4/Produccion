#!/usr/bin/env python3
"""Compara el index.html anterior con el nuevo y avisa si la subida BORRA datos.

Por qué existe: si alguien sube un index.html armado sobre una versión vieja, pisa en silencio
lo que el refresh automático ya había publicado (pasó el 12/oct, 19/oct y 23/oct de 2026).
El index guarda TODA la historia, así que nada de lo que ya estaba debe desaparecer:
  - OPs (RAW), pedidos (PEDIDOS), clientes (CLIENTES), artículos (ARTICULOS)
  - ninguna OP cerrada (T) debe volver a abierta (V)
Uso: verificar_subida.py <index_anterior.html> <index_nuevo.html>   (sale con 1 si detecta pérdidas)
"""
import json, re, sys

def consts(path):
    s = open(path, encoding='utf-8').read()
    out = {}
    for n in ('RAW', 'PEDIDOS', 'CLIENTES', 'ARTICULOS'):
        m = re.search(r'const %s=(\{.*?\}|\[.*?\]);\s*\n' % n, s)
        out[n] = json.loads(m.group(1)) if m else None
    return out

def main(old, new):
    a, b = consts(old), consts(new)
    problemas = []
    for n in ('RAW', 'PEDIDOS', 'CLIENTES', 'ARTICULOS'):
        if b[n] is None: problemas.append('%s: el index nuevo ya no trae esta constante' % n); return problemas
    ops = lambda c: {r['OP'] for r in c['RAW']}
    for etq, x, y in (('OPs', ops(a), ops(b)), ('PEDIDOS', set(a['PEDIDOS']), set(b['PEDIDOS'])),
                      ('CLIENTES', set(a['CLIENTES']), set(b['CLIENTES'])), ('ARTICULOS', set(a['ARTICULOS']), set(b['ARTICULOS']))):
        perd = sorted(x - y)
        if perd: problemas.append('Se borraron %d de %s: %s%s' % (len(perd), etq, ', '.join(perd[:15]), ' …' if len(perd) > 15 else ''))
    st = lambda c: {(r['OP'], r['PART']): r['STATUS'] for r in c['RAW']}
    sa, sb = st(a), st(b)
    rev = sorted({k[0] for k, v in sa.items() if v == 'T' and sb.get(k) == 'V'})
    if rev: problemas.append('Se reabrieron OPs que estaban cerradas (T→V): %s' % ', '.join(rev[:15]))
    return problemas

if __name__ == '__main__':
    p = main(sys.argv[1], sys.argv[2])
    if p:
        print('⚠ ESTA SUBIDA PISA DATOS QUE YA ESTABAN PUBLICADOS (probablemente se armó sobre una versión vieja del index):')
        for x in p: print(' -', x)
        print('Sube de nuevo un index.html basado en el actual de GitHub (o revierte este commit).')
        sys.exit(1)
    print('OK: la subida no borra datos.')
