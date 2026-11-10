#!/usr/bin/env python3
"""guard_modelo: entrena el clasificador final con todos los datos y lo
exporta a un fichero .npz que la Pi evalua solo con NumPy.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import sklearn
    from sklearn.ensemble import RandomForestClassifier
except ImportError:
    print("falta scikit-learn: pip install scikit-learn", file=sys.stderr)
    sys.exit(1)

META = ["etiqueta", "captura", "origen", "receptor", "ventana"]
POTENCIA = ("pico_sobre_suelo_db", "media_activa_db")
NEGATIVAS = {"sin_uav", "ambiente", "wifi_inactivo", "wifi_trafico",
             "bluetooth_busqueda", "bluetooth_audio", "mixto"}
EXCLUIR = {"sin_etiquetar"}


def leer(rasgos: Path, manifest: Path, solo_sesion: str | None):
    """Lee un rasgos.csv, opcionalmente filtrando por sesion propia.

    @param  rasgos       rasgos.csv de guard_rasgos.py
    @param  manifest     manifest.jsonl del mismo dataset
    @param  solo_sesion  si se da (p. ej. "2"), solo las capturas cuya nota diga "sesion N"; las capturas sin sesion se aceptan
    @return tupla (X, y, nombres_de_rasgos, ganancias)
    """
    info = {}
    for l in open(manifest):
        if l.strip():
            r = json.loads(l)
            m = re.search(r"sesion\s*(\d+)", r.get("nota", ""))
            info[Path(r["fichero"]).stem] = (
                m.group(1) if m else None,
                (r.get("ganancia_lna_db"), r.get("ganancia_vga_db")))
    filas = []
    for f in csv.DictReader(open(rasgos)):
        if f["etiqueta"] in EXCLUIR:
            continue
        ses, _ = info.get(f["captura"], (None, None))
        if solo_sesion and ses is not None and ses != solo_sesion:
            continue
        filas.append(f)
    nombres = [c for c in filas[0] if c not in META]
    X = np.array([[float(f[c]) for c in nombres] for f in filas])
    y = np.array(["sin_uav" if f["etiqueta"] in NEGATIVAS else f["etiqueta"]
                  for f in filas])
    gan = sorted({str(info.get(f["captura"], (None, None))[1])
                  for f in filas})
    return X, y, nombres, gan


def exportar(modelo, clases):
    """Convierte los arboles de un bosque de scikit-learn en arrays planos.

    @param  modelo  RandomForestClassifier entrenado
    @param  clases  orden de las clases
    @return diccionario de arrays numpy listo para np.savez
    """
    raiz, rasgo, umbral, izq, der, votos = [], [], [], [], [], []
    base = 0
    for est in modelo.estimators_:
        t = est.tree_
        n = t.node_count
        raiz.append(base)
        rasgo.append(t.feature.astype(np.int16))
        umbral.append(t.threshold.astype(np.float64))
        izq.append(np.where(t.children_left >= 0, t.children_left + base, -1))
        der.append(np.where(t.children_right >= 0,
                            t.children_right + base, -1))
        v = t.value[:, 0, :]
        v = v / v.sum(axis=1, keepdims=True)
        votos.append(v.astype(np.float64))
        base += n
    return {
        "raiz": np.array(raiz, dtype=np.int32),
        "rasgo": np.concatenate(rasgo),
        "umbral": np.concatenate(umbral),
        "izq": np.concatenate(izq).astype(np.int32),
        "der": np.concatenate(der).astype(np.int32),
        "votos": np.concatenate(votos),
        "clases": np.array(clases),
    }


def predecir_exportado(m, X):
    """Prediccion con el modelo exportado, igual que en la Pi.

    @param  m  diccionario devuelto por exportar()
    @param  X  matriz ventanas x rasgos
    @return matriz ventanas x clases con la probabilidad media de los arboles
    """
    # scikit-learn convierte los rasgos a float32 antes de comparar con
    # los umbrales; hay que hacer lo mismo para decidir igual en el limite
    X = np.asarray(X, dtype=np.float32).astype(np.float64)
    prob = np.zeros((len(X), len(m["clases"])))
    for r0 in m["raiz"]:
        nodo = np.full(len(X), r0)
        activo = m["rasgo"][nodo] >= 0
        while activo.any():
            n = nodo[activo]
            ir_izq = X[activo, m["rasgo"][n]] <= m["umbral"][n]
            nodo[activo] = np.where(ir_izq, m["izq"][n], m["der"][n])
            activo = m["rasgo"][nodo] >= 0
        prob += m["votos"][nodo]
    return prob / len(m["raiz"])


def main() -> int:
    """Entrena con todos los datos indicados, exporta y verifica.

    @return codigo de salida para el sistema operativo
    """
    p = argparse.ArgumentParser(description="Modelo final para la Pi")
    p.add_argument("--datos", nargs=2, action="append", required=True,
                   metavar=("RASGOS", "MANIFEST"))
    p.add_argument("--solo-sesion", default=None,
                   help="usar solo esta sesion de los datos propios")
    p.add_argument("--con-potencia", action="store_true")
    p.add_argument("--salida", type=Path, default=Path("modelo_guard.npz"))
    a = p.parse_args()

    partes = [leer(Path(r), Path(m), a.solo_sesion) for r, m in a.datos]
    nombres = partes[0][2]
    assert all(pt[2] == nombres for pt in partes), "rasgos distintos"
    X = np.vstack([pt[0] for pt in partes])
    y = np.concatenate([pt[1] for pt in partes])
    usar = [i for i, n in enumerate(nombres)
            if a.con_potencia or n not in POTENCIA]
    X = X[:, usar]
    nombres_usados = [nombres[i] for i in usar]

    modelo = RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                    class_weight="balanced", n_jobs=-1,
                                    random_state=7).fit(X, y)
    clases = list(modelo.classes_)
    m = exportar(modelo, clases)

    # Verificacion: el exportado debe decidir igual que scikit-learn
    p_sk = modelo.predict_proba(X)
    p_ex = predecir_exportado(m, X)
    dif = np.abs(p_sk - p_ex).max()
    igual = (p_sk.argmax(1) == p_ex.argmax(1)).mean()
    print(f"verificacion: diferencia maxima de probabilidad {dif:.2e}, "
          f"misma clase en el {100 * igual:.2f} % de las ventanas")
    if igual < 1.0:
        print("error: el modelo exportado no coincide", file=sys.stderr)
        return 1

    meta = {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "rasgos": nombres_usados,
        "clases": clases,
        "negativa": "sin_uav",
        "arboles": len(m["raiz"]),
        "nodos": int(len(m["rasgo"])),
        "ventanas_entrenamiento": {c: int((y == c).sum()) for c in clases},
        "ganancias_vistas": sorted({g for pt in partes for g in pt[3]}),
        "solo_sesion_propia": a.solo_sesion,
        "con_potencia": a.con_potencia,
        "sklearn_entrenamiento": sklearn.__version__,
        "ventana_ms": 50,
        "nota": ("Rasgos de guard_rasgos.py (version de rasgos 2). La "
                 "ganancia del receptor en operacion debe ser la misma "
                 "que la de los datos propios de entrenamiento."),
    }
    np.savez_compressed(a.salida, meta=json.dumps(meta, ensure_ascii=False),
                        **m)
    tam = a.salida.stat().st_size
    print(f"modelo -> {a.salida} ({tam / 1024:.0f} KB, {meta['nodos']} "
          f"nodos, {len(nombres_usados)} rasgos)")
    print(json.dumps(meta["ventanas_entrenamiento"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
