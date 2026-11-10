#!/usr/bin/env python3
"""guard_probar_s3: prueba un modelo exportado (.npz) con los rasgos de
capturas que no vio al entrenar y aplica las reglas de alarma del equipo.
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, sys.argv[4] if len(sys.argv) > 4 else "pi_nuevo/tools")
import guard_clasificador as gcl
import guard_rasgos as gr

modelo = gcl.Modelo(Path(sys.argv[1]))
filas = list(csv.DictReader(open(sys.argv[2])))
nota = {Path(json.loads(l)["fichero"]).stem: json.loads(l)["nota"] + f" @ {json.loads(l)['f_centro_hz'] // 1000000} MHz"
        for l in open(sys.argv[3]) if l.strip()}
X = np.array([[float(f[n]) for n in gr.NOMBRES] for f in filas])
pred = [modelo.clases[i] for i in modelo.probabilidades(X).argmax(axis=1)]

por_grupo = defaultdict(lambda: {"v": 0, "fa_v": 0, "niv": Counter(),
                                 "r0": 0, "r2": 0, "b": 0, "conf": Counter()})
for cap in dict.fromkeys(f["captura"] for f in filas):
    idx = [i for i, f in enumerate(filas) if f["captura"] == cap]
    g = por_grupo[nota.get(cap, "?")]
    g["v"] += len(idx)
    g["fa_v"] += sum(pred[i] != gcl.NEGATIVA for i in idx)
    g["conf"].update(pred[i] for i in idx if pred[i] != gcl.NEGATIVA)
    previo = None
    for a in range(0, len(idx) - 9, 10):             # capturas de 0,5 s
        votos = Counter(pred[i] for i in idx[a:a + 10])
        clase, n = votos.most_common(1)[0]
        nv = gcl.nivel_captura(clase, n / 10)
        g["niv"][nv] += 1
        g["b"] += 1
        g["r0"] += nv != "negativo"
        g["r2"] += nv == "fuerte" or (nv == "posible" and previo not in (None, "negativo"))
        previo = nv

print(f"Modelo: {sys.argv[1]} ({modelo.meta.get('fecha')})\n")
print("| Condicion | Ventanas | FA por ventana | Capturas 0,5 s | posible | fuerte "
      "| Alarma regla anterior | Alarma regla nueva | Confundido con |")
print("|---|---|---|---|---|---|---|---|---|")
tot = Counter()
for k, g in sorted(por_grupo.items()):
    print(f"| {k} | {g['v']} | {100 * g['fa_v'] / g['v']:.1f} % | {g['b']} "
          f"| {g['niv']['posible']} | {g['niv']['fuerte']} "
          f"| {100 * g['r0'] / g['b']:.1f} % | {100 * g['r2'] / g['b']:.1f} % "
          f"| {', '.join(f'{c} {n}' for c, n in g['conf'].most_common(2)) or '-'} |")
    for x in ("v", "fa_v", "b", "r0", "r2"):
        tot[x] += g[x]
print(f"| **Total** | {tot['v']} | {100 * tot['fa_v'] / tot['v']:.1f} % | {tot['b']} | | "
      f"| {100 * tot['r0'] / tot['b']:.1f} % | {100 * tot['r2'] / tot['b']:.1f} % | |")
