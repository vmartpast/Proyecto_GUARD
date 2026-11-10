# Evaluacion final

Rasgos: sin potencia. Externo: 2020 ventanas, 25 capturas, 5 pliegues por captura. Propio: 3080 ventanas, 154 capturas, sesiones s1, s2 (entrenar con una, evaluar con la otra).

## 1. Deteccion de UAV (conjunto externo)

| Clase | Ventanas | Detectado como UAV | Clase exacta |
|---|---|---|---|
| sin_uav | 420 | 21.0 % | 79.0 % |
| uav_f450 | 300 | 99.3 % | 87.0 % |
| uav_hunter | 460 | 93.9 % | 80.7 % |
| uav_mavic_pro | 420 | 76.2 % | 66.7 % |
| uav_mini3 | 420 | 77.6 % | 65.5 % |

Sensibilidad (UAV detectados): 86.0 %. Precision: 94.0 %. Exactitud multiclase: 75.2 %.

Matriz de confusion (filas: real; columnas: predicho):

| | sin_uav | uav_f450 | uav_hunter | uav_mavic_pro | uav_mini3 |
|---|---|---|---|---|---|
| sin_uav | 332 | 0 | 19 | 55 | 14 |
| uav_f450 | 2 | 261 | 28 | 9 | 0 |
| uav_hunter | 28 | 35 | 371 | 26 | 0 |
| uav_mavic_pro | 100 | 7 | 27 | 280 | 6 |
| uav_mini3 | 94 | 0 | 29 | 22 | 275 |

## 2. Falsas alarmas en el entorno propio (sesion no vista)

| Emisor | Sesion | Ventanas | Falsa alarma (ventana) | Falsa alarma (por segundo) | Confundido sobre todo con |
|---|---|---|---|---|---|
| ambiente | s1 | 600 | 74.3 % | 70.0 % | uav_mavic_pro |
| ambiente | s2 | 300 | 14.3 % | 0.0 % | uav_mavic_pro |
| bluetooth_audio | s1 | 560 | 27.3 % | 3.6 % | uav_mavic_pro |
| bluetooth_audio | s2 | 280 | 56.4 % | 64.3 % | uav_mavic_pro |
| bluetooth_busqueda | s1 | 280 | 87.9 % | 100.0 % | uav_mavic_pro |
| bluetooth_busqueda | s2 | 260 | 88.5 % | 100.0 % | uav_mavic_pro |
| wifi_trafico | s1 | 300 | 18.3 % | 13.3 % | uav_mavic_pro |
| wifi_trafico | s2 | 500 | 11.6 % | 0.0 % | uav_mavic_pro |

Tasa global de falsas alarmas: 45.1 % por ventana, 39.0 % por segundo.

