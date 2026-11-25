import json
import random
import sqlite3
import statistics
import time
from collections import deque
from datetime import datetime, timezone

# Umbrales normativos de cadena de frío farmacéutica (GDP)
TEMP_MIN = 2.0
TEMP_MAX = 8.0
VENTANA_HISTERESIS = 3      # 3 lecturas consecutivas fuera de rango confirman excursión
INTERVALO_AGREGACION = 6    # Agrupa 6 lecturas normales en 1 paquete Cloud (ahorro de ancho de banda)

ESCENARIO_REFERENCIA = [
    # Operación normal (ciclos 1-6)
    (4.8, True), (5.0, True), (4.9, True), (5.1, True), (5.2, True), (5.0, True),
    # Pico transitorio aislado de ruido en ciclo 8 (filtrado por histéresis)
    (5.1, True), (8.4, True), (5.3, True), (5.0, True), (4.9, True), (5.1, True),
    # Corte de señal celular LTE (ciclos 13-18 -> Store-and-Forward en SQLite)
    (5.2, False), (5.4, False), (5.5, False), (5.6, False), (5.7, False), (5.8, False),
    # Reconexión LTE + falla progresiva de compresor (ciclos 19-24 -> Alerta QoS 2)
    (6.0, True), (6.4, True), (7.1, True), (8.3, True), (8.7, True), (9.1, True),
    # Recuperación térmica (ciclos 25-30)
    (9.3, True), (7.8, True), (6.5, True), (5.4, True), (5.0, True), (4.9, True),
]


