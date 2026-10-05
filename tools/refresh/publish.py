#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paso final del refresh automático: copia los resultados a los dos repos y deja
cierres.json / overrides.json con SOLO lo que llegó después de la corrida.

Uso: publish.py --out OUT --snap-cierres SNAP --snap-overrides SNAP --prod-repo DIR --mtx-repo DIR

SNAP = copia de cierres/overrides que se le dio a refresh.py (ya horneados en el RAW).
En el repo actual puede haber entradas nuevas (alguien cerró una OP mientras corría); esas se conservan.
"""
import argparse, json, os, shutil

def key(e): return json.dumps(e, sort_keys=True, ensure_ascii=False)

def rest(live_path, snap_path):
    live = json.load(open(live_path, encoding='utf-8')) if os.path.exists(live_path) else []
    snap = json.load(open(snap_path, encoding='utf-8')) if os.path.exists(snap_path) else []
    done = {key(e) for e in snap}
    return [e for e in live if key(e) not in done]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True); ap.add_argument('--prod-repo', required=True); ap.add_argument('--mtx-repo', required=True)
    ap.add_argument('--snap-cierres', required=True); ap.add_argument('--snap-overrides', required=True)
    a = ap.parse_args()
    for name in ('cierres.json', 'overrides.json'):
        snap = a.snap_cierres if name == 'cierres.json' else a.snap_overrides
        live = os.path.join(a.prod_repo, name)
        left = rest(live, snap)
        json.dump(left, open(live, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('%s: quedan %d entrada(s) sin consolidar' % (name, len(left)))
    shutil.copy(os.path.join(a.out, 'index.html'), os.path.join(a.prod_repo, 'index.html'))
    shutil.copy(os.path.join(a.out, 'matex.html'), os.path.join(a.mtx_repo, 'matex.html'))
    shutil.copy(os.path.join(a.out, 'historico.json'), os.path.join(a.mtx_repo, 'historico.json'))

if __name__ == '__main__':
    main()
