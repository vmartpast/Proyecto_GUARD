#!/usr/bin/env python3
"""ejecutar_pruebas: ejecuta la bateria de pruebas de GUARD (unitarias, de
integracion y de sistema) y guarda el informe en tests/resultados/.
"""

import argparse
import platform
import socket
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from comun import (PASS, FAIL, SKIP, MANUAL, BASE, RESULTADOS, Resultado,
                   ejecutar, es_root)
import unitarias
import integracion
import sistema

NIVELES = [("Pruebas unitarias", unitarias.PRUEBAS),
           ("Pruebas de integración", integracion.PRUEBAS),
           ("Pruebas de sistema", sistema.PRUEBAS)]


def cabecera() -> list:
    """Genera la cabecera del informe: fecha, equipo, version de Python
    y commit del repositorio.

    @return lista de lineas de texto
    """
    commit = ejecutar(["git", "-C", str(BASE), "rev-parse", "--short", "HEAD"]).stdout.strip()
    return [
        "=" * 78,
        f"  PRUEBAS GUARD   {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"  equipo {socket.gethostname()} · {platform.system()} {platform.release()}"
        f" · Python {platform.python_version()} · commit {commit or 'desconocido'}",
        "=" * 78,
    ]


def main() -> int:
    """Ejecuta las pruebas seleccionadas, muestra el resultado y escribe
    el informe y el historico.

    @return 0 si no fallo ninguna prueba, 1 si fallo alguna
    """
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--solo-unitarias", action="store_true")
    p.add_argument("--sin-usb", action="store_true",
                   help="omite S4 (desconexión lógica del HackRF)")
    p.add_argument("--solo", nargs="+", metavar="ID", help="identificadores a ejecutar")
    args = p.parse_args()

    lineas = cabecera()
    print("\n".join(lineas))
    cuenta = {PASS: 0, FAIL: 0, SKIP: 0, MANUAL: 0}

    for titulo, pruebas in NIVELES:
        bloque = [f"\n{titulo}", "-" * 78]
        print("\n".join(bloque), flush=True)
        lineas += bloque
        for ident, nombre, funcion, requiere_root in pruebas:
            if args.solo and ident not in args.solo:
                continue
            if args.solo_unitarias and ident[0] != "U":
                continue
            t0 = time.monotonic()
            if args.sin_usb and ident == "S4":
                r = Resultado(SKIP, "omitida con --sin-usb")
            elif requiere_root and not es_root():
                r = Resultado(SKIP, "requiere sudo")
            else:
                try:
                    r = funcion()
                except Exception as exc:          # una prueba rota no para el resto
                    r = Resultado(FAIL, f"excepción: {exc!r}")
                    traceback.print_exc()
            dur = time.monotonic() - t0
            cuenta[r.estado] += 1
            linea = f"[{r.estado:<6}] {ident:<3} {nombre}  ({dur:.1f} s)"
            print(linea)
            print(f"           {r.detalle}", flush=True)
            lineas += [linea, f"           {r.detalle}"]

    resumen = (f"\nResumen: {cuenta[PASS]} PASS · {cuenta[FAIL]} FAIL · "
               f"{cuenta[SKIP]} SKIP · {cuenta[MANUAL]} MANUAL")
    print(resumen)
    lineas.append(resumen)

    RESULTADOS.mkdir(parents=True, exist_ok=True)
    informe = RESULTADOS / f"pruebas_{datetime.now():%Y%m%d_%H%M%S}.txt"
    texto = "\n".join(lineas) + "\n"
    informe.write_text(texto)
    with open(RESULTADOS / "historico.txt", "a") as f:
        f.write(texto + "\n")
    print(f"Informe: {informe}")
    return 1 if cuenta[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
