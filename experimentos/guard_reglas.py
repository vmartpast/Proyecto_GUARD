#!/usr/bin/env python3
"""guard_reglas: compara reglas de alarma sobre bloques de capturas de
0,5 s a partir de las predicciones por ventana.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np

import guard_evaluar as ge

N = 10                       # ventanas por captura (0,5 s)


def bloques(pred, captura):
    """Agrupa las predicciones en bloques de N ventanas seguidas de la misma
    captura, en orden.

    @param  pred     prediccion de cada ventana
    @param  captura  captura de cada ventana
    @return lista de (captura, clase ganadora, fraccion de votos de la ganadora), en el orden del CSV
    """
    out = []
    inicio = 0
    while inicio < len(pred):
        fin = inicio
        while fin < len(pred) and captura[fin] == captura[inicio]:
            fin += 1
        for a in range(inicio, fin - N + 1, N):
            clase, n = Counter(pred[a:a + N]).most_common(1)[0]
            out.append((captura[a], clase, n / N))
        inicio = fin
    return out


def nivel(clase, frac):
    """Clasifica la confianza de un bloque segun su clase y la fraccion
    de votos de la clase ganadora.

    @param  clase  clase ganadora del bloque
    @param  frac   fraccion de votos de la clase ganadora
    @return "fuerte", "posible" o "negativo" para un bloque
    """
    if clase == ge.NEGATIVA or frac < 0.5:
        return "negativo"
    return "fuerte" if frac >= 0.7 else "posible"


def aplicar(bl, regla):
    """Decide la alarma de cada bloque con una regla.

    @param  bl     bloques de bloques()
    @param  regla  "R0", "R1" o "R2"
    @return array booleano: alarma por bloque
    """
    alarmas = []
    previo = None
    for cap, clase, frac in bl:
        nv = nivel(clase, frac)
        if regla == "R0":
            a = nv != "negativo"
        elif regla == "R1":
            a = nv == "fuerte"
        else:
            a = nv == "fuerte" or (
                nv == "posible" and previo is not None
                and previo[0] == cap and previo[1] != "negativo")
        alarmas.append(a)
        previo = (cap, nv)
    return np.array(alarmas)


def main() -> int:
    """Evalua las reglas de alarma y escribe el informe.

    @return codigo de salida
    """
    p = argparse.ArgumentParser(description="Comparar reglas de alarma")
    for n in ("ext_rasgos", "ext_manifest", "pro_rasgos", "pro_manifest"):
        p.add_argument(n, type=Path)
    a = p.parse_args()

    Xe, ye, ce, _, nombres = ge.leer(a.ext_rasgos, a.ext_manifest)
    Xp, yp_orig, cp, sp, _ = ge.leer(a.pro_rasgos, a.pro_manifest)
    usar = [i for i, n in enumerate(nombres) if n not in ge.POTENCIA]
    Xe, Xp = Xe[:, usar], Xp[:, usar]
    yp = np.full(len(yp_orig), ge.NEGATIVA)

    n_min = min(len(set(ce[ye == e])) for e in set(ye))
    k = max(2, min(5, n_min))
    pl = ge.pliegues_externos(ye, ce, k)
    pred_e = np.empty(len(ye), dtype=object)
    for i in range(k):
        tr, te = pl != i, pl == i
        m = ge.bosque().fit(np.vstack([Xe[tr], Xp]),
                            np.concatenate([ye[tr], yp]))
        pred_e[te] = m.predict(Xe[te])
    pred_p = np.empty(len(yp), dtype=object)
    for s in sorted(set(sp)):
        tr, te = sp != s, sp == s
        m = ge.bosque().fit(np.vstack([Xe, Xp[tr]]),
                            np.concatenate([ye, yp[tr]]))
        pred_p[te] = m.predict(Xp[te])

    be = bloques(pred_e, ce)
    bp = bloques(pred_p, cp)
    clase_cap = dict(zip(ce, ye))
    emisor_cap = dict(zip(cp, yp_orig))
    real_e = np.array([clase_cap[c] != ge.NEGATIVA for c, _, _ in be])
    emisor_p = np.array([emisor_cap[c] for c, _, _ in bp])

    print(f"Capturas de 0,5 s: externas {len(be)}, propias {len(bp)}\n")
    print("| Regla | Sensibilidad UAV | FA externas (sin_uav) "
          "| FA propias | FA bluetooth_audio |")
    print("|---|---|---|---|---|")
    for r in ("R0", "R1", "R2"):
        ae = aplicar(be, r)
        ap = aplicar(bp, r)
        bt = emisor_p == "bluetooth_audio"
        print(f"| {r} | {100 * ae[real_e].mean():.1f} % "
              f"| {100 * ae[~real_e].mean():.1f} % "
              f"| {100 * ap.mean():.1f} % ({ap.sum()}/{len(ap)}) "
              f"| {100 * ap[bt].mean():.1f} % |")

    # Reparto de niveles, para ver cuantos UAV quedan en "posible"
    print("\nReparto de niveles por captura de 0,5 s:\n")
    print("| Conjunto | negativo | posible | fuerte |")
    print("|---|---|---|---|")
    for nombre, bl, msk in (("UAV externos", be, real_e),
                            ("sin_uav externos", be, ~real_e),
                            ("propios", bp, np.ones(len(bp), bool))):
        niv = Counter(nivel(c, f) for (_, c, f), m in zip(bl, msk) if m)
        tot = sum(niv.values())
        print(f"| {nombre} | " + " | ".join(
            f"{100 * niv[x] / tot:.1f} %" for x in
            ("negativo", "posible", "fuerte")) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
