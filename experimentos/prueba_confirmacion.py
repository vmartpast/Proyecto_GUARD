"""prueba_confirmacion: prueba sin la Pi la confirmacion de capturas dudosas
entre el clasificador, el detector y el panel.
"""
import json, sys, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace

T = Path(tempfile.mkdtemp())
sys.path.insert(0, sys.argv[1])            # carpeta con los .py nuevos
import guard_clasificador as gcl
import guard_rf as grf

gcl.PETICION_FILE = T / "pet.json"; gcl.RESULTADO_FILE = T / "res.json"
gcl.HEALTH_FILE = T / "h"; gcl.CONFIRMAR_FILE = T / "conf.json"
grf.CLASIF_CONFIRMAR = T / "conf.json"

# --- 1. Logica del confirmador con resultados sinteticos
print("== 1. Confirmador")
c = gcl.Confirmador()
def r(nivel, f, conf=0.5, clase="uav_mavic_pro", i=0):
    """Crea un resultado sintetico del clasificador.

    @param  nivel  nivel de la captura ('negativo', 'posible' o 'fuerte')
    @param  f      frecuencia central en MHz
    @param  conf   confianza de la clase
    @param  clase  clase decidida
    @param  i      identificador de la captura
    @return diccionario con el resultado
    """
    return {"nivel": nivel, "f_centro_mhz": f, "confianza": conf,
            "clase": clase if nivel != "negativo" else "sin_uav", "id": i,
            "alarma": nivel == "fuerte"}
casos = [("posible sola", [r("posible", 2423)]),
         ("posible + posible", [r("posible", 2423), r("posible", 2425, i=1)]),
         ("posible + negativo", [r("posible", 2423), r("negativo", 2423, 1.0)]),
         ("posible + posible otra zona", [r("posible", 2423), r("posible", 2470)]),
         ("fuerte", [r("fuerte", 2440, 0.8)])]
for nombre, seq in casos:
    c = gcl.Confirmador()
    for x in seq:
        c.decidir(x)
    ult = seq[-1]
    print(f"  {nombre:30s} -> alarma={ult['alarma']!s:5} confirmacion={ult['confirmacion']}"
          f"  peticion_detector={'si' if gcl.CONFIRMAR_FILE.exists() else 'no'}")
    gcl.CONFIRMAR_FILE.unlink(missing_ok=True)

# --- 2. El detector atiende la peticion antes que nada
print("== 2. Detector")
grf._clasif_objetivo = {"f_centro_mhz": 2470}
gcl.pedir_confirmacion(2423.0, 1)
print("  elegir_centro con confirmacion pendiente:", grf.elegir_centro())
grf.CLASIF_CONFIRMAR.unlink()
print("  sin confirmacion:", grf.elegir_centro())
gcl.pedir_confirmacion(2423.0, 1)
import os; os.utime(grf.CLASIF_CONFIRMAR, (time.time() - 40, time.time() - 40))
print("  confirmacion caducada (40 s):", grf.leer_confirmacion(), "fichero sigue:", grf.CLASIF_CONFIRMAR.exists())

# --- 3. Servicio real con capturas reales de 0,5 s
if len(sys.argv) > 3:
    print("== 3. Servicio con capturas reales")
    modelo = gcl.Modelo(Path(sys.argv[2]))
    eventos = []
    gcl.emit = eventos.append
    th = threading.Thread(target=gcl.bucle, args=(modelo,), daemon=True); th.start()
    for k, src in enumerate(sys.argv[3:]):
        clip = T / f"clip{k}.cs8"
        with open(src, "rb") as fi:
            clip.write_bytes(fi.read(20_000_000))
        clip.with_suffix(".json").write_text(json.dumps({"muestreo_sps": 20e6}))
        gcl.escribir_atomico(gcl.PETICION_FILE, {"id": k, "fichero": str(clip),
            "f_centro_mhz": 2423.0, "motivo": "prueba"})
        while gcl.PETICION_FILE.exists() or clip.exists():
            time.sleep(0.1)
        time.sleep(0.3)
        res = json.loads(gcl.RESULTADO_FILE.read_text())
        print(f"  {Path(src).name[:40]:40s} {res['clase']:14s} {res['confianza']:.0%} "
              f"nivel={res['nivel']:8s} confirmacion={res['confirmacion']} alarma={res['alarma']} "
              f"{res['latencia_ms']['total']:.0f} ms")
    gcl._running = False
    print("  eventos:", [(e["type"], e.get("confirmacion")) for e in eventos])

# --- 4. Panel
print("== 4. Panel")
import guard_panel as gp
col = gp.Color(True) if "Color" in dir(gp) else None
from collections import deque
gp.CLASIF_FILE = T / "panel_res.json"
gp.leer_snapshot = lambda: (None, None, 0.0)
args = SimpleNamespace(autonomo=False, sin_receptor=True, f_min=2400, f_max=2500)
class S:
    """Sustituto del lector de salud del panel que no devuelve datos."""
    def leer(self):
        """Devuelve una salud vacia.

        @return diccionario vacio
        """
        return {}
datos = {"analisis": {}, "emisiones": [], "lista": [], "historial": deque(maxlen=24),
         "historial_bins": deque(maxlen=24), "registro": deque(maxlen=60),
         "salud": {}, "margenes": [], "fuente": "detector", "antiguedad": 0.0,
         "ultima_deteccion": 0.0, "alarma_uav": None}
col = gp.Color(False)
def publicar(i, **kw):
    """Publica un resultado del clasificador y muestra como lo presenta
    el panel.

    @param  i     numero de la prueba
    @param  **kw  campos del resultado que se sustituyen
    """
    base = {"ts": f"t{i}", "clase": "sin_uav", "confianza": 1.0, "nivel": "negativo",
            "alarma": False, "confirmacion": None, "f_centro_mhz": 2423.0,
            "latencia_ms": {"total": 2200}}
    base.update(kw)
    gp.CLASIF_FILE.write_text(json.dumps(base))
    gp.actualizar_datos(args, datos, S(), set(), None)
    c = datos["clasif"]
    estado = ("UAV" if gp.uav_vigente(c) else "POSIBLE" if gp.uav_posible(c) else "-")
    print(f"  estado={estado:8s} cabecera: {gp.texto_clasificacion(col, c)}")
seq = [dict(clase="uav_mavic_pro", confianza=0.5, nivel="posible", confirmacion="pendiente"),
       dict(confirmacion="descartada", descarta_a={"clase": "uav_mavic_pro", "confianza": 0.5, "id": 0}),
       dict(clase="uav_mavic_pro", confianza=0.6, nivel="posible", confirmacion="pendiente"),
       dict(clase="uav_mavic_pro", confianza=0.6, nivel="posible", confirmacion="confirmada", alarma=True),
       dict()]
for i, kw in enumerate(seq):
    publicar(i, **kw)
print("  alarma enclavada:", gp.texto_alarma(col, datos["alarma_uav"]).strip())
print("  registro:")
for h, t, _ in datos["registro"]:
    print("    ", t)
print("  perfil:", repr("".join(gp.bloque_espectro(col, args, [0, 5, 10, 15, 25, 0], False, [0, 9, 9, 21, 25, 3])[1:3])))
