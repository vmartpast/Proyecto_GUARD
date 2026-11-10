# Evaluacion final

Rasgos: sin potencia. Externo: 2020 ventanas, 25 capturas, 5 pliegues por captura. Propio: 2520 ventanas, 126 capturas, sesiones s21, s22, s3 (entrenar con una, evaluar con la otra).

## 1. Deteccion de UAV (conjunto externo)

| Clase | Ventanas | Detectado como UAV | Clase exacta |
|---|---|---|---|
| sin_uav | 420 | 21.0 % | 79.0 % |
| uav_f450 | 300 | 95.7 % | 84.7 % |
| uav_hunter | 460 | 92.4 % | 80.7 % |
| uav_mavic_pro | 420 | 73.1 % | 65.5 % |
| uav_mini3 | 420 | 75.2 % | 63.8 % |

Sensibilidad (UAV detectados): 83.4 %. Precision: 93.8 %. Exactitud multiclase: 74.3 %.

Matriz de confusion (filas: real; columnas: predicho):

| | sin_uav | uav_f450 | uav_hunter | uav_mavic_pro | uav_mini3 |
|---|---|---|---|---|---|
| sin_uav | 332 | 0 | 20 | 54 | 14 |
| uav_f450 | 13 | 254 | 26 | 7 | 0 |
| uav_hunter | 35 | 29 | 371 | 25 | 0 |
| uav_mavic_pro | 113 | 9 | 17 | 275 | 6 |
| uav_mini3 | 104 | 0 | 26 | 22 | 268 |

## 2. Falsas alarmas en el entorno propio (sesion no vista)

| Emisor | Sesion | Ventanas | Falsa alarma (ventana) | Falsa alarma (por segundo) | Confundido sobre todo con |
|---|---|---|---|---|---|
| ambiente | s21 | 140 | 0.0 % | 0.0 % | - |
| ambiente | s22 | 160 | 0.0 % | 0.0 % | - |
| bluetooth_audio | s21 | 140 | 0.0 % | 0.0 % | - |
| bluetooth_audio | s22 | 140 | 5.0 % | 0.0 % | uav_f450 |
| bluetooth_audio | s3 | 1180 | 6.8 % | 0.0 % | uav_mavic_pro |
| bluetooth_busqueda | s21 | 120 | 2.5 % | 0.0 % | uav_hunter |
| bluetooth_busqueda | s22 | 140 | 0.7 % | 0.0 % | uav_mavic_pro |
| wifi_trafico | s21 | 240 | 0.4 % | 0.0 % | uav_hunter |
| wifi_trafico | s22 | 260 | 0.8 % | 0.0 % | uav_mavic_pro |

Tasa global de falsas alarmas: 3.7 % por ventana, 0.0 % por segundo.

## 3. Decision segun la duracion de la captura

| Ventanas (duracion) | Sensibilidad UAV | Precision | Exactitud multiclase | Falsas alarmas propias |
|---|---|---|---|---|
| 1 (50 ms) | 83.4 % | 93.8 % | 74.3 % | 3.7 % (2520 decisiones) |
| 5 (250 ms) | 87.8 % | 94.3 % | 79.7 % | 0.6 % (504 decisiones) |
| 10 (500 ms) | 88.1 % | 95.3 % | 84.2 % | 0.4 % (252 decisiones) |
| 20 (1000 ms) | 87.5 % | 94.6 % | 83.2 % | 0.0 % (126 decisiones) |

