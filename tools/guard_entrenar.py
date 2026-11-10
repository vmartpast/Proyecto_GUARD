#!/usr/bin/env python3
"""guard_entrenar: entrena y evalua el clasificador de UAV con validacion
cruzada por captura y genera el informe con las matrices de confusion.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (accuracy_score, confusion_matrix,
                                 f1_score)
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    import joblib
except ImportError:
    print("falta scikit-learn: pip install scikit-learn", file=sys.stderr)
    sys.exit(1)

SEMILLA = 7
NEGATIVAS = {"sin_uav", "ambiente", "wifi_inactivo", "wifi_trafico",
             "bluetooth_busqueda", "bluetooth_audio", "mixto"}
META = ["etiqueta", "captura", "origen", "receptor", "ventana"]


# Datos

def cargar(csv_path: Path, manifest: Path | None):
    """Carga la tabla de rasgos y la frecuencia de sintonia de cada captura.

    @param  csv_path  rasgos.csv generado por guard_rasgos.py
    @param  manifest  manifest.jsonl del dataset, o None
    @return tupla (X, nombres, etiqueta, captura, f_centro) con la matriz de rasgos, sus nombres, la clase y la captura de cada ventana y la frecuencia de sintonia en MHz
    """
    import csv
    filas = list(csv.DictReader(open(csv_path)))
    rasgos = [c for c in filas[0] if c not in META]
    X = np.array([[float(f[c]) for c in rasgos] for f in filas])
    etiqueta = np.array([f["etiqueta"] for f in filas])
    captura = np.array([f["captura"] for f in filas])

    f_centro = np.full(len(filas), np.nan)
    if manifest and manifest.is_file():
        fc = {}
        for l in open(manifest):
            if l.strip():
                r = json.loads(l)
                fc[Path(r["fichero"]).stem] = r["f_centro_hz"] / 1e6
        f_centro = np.array([fc.get(c, np.nan) for c in captura])
    return X, rasgos, etiqueta, captura, f_centro


def pliegues_por_captura(etiqueta, captura, k=2):
    """Reparte las grabaciones enteras en k grupos para la validacion
    cruzada.

    @param  etiqueta  clase de cada ventana
    @param  captura   grabacion de cada ventana
    @param  k         numero de grupos (pliegues)
    @return tupla (pliegue, por_clase): grupo asignado a cada ventana y diccionario clase -> lista de sus capturas
    """
    por_clase = defaultdict(list)
    for c, e in sorted(set(zip(captura, etiqueta))):
        por_clase[e].append(c)
    pliegue_de = {}
    for e, caps in por_clase.items():
        for i, c in enumerate(sorted(caps)):
            pliegue_de[c] = i % k
    return np.array([pliegue_de[c] for c in captura]), por_clase


# Modelos

def bosque():
    """Crea un bosque aleatorio: 300 arboles de decision que votan.

    @return clasificador sin entrenar
    """
    return RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                  class_weight="balanced", n_jobs=-1,
                                  random_state=SEMILLA)


def logistica():
    """Crea una regresion logistica precedida de normalizacion.

    @return pipeline sin entrenar
    """
    return make_pipeline(StandardScaler(),
                         LogisticRegression(max_iter=2000,
                                            class_weight="balanced"))


class Energia:
    """Linea base sin aprendizaje: "UAV" si la ocupacion supera un umbral."""

    def __init__(self, col):
        """Crea la linea base sobre la columna de ocupacion.

        @param  col  indice de la columna "ocupacion" en X
        """
        self.col = col

    def fit(self, X, y):
        """Busca el umbral de ocupacion que mejor separa UAV y no UAV.

        @param  X  matriz de rasgos
        @param  y  etiquetas "uav" / "no_uav"
        @return el propio objeto, ya ajustado
        """
        v = X[:, self.col]
        mejores = (-1, 0)
        for t in np.unique(np.percentile(v, np.linspace(1, 99, 99))):
            acc = ((v > t) == (y == "uav")).mean()
            mejores = max(mejores, (acc, t))
        self.t = mejores[1]
        return self

    def predict(self, X):
        """Aplica el umbral aprendido.

        @param  X  matriz de rasgos
        @return array con "uav" o "no_uav" por ventana
        """
        return np.where(X[:, self.col] > self.t, "uav", "no_uav")


def evaluar(nombre, fabrica, X, y, pliegue, clases):
    """Evalua un modelo con validacion cruzada por captura.

    @param  nombre   nombre del modelo para el informe
    @param  fabrica  funcion sin argumentos que crea un modelo nuevo
    @param  X        matriz de rasgos
    @param  y        etiquetas reales
    @param  pliegue  grupo de cada ventana (de pliegues_por_captura)
    @param  clases   orden de las clases en la matriz de confusion
    @return diccionario con modelo, exactitud, f1_macro, confusion, clases
    """
    pred = np.empty_like(y)
    for k in np.unique(pliegue):
        tr, te = pliegue != k, pliegue == k
        if len(set(y[tr])) < 2:
            continue
        m = fabrica().fit(X[tr], y[tr])
        pred[te] = m.predict(X[te])
    return {
        "modelo": nombre,
        "exactitud": float(accuracy_score(y, pred)),
        "f1_macro": float(f1_score(y, pred, average="macro",
                                   labels=clases, zero_division=0)),
        "confusion": confusion_matrix(y, pred, labels=clases).tolist(),
        "clases": clases,
    }


def por_ventanas(fabrica, X, y, k=2):
    """Control: validacion ingenua repartiendo ventanas al azar.

    @param  fabrica  funcion que crea un modelo nuevo
    @param  X        matriz de rasgos
    @param  y        etiquetas reales
    @param  k        numero de grupos
    @return exactitud obtenida
    """
    rng = np.random.default_rng(SEMILLA)
    pl = rng.integers(0, k, size=len(y))
    pred = np.empty_like(y)
    for i in range(k):
        m = fabrica().fit(X[pl != i], y[pl != i])
        pred[pl == i] = m.predict(X[pl == i])
    return float(accuracy_score(y, pred))


# Informe

def dibujar(res, ruta):
    """Dibuja la matriz de confusion y la guarda como imagen PNG.

    @param  res   resultado de evaluar()
    @param  ruta  fichero .png de salida
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    cm = np.array(res["confusion"], dtype=float)
    cmn = cm / cm.sum(axis=1, keepdims=True).clip(1)
    fig, ax = plt.subplots(figsize=(1.2 + 0.9 * len(res["clases"]),) * 2)
    ax.imshow(cmn, cmap="Blues", vmin=0, vmax=1)
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, f"{cmn[i, j]:.2f}\n({int(cm[i, j])})",
                    ha="center", va="center", fontsize=8,
                    color="white" if cmn[i, j] > 0.5 else "black")
    ax.set_xticks(range(len(res["clases"])), res["clases"], rotation=45,
                  ha="right")
    ax.set_yticks(range(len(res["clases"])), res["clases"])
    ax.set_xlabel("predicho")
    ax.set_ylabel("real")
    ax.set_title(f"{res['modelo']} · exactitud {res['exactitud']:.3f}")
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)