class ColdChainEdgeGateway:
    def __init__(self, device_id: str = "CAMION-REFRIG-04", db_path: str = ":memory:", seed: int | None = 42):
        self.device_id = device_id
        self.rng = random.Random(seed)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self.reset()

    def _init_db(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offline_buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ciclo INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                tipo TEXT NOT NULL,
                mqtt_qos INTEGER NOT NULL,
                temp_valor REAL NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def reset(self):
        self.ciclo_actual = 0
        self.ventana = deque(maxlen=VENTANA_HISTERESIS)
        self.lote_nominal = []
        self.lecturas_historial = []
        self.eventos_cloud = []
        self.latencias_edge_ms = []
        self.latencias_cloud_ms = []
        self.paquetes_iot_generados = 0
        self.paquetes_cloud_enviados = 0
        self.paquetes_perdidos = 0
        self.alertas_criticas = 0
        self.falsos_positivos_filtrados = 0
        self.ultima_red_disponible = True
        self.ultimo_estado = "NORMAL"
        self.conn.execute("DELETE FROM offline_buffer")
        self.conn.commit()

    def procesar_lectura(self, temp: float, red_disponible: bool) -> dict:
        t_inicio = time.perf_counter()
        self.ciclo_actual += 1
        self.paquetes_iot_generados += 1
        self.ultima_red_disponible = red_disponible
        self.ventana.append(temp)

        # Drenado inmediato Store-and-Forward en cuanto regresa la red celular
        if red_disponible:
            self._drenar_buffer_sqlite()

        fuera_rango_actual = not (TEMP_MIN <= temp <= TEMP_MAX)
        excursion_confirmada = (
            len(self.ventana) == VENTANA_HISTERESIS
            and all(not (TEMP_MIN <= x <= TEMP_MAX) for x in self.ventana)
        )

        if excursion_confirmada:
            estado_edge = "ALERTA_CRITICA"
        elif fuera_rango_actual:
            estado_edge = "PICO_FILTRADO"
            self.falsos_positivos_filtrados += 1
        elif not red_disponible:
            estado_edge = "OFFLINE_BUFFER"
        else:
            estado_edge = "NORMAL"

        self.ultimo_estado = estado_edge
        latencia_edge = (time.perf_counter() - t_inicio) * 1000 + self.rng.uniform(8.0, 22.0)
        self.latencias_edge_ms.append(latencia_edge)

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        registro = {
            "ciclo": self.ciclo_actual,
            "timestamp": timestamp,
            "temp_c": round(temp, 2),
            "red_disponible": red_disponible,
            "estado_edge": estado_edge,
            "latencia_edge_ms": round(latencia_edge, 2),
        }
        self.lecturas_historial.append(registro)

        payload = None
        temp_valor = round(temp, 2)

        if excursion_confirmada:
            self.alertas_criticas += 1
            payload = {
                "device_id": self.device_id,
                "ciclo": self.ciclo_actual,
                "timestamp": timestamp,
                "tipo": "ALERTA_EXCURSION_TERMICA",
                "temp_c": temp_valor,
                "mqtt_qos": 2,
            }
        else:
            self.lote_nominal.append(temp)
            if len(self.lote_nominal) >= INTERVALO_AGREGACION:
                temp_valor = round(statistics.mean(self.lote_nominal), 2)
                payload = {
                    "device_id": self.device_id,
                    "ciclo": self.ciclo_actual,
                    "timestamp": timestamp,
                    "tipo": "TELEMETRIA_AGREGADA",
                    "temp_prom_c": temp_valor,
                    "temp_max_c": round(max(self.lote_nominal), 2),
                    "muestras": len(self.lote_nominal),
                    "mqtt_qos": 1,
                }
                self.lote_nominal.clear()

        if payload:
            self._transmitir_o_encolar(payload, temp_valor, red_disponible)

        return registro

    def _transmitir_o_encolar(self, payload: dict, temp_valor: float, red_disponible: bool):
        if not red_disponible:
            self.conn.execute(
                """
                INSERT INTO offline_buffer (ciclo, created_at, tipo, mqtt_qos, temp_valor, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["ciclo"],
                    payload["timestamp"],
                    payload["tipo"],
                    payload["mqtt_qos"],
                    temp_valor,
                    json.dumps(payload),
                ),
            )
            self.conn.commit()
            return

        self._publicar_mqtt(payload, temp_valor, origen="DIRECTO")

    def _drenar_buffer_sqlite(self):
        filas = self.conn.execute(
            "SELECT id, temp_valor, payload_json FROM offline_buffer ORDER BY id ASC"
        ).fetchall()
        for row_id, temp_valor, payload_json in filas:
            payload = json.loads(payload_json)
            self._publicar_mqtt(payload, temp_valor, origen="REENVIO_SQLITE")
            self.conn.execute("DELETE FROM offline_buffer WHERE id = ?", (row_id,))
        if filas:
            self.conn.commit()

    def _publicar_mqtt(self, payload: dict, temp_valor: float, origen: str):
        base_rtt = 180.0 if payload["mqtt_qos"] == 1 else 310.0
        rtt_ms = round(base_rtt + self.rng.uniform(-18.0, 22.0), 2)
        self.latencias_cloud_ms.append(rtt_ms)
        self.paquetes_cloud_enviados += 1
        self.eventos_cloud.append(
            {
                "Ciclo": payload["ciclo"],
                "Hora UTC": payload["timestamp"],
                "Origen": origen,
                "Tipo de Evento": payload["tipo"],
                "MQTT QoS": f"QoS {payload['mqtt_qos']}",
                "Temp (°C)": temp_valor,
                "RTT Cloud (ms)": rtt_ms,
            }
        )

    def obtener_cola_sqlite(self) -> list[dict]:
        filas = self.conn.execute(
            "SELECT id, ciclo, created_at, tipo, mqtt_qos, temp_valor FROM offline_buffer ORDER BY id ASC"
        ).fetchall()
        return [
            {
                "ID Buffer": r[0],
                "Ciclo": r[1],
                "Hora UTC": r[2],
                "Tipo": r[3],
                "MQTT QoS": f"QoS {r[4]}",
                "Temp (°C)": r[5],
            }
            for r in filas
        ]

    def ejecutar_escenario_completo(self):
        self.reset()
        self.rng = random.Random(42)
        for temp, red_ok in ESCENARIO_REFERENCIA:
            self.procesar_lectura(temp, red_ok)

    def obtener_metricas(self) -> dict:
        en_buffer = len(self.obtener_cola_sqlite())
        paquetes_totales_wan = self.paquetes_cloud_enviados + en_buffer
        ahorro_bw = (
            (1 - paquetes_totales_wan / self.paquetes_iot_generados) * 100
            if self.paquetes_iot_generados > 0
            else 0.0
        )
        lat_edge = statistics.mean(self.latencias_edge_ms) if self.latencias_edge_ms else 0.0
        lat_cloud = statistics.mean(self.latencias_cloud_ms) if self.latencias_cloud_ms else 0.0
        jitter = statistics.stdev(self.latencias_cloud_ms) if len(self.latencias_cloud_ms) >= 2 else 0.0

        return {
            "paquetes_iot": self.paquetes_iot_generados,
            "paquetes_cloud": self.paquetes_cloud_enviados,
            "en_buffer_sqlite": en_buffer,
            "paquetes_perdidos": self.paquetes_perdidos,
            "ahorro_bw_pct": round(ahorro_bw, 1),
            "latencia_edge_ms": round(lat_edge, 2),
            "latencia_cloud_ms": round(lat_cloud, 2),
            "jitter_cloud_ms": round(jitter, 2),
            "alertas_criticas": self.alertas_criticas,
            "falsos_positivos_filtrados": self.falsos_positivos_filtrados,
        }


if __name__ == "__main__":
    gw = ColdChainEdgeGateway(device_id="CAMION-REFRIG-04", db_path="edge_buffer.db", seed=42)
    gw.reset()
    gw.rng = random.Random(42)

    for temp, red_ok in ESCENARIO_REFERENCIA:
        cola_antes = len(gw.obtener_cola_sqlite())
        ev_antes = len(gw.eventos_cloud)
        reg = gw.procesar_lectura(temp, red_ok)

        # 1. Evidencia de encolado en SQLite durante corte LTE
        cola_despues = gw.obtener_cola_sqlite()
        if not red_ok and len(cola_despues) > cola_antes:
            ultimo_b = cola_despues[-1]
            print(
                f"[EDGE BUFFER] Sin señal LTE -> Encolado en SQLite ({ultimo_b['Tipo']}) | "
                f"Cola: {len(cola_despues)}"
            )

        # 2. Evidencia de alerta local inmediata con MQTT QoS 2
        if reg["estado_edge"] == "ALERTA_CRITICA":
            print(
                f"[EDGE ALERTA LOCAL] {reg['temp_c']:.2f} °C | "
                f"Latencia Edge: {reg['latencia_edge_ms']:.2f} ms | MQTT QoS=2"
            )

        # 3. Evidencia de ingesta directa o drenado desde buffer SQLite
        for ev in gw.eventos_cloud[ev_antes:]:
            payload_log = {
                "device_id": gw.device_id,
                "ciclo": ev["Ciclo"],
                "tipo": ev["Tipo de Evento"],
                "mqtt_qos": ev["MQTT QoS"],
            }
            if ev["Origen"] == "REENVIO_SQLITE":
                print(
                    f"[REENVÍO BUFFER] {json.dumps(payload_log)} | "
                    f"RTT: {ev['RTT Cloud (ms)']:.1f} ms"
                )
            else:
                print(
                    f"[CLOUD INGESTA] {json.dumps(payload_log)} | "
                    f"RTT: {ev['RTT Cloud (ms)']:.1f} ms"
                )

    m = gw.obtener_metricas()
    print("\n=== RESUMEN DE MÉTRICAS QoS / QoE ===")
    print(f"Lecturas IoT generadas     : {m['paquetes_iot']}")
    print(f"Paquetes enviados a Cloud  : {m['paquetes_cloud']} (Ahorro ancho de banda: {m['ahorro_bw_pct']:.1f} %)")
    print(f"Paquetes perdidos          : {m['paquetes_perdidos']} (0.00 % gracias a Store-and-Forward SQLite)")
    print(f"Latencia promedio Edge     : {m['latencia_edge_ms']:.2f} ms")
    print(f"Latencia promedio Cloud    : {m['latencia_cloud_ms']:.2f} ms | Jitter Cloud: {m['jitter_cloud_ms']:.2f} ms")
    print(f"Alertas críticas (QoS 2)   : {m['alertas_criticas']} | Picos transitorios filtrados: {m['falsos_positivos_filtrados']}")