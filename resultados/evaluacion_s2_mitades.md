# Evaluacion final

Rasgos: sin potencia. Externo: 2020 ventanas, 25 capturas, 5 pliegues por captura. Propio: 1340 ventanas, 67 capturas, sesiones s21, s22 (entrenar con una, evaluar con la otra).

## 1. Deteccion de UAV (conjunto externo)

| Clase | Ventanas | Detectado como UAV | Clase exacta |
|---|---|---|---|
| sin_uav | 420 | 24.5 % | 75.5 % |
| uav_f450 | 300 | 99.0 % | 86.7 % |
| uav_hunter | 460 | 93.0 % | 80.2 % |
| uav_mavic_pro | 420 | 75.0 % | 66.7 % |
| uav_mini3 | 420 | 78.1 % | 65.7 % |

Sensibilidad (UAV detectados): 85.5 %. Precision: 93.0 %. Exactitud multiclase: 74.4 %.

Matriz de confusion (filas: real; columnas: predicho):

| | sin_uav | uav_f450 | uav_hunter | uav_mavic_pro | uav_mini3 |
|---|---|---|---|---|---|
| sin_uav | 317 | 0 | 21 | 66 | 16 |
| uav_f450 | 3 | 260 | 29 | 8 | 0 |
| uav_hunter | 32 | 29 | 369 | 30 | 0 |
| uav_mavic_pro | 105 | 6 | 22 | 280 | 7 |
| uav_mini3 | 92 | 0 | 28 | 24 | 276 |

## 2. Falsas alarmas en el entorno propio (sesion no vista)

| Emisor | Sesion | Ventanas | Falsa alarma (ventana) | Falsa alarma (por segundo) | Confundido sobre todo con |
|---|---|---|---|---|---|
| ambiente | s21 | 140 | 0.0 % | 0.0 % | - |
| ambiente | s22 | 160 | 0.6 % | 0.0 % | uav_mavic_pro |
| bluetooth_audio | s21 | 140 | 0.0 % | 0.0 % | - |
| bluetooth_audio | s22 | 140 | 8.6 % | 0.0 % | uav_f450 |
| bluetooth_busqueda | s21 | 120 | 2.5 % | 0.0 % | uav_hunter |
| bluetooth_busqueda | s22 | 140 | 1.4 % | 0.0 % | uav_mavic_pro |
| wifi_trafico | s21 | 240 | 0.4 % | 0.0 % | uav_hunter |
| wifi_trafico | s22 | 260 | 1.5 % | 0.0 % | uav_mavic_pro |

Tasa global de falsas alarmas: 1.7 % por ventana, 0.0 % por segundo.

## 3. Decision segun la duracion de la captura

| Ventanas (duracion) | Sensibilidad UAV | Precision | Exactitud multiclase | Falsas alarmas propias |
|---|---|---|---|---|
| 1 (50 ms) | 85.5 % | 93.0 % | 74.4 % | 1.7 % (1340 decisiones) |
| 5 (250 ms) | 89.1 % | 94.1 % | 80.7 % | 0.0 % (268 decisiones) |
| 10 (500 ms) | 90.0 % | 95.4 % | 84.2 % | 0.0 % (134 decisiones) |
| 20 (1000 ms) | 87.5 % | 94.6 % | 83.2 % | 0.0 % (67 decisiones) |