def main() -> int:
    """Entrena, evalua y genera el informe completo.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Entrena el clasificador de UAV")
    p.add_argument("rasgos", type=Path)
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--salida", type=Path, default=None)
    a = p.parse_args()
    salida = a.salida or a.rasgos.parent / "resultados"
    salida.mkdir(parents=True, exist_ok=True)

    X, nombres, etiqueta, captura, f_centro = cargar(
        a.rasgos, a.manifest or a.rasgos.parent / "manifest.jsonl")
    # Tantos pliegues como capturas tenga la clase mas pobre (maximo 5):
    # en cada ronda queda fuera una captura de cada clase.
    n_min = min(len(set(captura[etiqueta == e])) for e in set(etiqueta))
    n_pliegues = max(2, min(5, n_min))
    pliegue, por_clase = pliegues_por_captura(etiqueta, captura, n_pliegues)

    binaria = np.where(np.isin(etiqueta, list(NEGATIVAS)), "no_uav", "uav")
    clases_m = sorted(set(etiqueta))
    informe = {
        "ventanas": int(len(etiqueta)),
        "pliegues": n_pliegues,
        "capturas_por_clase": {e: sorted(c) for e, c in por_clase.items()},
        "ventanas_por_clase": {e: int((etiqueta == e).sum())
                               for e in clases_m},
        "rasgos": nombres,
        "binaria": [], "multiclase": [], "controles": {},
    }

    col_ocup = nombres.index("ocupacion")
    bin_cl = ["no_uav", "uav"]
    informe["binaria"].append(
        evaluar("energia (linea base)", lambda: Energia(col_ocup),
                X, binaria, pliegue, bin_cl))
    informe["binaria"].append(
        evaluar("logistica", logistica, X, binaria, pliegue, bin_cl))
    informe["binaria"].append(
        evaluar("bosque", bosque, X, binaria, pliegue, bin_cl))

    # Binaria derivada del multiclase: alerta si predice cualquier UAV.
    # Juntar los cuatro drones en una sola clase obliga al modelo a
    # dibujar una frontera alrededor de cosas muy distintas (video ancho
    # del Mini 3, rafagas estrechas del Hunter); separarlos y agrupar
    # despues suele generalizar mejor.
    pred_m = np.empty_like(etiqueta)
    for k in np.unique(pliegue):
        tr, te = pliegue != k, pliegue == k
        pred_m[te] = bosque().fit(X[tr], etiqueta[tr]).predict(X[te])
    pred_b = np.where(np.isin(pred_m, list(NEGATIVAS)), "no_uav", "uav")
    informe["binaria"].append({
        "modelo": "bosque multiclase agrupado",
        "exactitud": float(accuracy_score(binaria, pred_b)),
        "f1_macro": float(f1_score(binaria, pred_b, average="macro",
                                   labels=bin_cl, zero_division=0)),
        "confusion": confusion_matrix(binaria, pred_b,
                                      labels=bin_cl).tolist(),
        "clases": bin_cl,
    })
    # Exactitud por captura: muestra que capturas arrastran el resultado
    informe["por_captura"] = {
        c: round(float((pred_m[captura == c] == etiqueta[captura == c])
                       .mean()), 3) for c in sorted(set(captura))}

    informe["multiclase"].append(
        evaluar("logistica", logistica, X, etiqueta, pliegue, clases_m))
    informe["multiclase"].append(
        evaluar("bosque", bosque, X, etiqueta, pliegue, clases_m))

    # Controles
    informe["controles"]["bosque_particion_por_ventanas"] = por_ventanas(
        bosque, X, etiqueta)
    # Sin rasgos de potencia: dependen de la ganancia del receptor y de la
    # distancia al dron, justo lo que cambia al pasar del USRP al HackRF.
    potencia = [i for i, n in enumerate(nombres)
                if n in ("pico_sobre_suelo_db", "media_activa_db")]
    sin_pot = [i for i in range(len(nombres)) if i not in potencia]
    r = evaluar("bosque sin potencia", bosque, X[:, sin_pot], etiqueta,
                pliegue, clases_m)
    informe["controles"]["bosque_sin_rasgos_de_potencia"] = r["exactitud"]
    if not np.isnan(f_centro).any():
        Xf = np.column_stack([X, f_centro])
        r = evaluar("bosque + frecuencia", bosque, Xf, etiqueta, pliegue,
                    clases_m)
        informe["controles"]["bosque_con_frecuencia"] = r["exactitud"]
        r = evaluar("solo frecuencia", bosque, f_centro[:, None], etiqueta,
                    pliegue, clases_m)
        informe["controles"]["solo_frecuencia"] = r["exactitud"]

    # Modelo final con todo, importancia de rasgos y latencia
    final = bosque().fit(X, etiqueta)
    imp = sorted(zip(nombres, final.feature_importances_),
                 key=lambda t: -t[1])
    informe["importancia"] = [(n, round(float(v), 4)) for n, v in imp]
    final.set_params(n_jobs=1)     # en la Pi se predice de una en una
    t0 = time.perf_counter()
    for _ in range(200):
        final.predict(X[:1])
    informe["latencia_prediccion_ms"] = (time.perf_counter() - t0) / 200 * 1e3
    joblib.dump({"modelo": final, "rasgos": nombres}, salida / "modelo.joblib")

    for tarea in ("binaria", "multiclase"):
        for r in informe[tarea]:
            dibujar(r, salida / f"confusion_{tarea}_"
                    f"{'_'.join(r['modelo'].split()[:2])}.png")

    with open(salida / "informe.json", "w") as f:
        json.dump(informe, f, indent=2, ensure_ascii=False)

    # Resumen legible
    lin = ["# Resultados del clasificador", "",
           f"{informe['ventanas']} ventanas de "
           f"{sum(len(c) for c in por_clase.values())} capturas. "
           f"Evaluacion por captura ({n_pliegues} pliegues).", "",
           "| Tarea | Modelo | Exactitud | F1 macro |", "|---|---|---|---|"]
    for tarea in ("binaria", "multiclase"):
        for r in informe[tarea]:
            lin.append(f"| {tarea} | {r['modelo']} | {r['exactitud']:.3f} "
                       f"| {r['f1_macro']:.3f} |")
    lin += ["", "## Acierto por captura (bosque multiclase)", ""]
    lin += [f"- {c}: {v:.3f}" for c, v in informe["por_captura"].items()]
    lin += ["", "## Controles", ""]
    for k, v in informe["controles"].items():
        lin.append(f"- {k}: {v:.3f}")
    lin += ["", "## Rasgos mas importantes", ""]
    lin += [f"{i+1}. {n} ({v})" for i, (n, v) in
            enumerate(informe["importancia"][:8])]
    lin += ["", f"Latencia de prediccion por ventana: "
            f"{informe['latencia_prediccion_ms']:.2f} ms"]
    (salida / "informe.md").write_text("\n".join(lin) + "\n")
    print("\n".join(lin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
