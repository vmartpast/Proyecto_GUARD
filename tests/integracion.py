"""integracion: pruebas de integracion de la comunicacion entre componentes
(ESP32, puente, detector, lista blanca, receptor y orden de arranque).
"""

import json
import shutil
import time

from comun import (PASS, FAIL, SKIP, Resultado, importar, ejecutar, activo,
                   propiedad, esperar, leer_snapshot, unidad_detector,
                   LISTA_BLANCA, esperar_detector_listo, UNIDAD_RF, UNIDAD_STUB, UNIDAD_PUENTE,
                   UNIDAD_PANEL, UNIDAD_SILENTE)

PUERTO = "/dev/ttyAMA3"


def i1_tramas_corruptas() -> Resultado:
    """I1: comprueba que el ESP32 descarta las tramas invalidas y responde
    con ERR.

    @return Resultado de la prueba
    """
    import serial
    br = importar("guard_bridge")

    puente_activo = activo(UNIDAD_PUENTE)
    ejecutar(["systemctl", "stop", UNIDAD_PUENTE])
    time.sleep(1)
    respuestas = {}
    try:
        with serial.Serial(PUERTO, 115200, timeout=0.6) as p:
            def enviar(trama: str) -> str:
                """Envia una trama al ESP32 y recoge lo que responde en medio
                segundo.

                @param  trama  trama completa a enviar
                @return respuesta del ESP32, sin espacios al principio ni al final
                """
                p.reset_input_buffer()
                p.write(trama.encode())
                time.sleep(0.5)
                return p.read(300).decode(errors="replace").strip()

            cuerpo = "HB|10|OK|"
            cs = br.checksum(cuerpo)
            malo = "00" if cs != "00" else "01"
            respuestas["válida"] = enviar(f">{cuerpo}*{cs}\n")
            respuestas["checksum incorrecto"] = enviar(f">{cuerpo}*{malo}\n")
            respuestas["faltan campos"] = enviar(f">HB|*{br.checksum('HB|')}\n")
            respuestas["demasiado larga"] = enviar(">" + "X" * 150 + "\n")
    finally:
        if puente_activo:
            ejecutar(["systemctl", "start", UNIDAD_PUENTE])

    esperado = {"válida": None, "checksum incorrecto": "ERR|CS",
                "faltan campos": "ERR|FMT", "demasiado larga": "ERR|LEN"}
    ok = True
    for caso, codigo in esperado.items():
        r = respuestas.get(caso, "")
        if codigo is None:
            ok &= "ERR" not in r
        else:
            ok &= codigo in r
    detalle = "; ".join(f"{c}: {r or '(sin respuesta)'}" for c, r in respuestas.items())
    return Resultado(PASS if ok else FAIL, detalle)


def i2_detector_caido() -> Resultado:
    """I2: comprueba que el puente pasa a ERROR al parar el detector y a
    OK cuando vuelve.

    @return Resultado de la prueba
    """
    br = importar("guard_bridge")
    unidad = unidad_detector()
    if not unidad:
        return Resultado(SKIP, "no hay ningún detector en marcha")
    if br.estado_detector(unidad) != "OK":
        return Resultado(SKIP, f"estado inicial {br.estado_detector(unidad)}, se esperaba OK")

    ejecutar(["systemctl", "stop", unidad])
    try:
        t_error = esperar(lambda: br.estado_detector(unidad) == "ERROR", 10)
        time.sleep(3)                       # LED ambar visible unos segundos
    finally:
        arranque = time.time()
        ejecutar(["systemctl", "start", unidad])

    def latido_nuevo():
        """Comprueba si el detector ha publicado un latido nuevo y esta en OK.

        @return True si hay latido nuevo
        """
        try:
            return (br.HEALTH_FILE.stat().st_mtime > arranque
                    and br.estado_detector(unidad) == "OK")
        except OSError:
            return False
    t_ok = esperar(latido_nuevo, 90)

    ok = t_error is not None and t_ok is not None
    detalle = (f"unidad {unidad}; paso a ERROR: "
               f"{'%.1f s' % t_error if t_error is not None else 'no ocurrió en 10 s'}; "
               f"vuelta a OK con el primer latido del detector arrancado: "
               f"{'%.1f s' % t_ok if t_ok is not None else 'no ocurrió en 90 s'}. "
               f"Comprobar a la vista: LED ámbar durante la parada")
    return Resultado(PASS if ok else FAIL, detalle)


