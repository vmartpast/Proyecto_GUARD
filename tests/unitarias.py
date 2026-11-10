"""unitarias: pruebas unitarias de funciones aisladas del detector y del
puente, con datos sinteticos y sin hardware.
"""

import json
import random
import tempfile
from pathlib import Path

from comun import PASS, FAIL, Resultado, importar

SUELO = -55.0
CANAL6 = range(2432, 2443)          # 11 MHz sobre el umbral, centrados en 2437


def _barridos_sinteticos(semilla: int = 1, wifi: bool = True, fhss: bool = True) -> list:
    """Genera diez barridos sinteticos con ruido, un WiFi en el canal 6
    y un emisor de salto de frecuencia.

    @param  semilla  semilla del generador aleatorio
    @param  wifi     incluir el WiFi
    @param  fhss     incluir el emisor de salto
    @return lista de barridos (frecuencia en MHz -> potencia en dB)
    """
    rnd = random.Random(semilla)
    candidatos = [f for f in range(2402, 2481) if not 2428 <= f <= 2446]
    barridos = []
    for k in range(10):
        b = {f: SUELO + rnd.uniform(-1, 1) for f in range(2400, 2501)}
        if wifi and k < 8:                      # presente en 8 de 10 barridos
            for f in CANAL6:
                b[f] = -33 + rnd.uniform(-1, 1)
        if fhss:                                # 10 canales distintos por barrido
            for f in rnd.sample(candidatos, 10):
                b[f] = -35 + rnd.uniform(-1, 1)
        barridos.append(b)
    return barridos


def u1_persistencia() -> Resultado:
    """U1: comprueba que la persistencia separa el WiFi del emisor de salto.

    @return Resultado de la prueba
    """
    rf = importar("guard_rf")
    emisiones = rf.identificar(rf.analizar(_barridos_sinteticos(), rf.MARGEN_DB))
    wifi = [e for e in emisiones if e["clase"] == "continua" and e["tipo"] == "wifi"]
    salto = [e for e in emisiones if e["clase"] == "salto_frecuencia"]
    ruido = rf.identificar(rf.analizar(_barridos_sinteticos(wifi=False, fhss=False),
                                       rf.MARGEN_DB))
    ok = (len(wifi) == 1 and wifi[0]["detalle"] == "canal 6"
          and len(salto) == 1 and not ruido)
    detalle = (f"WiFi: {wifi[0]['detalle'] if wifi else 'no detectado'}"
               f" (persistencia {wifi[0]['persistencia'] if wifi else '-'}); "
               f"salto: {salto[0]['canales_visitados'] if salto else 0} canales"
               f" (persistencia {salto[0]['persistencia'] if salto else '-'}); "
               f"solo ruido: {len(ruido)} emisiones")
    return Resultado(PASS if ok else FAIL, detalle)


def u2_detector_energia() -> Resultado:
    """U2: comprueba que el detector de energia sobre el barrido acumulado
    sube el suelo de ruido y pierde el WiFi.

    @return Resultado de la prueba
    """
    rf = importar("guard_rf")
    barridos = _barridos_sinteticos()
    acumulado = {f: max(b[f] for b in barridos) for f in barridos[0]}
    cortos = rf.analizar(barridos, rf.MARGEN_DB)
    acum = rf.analizar([acumulado], rf.MARGEN_DB)
    wifi_acum = [e for e in rf.identificar(acum) if e["tipo"] == "wifi"]
    subida = acum["suelo_ruido_db"] - cortos["suelo_ruido_db"]
    ok = subida >= 10 and not wifi_acum
    detalle = (f"suelo con barridos cortos {cortos['suelo_ruido_db']} dB; "
               f"con barrido acumulado {acum['suelo_ruido_db']} dB (+{subida:.1f} dB); "
               f"WiFi detectado sobre el acumulado: {'sí' if wifi_acum else 'no'}")
    return Resultado(PASS if ok else FAIL, detalle)


