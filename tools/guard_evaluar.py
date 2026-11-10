#!/usr/bin/env python3
"""guard_evaluar: evaluacion final del clasificador. Mide la deteccion de
UAV con el conjunto externo y las falsas alarmas con las sesiones propias.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import confusion_matrix
except ImportError:
    print("falta scikit-learn: pip install scikit-learn", file=sys.stderr)
    sys.exit(1)

META = ["etiqueta", "captura", "origen", "receptor", "ventana"]
POTENCIA = ("pico_sobre_suelo_db", "media_activa_db")
NEGATIVA = "sin_uav"
EXCLUIR = {"sin_etiquetar"}


def leer(rasgos: Path, manifest: Path):
    """Lee un rasgos.csv y le asocia la sesion de cada captura.

    @param  rasgos    rasgos.csv de guard_rasgos.py
    @param  manifest  manifest.jsonl del mismo dataset
    @return tupla (X, etiqueta, captura, sesion, nombres_de_rasgos)
    """
    sesion_de = {}
    for l in open(manifest):
        if l.strip():
            r = json.loads(l)
            m = re.search(r"sesion\s*(\d+)", r.get("nota", ""))
            sesion_de[Path(r["fichero"]).stem] = (
                f"s{m.group(1)}" if m else Path(r["fichero"]).stem)
    filas = [f for f in csv.DictReader(open(rasgos))
             if f["etiqueta"] not in EXCLUIR]
    nombres = [c for c in filas[0] if c not in META]
    X = np.array([[float(f[c]) for c in nombres] for f in filas])
    etiqueta = np.array([f["etiqueta"] for f in filas])
    captura = np.array([f["captura"] for f in filas])
    sesion = np.array([sesion_de.get(c, c) for c in captura])
    return X, etiqueta, captura, sesion, nombres


def pliegues_externos(etiqueta, captura, k):
    """Asigna cada captura externa a uno de k pliegues, alternando dentro de
    cada clase, de modo que cada pliegue tenga una captura por clase.

    @param  etiqueta  clase de cada ventana
    @param  captura   grabacion de cada ventana
    @param  k         numero de pliegues
    @return array con el pliegue de cada ventana
    """
    por_clase = defaultdict(list)
    for c, e in sorted(set(zip(captura, etiqueta))):
        por_clase[e].append(c)
    p = {c: i % k for caps in por_clase.values()
         for i, c in enumerate(sorted(caps))}
    return np.array([p[c] for c in captura])


def por_segundo(pred, captura):
    """Decision por captura: la clase mas votada entre sus ventanas.

    @param  pred     prediccion de cada ventana
    @param  captura  captura de cada ventana
    @return diccionario captura -> clase decidida
    """
    votos = defaultdict(Counter)
    for p, c in zip(pred, captura):
        votos[c][p] += 1
    return {c: v.most_common(1)[0][0] for c, v in votos.items()}


def decisiones_por_bloque(pred, real, captura, n):
    """Decision por mayoria sobre bloques de n ventanas consecutivas.

    @param  pred     prediccion de cada ventana
    @param  real     clase real de cada ventana
    @param  captura  captura de cada ventana
    @param  n        ventanas por bloque
    @return tupla (prediccion, clase real) por bloque, como arrays
    """
    p_b, r_b = [], []
    inicio = 0
    while inicio < len(pred):
        fin = inicio
        while fin < len(pred) and captura[fin] == captura[inicio]:
            fin += 1
        for a in range(inicio, fin - n + 1, n):
            p_b.append(Counter(pred[a:a + n]).most_common(1)[0][0])
            r_b.append(real[a])
        inicio = fin
    return np.array(p_b, dtype=object), np.array(r_b, dtype=object)


def bosque():
    """Crea el bosque aleatorio con la misma configuracion que
    guard_entrenar.py.

    @return bosque aleatorio sin entrenar, con la misma configuracion que guard_entrenar.py
    """
    return RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                  class_weight="balanced", n_jobs=-1,
                                  random_state=7)


def main() -> int:
    """Ejecuta las rondas de evaluacion y escribe el informe.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Evaluacion final")
    p.add_argument("ext_rasgos", type=Path)
    p.add_argument("ext_manifest", type=Path)
    p.add_argument("pro_rasgos", type=Path)
    p.add_argument("pro_manifest", type=Path)
    p.add_argument("--con-potencia", action="store_true")
    p.add_argument("--salida", type=Path, default=None)
    a = p.parse_args()

    Xe, ye, ce, _, nombres = leer(a.ext_rasgos, a.ext_manifest)
    Xp, yp_orig, cp, sp, nombres_p = leer(a.pro_rasgos, a.pro_manifest)
    assert nombres == nombres_p, "los dos CSV tienen rasgos distintos"
    if not a.con_potencia:
        usar = [i for i, n in enumerate(nombres) if n not in POTENCIA]
        Xe, Xp = Xe[:, usar], Xp[:, usar]
    yp = np.full(len(yp_orig), NEGATIVA)        # todo lo propio es "sin UAV"

    n_min = min(len(set(ce[ye == e])) for e in set(ye))
    k = max(2, min(5, n_min))
    pl = pliegues_externos(ye, ce, k)
    sesiones = sorted(set(sp))

    # Ronda A, deteccion de UAV: por cada pliegue externo, entrenar con
    # los demas pliegues y con todo lo propio (que son solo negativos y no
    # se parecen a ninguna captura externa de prueba).
    pred_e = np.empty(len(ye), dtype=object)
    for i in range(k):
        tr, te = pl != i, pl == i
        m = bosque().fit(np.vstack([Xe[tr], Xp]), np.concatenate([ye[tr], yp]))
        pred_e[te] = m.predict(Xe[te])
        print(f"  pliegue externo {i + 1}/{k}", flush=True)

    # Ronda B, falsas alarmas: por cada sesion propia, entrenar con todo
    # lo externo y con las otras sesiones, y evaluar la sesion apartada.
    predp = np.empty(len(yp), dtype=object)
    for s in sesiones:
        tr, te = sp != s, sp == s
        m = bosque().fit(np.vstack([Xe, Xp[tr]]), np.concatenate([ye, yp[tr]]))
        predp[te] = m.predict(Xp[te])
        print(f"  sesion propia {s} evaluada", flush=True)

    clases = sorted(set(ye))
    uav = [c for c in clases if c != NEGATIVA]
    lin = ["# Evaluacion final", "",
           f"Rasgos: {'todos' if a.con_potencia else 'sin potencia'}. "
           f"Externo: {len(ye)} ventanas, {len(set(ce))} capturas, {k} "
           f"pliegues por captura. Propio: {len(yp)} ventanas, "
           f"{len(set(cp))} capturas, sesiones {', '.join(sesiones)} "
           "(entrenar con una, evaluar con la otra).", ""]

    # 1. Deteccion de UAV en el conjunto externo
    es_uav = ye != NEGATIVA
    alarma_e = pred_e != NEGATIVA
    lin += ["## 1. Deteccion de UAV (conjunto externo)", "",
            "| Clase | Ventanas | Detectado como UAV | Clase exacta |",
            "|---|---|---|---|"]
    for c in clases:
        m = ye == c
        lin.append(f"| {c} | {m.sum()} | {100 * alarma_e[m].mean():.1f} % "
                   f"| {100 * (pred_e[m] == c).mean():.1f} % |")
    tp = (alarma_e & es_uav).sum()
    fn = (~alarma_e & es_uav).sum()
    fp = (alarma_e & ~es_uav).sum()
    sens = tp / max(tp + fn, 1)
    prec = tp / max(tp + fp, 1)
    lin += ["", f"Sensibilidad (UAV detectados): {100 * sens:.1f} %. "
            f"Precision: {100 * prec:.1f} %. "
            f"Exactitud multiclase: {100 * (pred_e == ye).mean():.1f} %.",
            ""]
    cm = confusion_matrix(ye, pred_e.astype(str), labels=clases)
    lin += ["Matriz de confusion (filas: real; columnas: predicho):", "",
            "| | " + " | ".join(clases) + " |",
            "|---" * (len(clases) + 1) + "|"]
    for c, fila in zip(clases, cm):
        lin.append(f"| {c} | " + " | ".join(str(v) for v in fila) + " |")

    # 2. Falsas alarmas en el entorno propio
    alarma_p = predp != NEGATIVA
    dec = por_segundo(predp, cp)
    lin += ["", "## 2. Falsas alarmas en el entorno propio (sesion no vista)",
            "", "| Emisor | Sesion | Ventanas | Falsa alarma (ventana) "
            "| Falsa alarma (por segundo) | Confundido sobre todo con |",
            "|---|---|---|---|---|---|"]
    for e in sorted(set(yp_orig)):
        for s in sesiones:
            m = (yp_orig == e) & (sp == s)
            if not m.any():
                continue
            caps = sorted(set(cp[m]))
            fa_seg = np.mean([dec[c] != NEGATIVA for c in caps])
            conf = Counter(predp[m & alarma_p]).most_common(1)
            lin.append(f"| {e} | {s} | {m.sum()} "
                       f"| {100 * alarma_p[m].mean():.1f} % "
                       f"| {100 * fa_seg:.1f} % "
                       f"| {conf[0][0] if conf else '-'} |")
    caps_p = sorted(set(cp))
    lin += ["", f"Tasa global de falsas alarmas: "
            f"{100 * alarma_p.mean():.1f} % por ventana, "
            f"{100 * np.mean([dec[c] != NEGATIVA for c in caps_p]):.1f} % "
            "por segundo.", ""]

    # 3. Duracion de la captura: decidir con n ventanas de 50 ms
    lin += ["## 3. Decision segun la duracion de la captura", "",
            "| Ventanas (duracion) | Sensibilidad UAV | Precision | "
            "Exactitud multiclase | Falsas alarmas propias |",
            "|---|---|---|---|---|"]
    for n in (1, 5, 10, 20):
        pe, re_ = decisiones_por_bloque(pred_e, ye, ce, n)
        pp, _ = decisiones_por_bloque(predp, yp, cp, n)
        al_e, uav_b = pe != NEGATIVA, re_ != NEGATIVA
        tp_b = (al_e & uav_b).sum()
        sens_b = tp_b / max(uav_b.sum(), 1)
        prec_b = tp_b / max(al_e.sum(), 1)
        lin.append(f"| {n} ({50 * n} ms) | {100 * sens_b:.1f} % "
                   f"| {100 * prec_b:.1f} % | {100 * (pe == re_).mean():.1f} % "
                   f"| {100 * (pp != NEGATIVA).mean():.1f} % "
                   f"({len(pp)} decisiones) |")
    lin.append("")

    texto = "\n".join(lin) + "\n"
    print(texto)
    if a.salida:
        a.salida.write_text(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