def i3_lista_blanca_servicio() -> Resultado:
    """I3: comprueba que una emision declarada en la lista blanca pasa a
    conocida en el detector real.

    @return Resultado de la prueba
    """
    if not activo(UNIDAD_RF):
        return Resultado(SKIP, "el detector real no está en marcha")
    motivo = esperar_detector_listo()
    if motivo:
        return Resultado(SKIP, motivo)
    snap = leer_snapshot()
    candidatas = [e for e in snap.get("emisiones", [])
                  if e.get("clase") == "continua" and not e.get("conocido")]
    if not candidatas:
        return Resultado(SKIP, "no hay ninguna emisión continua no identificada "
                               "en este momento (encender un WiFi y repetir)")
    e = max(candidatas, key=lambda x: x.get("snr_db", 0))
    ini, fin = e["f_inicio_mhz"], e["f_fin_mhz"]

    copia = LISTA_BLANCA.with_suffix(".json.prueba")
    existia = LISTA_BLANCA.exists()
    if existia:
        shutil.copy2(LISTA_BLANCA, copia)
    try:
        datos = json.loads(LISTA_BLANCA.read_text()) if existia else {}
        datos.setdefault("emisores", []).append({
            "etiqueta": "prueba_automatica", "f_inicio_mhz": ini - 1,
            "f_fin_mhz": fin + 1, "clase": "continua"})
        LISTA_BLANCA.write_text(json.dumps(datos, indent=2, ensure_ascii=False))

        reinicio = time.time()
        ejecutar(["systemctl", "restart", UNIDAD_RF])

        def marcada():
            """Comprueba si el detector, tras reiniciarse, ya marca como conocida
            la emision de prueba anadida a la lista blanca.

            @return True si la emision aparece con la etiqueta de prueba
            """
            s = leer_snapshot()
            if not s or s.get("receptor") == "sin_datos":
                return False
            if _ts(s) < reinicio:
                return False
            return any(x.get("etiqueta") == "prueba_automatica" and x.get("conocido")
                       for x in s.get("emisiones", []))

        t = esperar(marcada, 120, paso=2)
    finally:
        if existia:
            shutil.move(copia, LISTA_BLANCA)
        else:
            LISTA_BLANCA.unlink(missing_ok=True)
        ejecutar(["systemctl", "restart", UNIDAD_RF])

    if t is None:
        return Resultado(FAIL, f"emisión {ini}-{fin} MHz declarada en el fichero; "
                               "no apareció como conocida en 120 s (puede haber "
                               "desaparecido del aire: repetir)")
    return Resultado(PASS, f"emisión {ini}-{fin} MHz declarada en el fichero; marcada como "
                           f"conocida tras reiniciar el detector ({t:.0f} s). "
                           "Lista blanca original restaurada")


def i4_receptor_exclusivo() -> Resultado:
    """I4: comprueba que con el detector en marcha ningun otro proceso
    puede abrir el HackRF.

    @return Resultado de la prueba
    """
    if not activo(UNIDAD_RF):
        return Resultado(SKIP, "el detector real no está en marcha")
    motivo = esperar_detector_listo()
    if motivo:
        return Resultado(SKIP, motivo)
    if shutil.which("hackrf_info") is None:
        return Resultado(SKIP, "hackrf_info no está instalado")
    salida = ejecutar(["hackrf_info"], timeout=20)
    texto = (salida.stdout + salida.stderr).lower()
    ocupado = "busy" in texto or "failed" in texto
    conflicto = UNIDAD_STUB in propiedad(UNIDAD_RF, "Conflicts")
    ok = ocupado and conflicto
    linea = next((l.strip() for l in (salida.stdout + salida.stderr).splitlines()
                  if "busy" in l.lower() or "failed" in l.lower()),
                 "abrió el receptor (" + " / ".join(
                     l.strip() for l in salida.stdout.splitlines()[:3]) + ")")
    detalle = (f"hackrf_info con el detector activo: {linea}; "
               f"el detector real y el simulado se excluyen en systemd: "
               f"{'sí' if conflicto else 'no'}")
    return Resultado(PASS if ok else FAIL, detalle)


def i5_cadena_arranque() -> Resultado:
    """I5: comprueba el orden de arranque declarado: modo silente,
    detector, puente y panel.

    @return Resultado de la prueba
    """
    cadena = [(UNIDAD_RF, UNIDAD_SILENTE), (UNIDAD_PUENTE, UNIDAD_RF),
              (UNIDAD_PANEL, UNIDAD_PUENTE)]
    faltan = [f"{u} no arranca después de {previo}" for u, previo in cadena
              if previo not in propiedad(u, "After").split()]
    detalle = "dependencias After= correctas" if not faltan else "; ".join(faltan)

    if activo(UNIDAD_SILENTE):
        def t(u):
            """Devuelve el instante en que una unidad de systemd paso a activa.

            @param  u  nombre de la unidad
            @return instante monotono en microsegundos, o 0 si no consta
            """
            return int(propiedad(u, "ActiveEnterTimestampMonotonic") or 0)
        orden = t(UNIDAD_SILENTE) <= t(UNIDAD_RF) <= t(UNIDAD_PUENTE)
        detalle += (f"; en este arranque el modo silente se activó antes que el "
                    f"detector y el puente: {'sí' if orden else 'no'}")
        ok = not faltan and orden
    else:
        detalle += "; el modo silente no está activo en este arranque (desarrollo)"
        ok = not faltan
    return Resultado(PASS if ok else FAIL, detalle)


def _ts(snapshot: dict) -> float:
    """Obtiene la marca de tiempo de un analisis del detector.

    @param  snapshot  analisis leido de /run/guard
    @return marca de tiempo Unix, o 0 si no tiene
    """
    from datetime import datetime
    try:
        return datetime.fromisoformat(snapshot["ts"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return 0.0


PRUEBAS = [
    ("I1", "ESP32: descarte de tramas corruptas", i1_tramas_corruptas, True),
    ("I2", "Puente: estado del detector caído y recuperado", i2_detector_caido, True),
    ("I3", "Detector: lista blanca leída del fichero", i3_lista_blanca_servicio, True),
    ("I4", "Receptor: acceso exclusivo del detector", i4_receptor_exclusivo, True),
    ("I5", "systemd: cadena de arranque de los servicios", i5_cadena_arranque, True),
]
