#!/usr/bin/env python3
"""guard_probar: entrena el clasificador con un conjunto de datos y lo prueba
con otro de distinto origen.
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
except ImportError:
    print("falta scikit-learn: pip install scikit-learn", file=sys.stderr)
    sys.exit(1)

META = ["etiqueta", "captura", "origen", "receptor", "ventana"]
POTENCIA = ("pico_sobre_suelo_db", "media_activa_db")
NEGATIVAS = {"sin_uav", "ambiente", "wifi_inactivo", "wifi_trafico",
             "bluetooth_busqueda", "bluetooth_audio", "mixto"}
EXCLUIR = {"sin_etiquetar"}


def leer(ruta: Path, columnas=None):
    """Lee un rasgos.csv.

    @param  ruta      fichero CSV de guard_rasgos.py
    @param  columnas  rasgos a usar (None = todos los del fichero)
    @return tupla (X, etiqueta, captura, columnas)
    """
    filas = [f for f in csv.DictReader(open(ruta))
             if f["etiqueta"] not in EXCLUIR]
    if columnas is None:
        columnas = [c for c in filas[0] if c not in META]
    X = np.array([[float(f[c]) for c in columnas] for f in filas])
    return (X, np.array([f["etiqueta"] for f in filas]),
            np.array([f["captura"] for f in filas]), columnas)


def main() -> int:
    """Entrena con el primer conjunto, clasifica el segundo y resume.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Prueba con datos de otro origen")
    p.add_argument("entrenar", type=Path)
    p.add_argument("probar", type=Path)
    p.add_argument("--sin-potencia", action="store_true",
                   help="excluye los rasgos de potencia (dependen del receptor)")
    p.add_argument("--salida", type=Path, default=None)
    a = p.parse_args()

    Xa, ya, _, cols = leer(a.entrenar)
    if a.sin_potencia:
        cols = [c for c in cols if c not in POTENCIA]
        Xa, ya, _, cols = leer(a.entrenar, cols)
    Xb, yb, cb, _ = leer(a.probar, cols)

    modelo = RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                    class_weight="balanced", n_jobs=-1,
                                    random_state=7).fit(Xa, ya)
    pred = modelo.predict(Xb)
    alarma = ~np.isin(pred, list(NEGATIVAS))

    clases = sorted(set(ya))
    titulo = ("sin rasgos de potencia" if a.sin_potencia
              else "con todos los rasgos")
    lin = [f"# Prueba cruzada ({titulo})", "",
           f"Entrenado con {a.entrenar} ({len(ya)} ventanas), "
           f"probado con {a.probar} ({len(yb)} ventanas).", "",
           "| Clase real | Ventanas | " + " | ".join(clases)
           + " | Alarma (ventana) | Alarma (captura) |",
           "|---" * (len(clases) + 4) + "|"]
    for e in sorted(set(yb)):
        m = yb == e
        rep = Counter(pred[m])
        # Decision por captura: mayoria de sus ventanas
        por_cap = defaultdict(list)
        for c, al in zip(cb[m], alarma[m]):
            por_cap[c].append(al)
        alarma_cap = np.mean([np.mean(v) > 0.5 for v in por_cap.values()])
        lin.append(f"| {e} | {m.sum()} | "
                   + " | ".join(str(rep.get(c, 0)) for c in clases)
                   + f" | {100 * alarma[m].mean():.1f} % "
                   f"| {100 * alarma_cap:.1f} % |")
    neg = np.isin(yb, list(NEGATIVAS))
    if neg.any():
        lin += ["", f"Tasa global de falsas alarmas por ventana: "
                f"{100 * alarma[neg].mean():.1f} %"]
    texto = "\n".join(lin) + "\n"
    print(texto)
    if a.salida:
        a.salida.write_text(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