def u3_lista_blanca() -> Resultado:
    """U3: comprueba que la lista blanca silencia el WiFi sin ocultar el
    emisor de salto de la misma banda.

    @return Resultado de la prueba
    """
    rf = importar("guard_rf")
    emisiones = rf.identificar(rf.analizar(_barridos_sinteticos(), rf.MARGEN_DB))
    lista = [{"etiqueta": "wifi propio", "f_inicio_mhz": 2431,
              "f_fin_mhz": 2443, "clase": "continua"}]
    rf.marcar_conocidos(emisiones, lista)

    eventos = []
    emit_original = rf.emit
    rf.emit = eventos.append
    try:
        n = rf.emitir_eventos(emisiones)
    finally:
        rf.emit = emit_original

    wifi = next(e for e in emisiones if e["tipo"] == "wifi")
    salto = next(e for e in emisiones if e["clase"] == "salto_frecuencia")
    ok = (wifi["conocido"] and wifi["etiqueta"] == "wifi propio"
          and not salto["conocido"]
          and not any(e["tipo"] == "wifi" for e in eventos)
          and any(e["clase"] == "salto_frecuencia" for e in eventos))
    detalle = (f"WiFi conocido: {wifi['conocido']}; salto conocido: {salto['conocido']} "
               f"(solapa con la entrada); eventos emitidos: {n} "
               f"({', '.join(e['clase'] for e in eventos)})")
    return Resultado(PASS if ok else FAIL, detalle)


def u4_fichero_lista_blanca() -> Resultado:
    """U4: comprueba que un fichero de lista blanca corrupto o inexistente
    se lee como lista vacia sin detener el detector.

    @return Resultado de la prueba
    """
    rf = importar("guard_rf")
    with tempfile.TemporaryDirectory() as d:
        valido = Path(d) / "valida.json"
        valido.write_text(json.dumps({"emisores": [
            {"etiqueta": "a", "f_inicio_mhz": 2410, "f_fin_mhz": 2420}]}))
        roto = Path(d) / "rota.json"
        roto.write_text("{ esto no es json")
        errores = []
        emit_original = rf.emit
        rf.emit = errores.append
        try:
            n_valido = len(rf.cargar_lista_blanca(valido))
            n_roto = len(rf.cargar_lista_blanca(roto))
            n_falta = len(rf.cargar_lista_blanca(Path(d) / "no_existe.json"))
        finally:
            rf.emit = emit_original
    ok = n_valido == 1 and n_roto == 0 and n_falta == 0 and len(errores) == 1
    detalle = (f"fichero válido: {n_valido} entrada; fichero corrupto: {n_roto} entradas "
               f"y {len(errores)} error registrado; fichero inexistente: {n_falta} entradas")
    return Resultado(PASS if ok else FAIL, detalle)


def u5_tramas_puente() -> Resultado:
    """U5: comprueba la suma de control y el rechazo de tramas invalidas
    en el puente.

    @return Resultado de la prueba
    """
    br = importar("guard_bridge")
    cuerpo = "ACK|DET|"
    manual = 0
    for b in cuerpo.encode():
        manual ^= b
    cs = br.checksum(cuerpo)
    valida = br.descomponer(f"<{cuerpo}*{cs}")
    malo = "00" if cs != "00" else "01"
    corrupta = br.descomponer(f"<{cuerpo}*{malo}")
    sin_cs = br.descomponer("<ACK|DET|")
    ok = (cs == f"{manual:02X}" and valida == ["ACK", "DET"]
          and corrupta == [] and sin_cs == [])
    detalle = (f"checksum {cs} (esperado {manual:02X}); trama válida -> {valida}; "
               f"checksum incorrecto -> {corrupta}; sin checksum -> {sin_cs}")
    return Resultado(PASS if ok else FAIL, detalle)


PRUEBAS = [
    ("U1", "Persistencia: separa WiFi y salto de frecuencia", u1_persistencia, False),
    ("U2", "Detector de energía sobre barrido acumulado ante salto", u2_detector_energia, False),
    ("U3", "Lista blanca: filtrado por familia de emisión", u3_lista_blanca, False),
    ("U4", "Lista blanca: lectura de fichero válido, corrupto e inexistente",
     u4_fichero_lista_blanca, False),
    ("U5", "Puente: checksum y validación de tramas del ESP32", u5_tramas_puente, False),
]
