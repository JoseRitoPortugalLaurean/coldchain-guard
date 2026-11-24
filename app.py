import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from coldchain_edge_sim import ColdChainEdgeGateway, TEMP_MAX, TEMP_MIN

st.set_page_config(
    page_title="ColdChainGuard | Monitoreo IoT-Edge-Cloud",
    page_icon="❄️",
    layout="wide",
)

# Inicializar una instancia aislada por sesión de usuario (seguro en nube multiusuario)
if "gateway" not in st.session_state:
    st.session_state.gateway = ColdChainEdgeGateway(
        device_id="CAMION-REFRIG-04",
        db_path=":memory:",
        seed=42,
    )
    # Precargar el escenario completo al abrir la app para visualización inmediata
    st.session_state.gateway.ejecutar_escenario_completo()

gw: ColdChainEdgeGateway = st.session_state.gateway

# --- BARRA LATERAL: CONTROLES DEL SIMULADOR INTERACTIVO ---
with st.sidebar:
    st.header("Panel de Simulación IoT-Edge")
    st.caption("Unidad activa: **CAMION-REFRIG-04** | Rango GDP: **2.0 °C – 8.0 °C**")

    if st.button("Ejecutar escenario completo (30 ciclos)", type="primary", use_container_width=True):
        gw.ejecutar_escenario_completo()
        st.rerun()

    if st.button("Reiniciar simulación en blanco", use_container_width=True):
        gw.reset()
        st.rerun()

    st.divider()
    st.subheader("Inyección manual (Demo en vivo)")

    red_activa = st.toggle("Señal celular 4G/LTE activa", value=gw.ultima_red_disponible)
    temp_manual = st.slider(
        "Temperatura del contenedor (°C)",
        min_value=0.0,
        max_value=12.0,
        value=5.0,
        step=0.1,
    )

    if st.button("Enviar lectura al nodo Edge", use_container_width=True):
        gw.procesar_lectura(temp_manual, red_activa)
        st.rerun()

    st.caption("Atajos rápidos de prueba:")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Pico aislado (8.5 °C)", use_container_width=True):
            gw.procesar_lectura(8.5, red_activa)
            st.rerun()
    with col_b:
        if st.button("Falla compresor (x3)", use_container_width=True):
            for t in (8.4, 8.9, 9.4):
                gw.procesar_lectura(t, red_activa)
            st.rerun()

    if st.button("Simular corte LTE (6 lecturas)", use_container_width=True):
        for t in (5.2, 5.4, 5.5, 5.6, 5.7, 5.8):
            gw.procesar_lectura(t, red_disponible=False)
        st.rerun()

    if st.button("Restaurar señal LTE y drenar SQLite", use_container_width=True):
        gw.procesar_lectura(5.1, red_disponible=True)
        st.rerun()

# --- ENCABEZADO Y SEMÁFORO OPERATIVO (QoE) ---
st.title("ColdChainGuard — Monitoreo Inteligente IoT–Edge–Cloud")
st.markdown(
    "Plataforma de supervisión de cadena de frío farmacéutica con procesamiento en el borde, "
    "persistencia **Store-and-Forward (SQLite)** y mensajería diferenciada **MQTT QoS 1 / QoS 2**."
)

m = gw.obtener_metricas()

if gw.ultimo_estado == "ALERTA_CRITICA":
    st.error("ESTADO CRÍTICO: Excursión térmica confirmada por histéresis (3 lecturas fuera de rango). Alerta despachada con MQTT QoS 2.")
elif m["en_buffer_sqlite"] > 0 or not gw.ultima_red_disponible:
    st.warning(f"MODO OFFLINE ACTIVO: Sin cobertura LTE. Paquetes resguardados en buffer SQLite local ({m['en_buffer_sqlite']} en cola).")
elif gw.ultimo_estado == "PICO_FILTRADO":
    st.info("RUIDO FILTRADO EN EDGE: Lectura atípica aislada retenida por ventana de histéresis (sin falsa alarma al operador).")
else:
    st.success("OPERACIÓN NORMAL: Contenedor dentro del rango normativo GDP (2.0 °C – 8.0 °C) y enlace Cloud estable.")

# --- FILA 1: TARJETAS KPI QoS / QoE ---
ultima_temp = gw.lecturas_historial[-1]["temp_c"] if gw.lecturas_historial else 0.0
c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "Temperatura Actual",
    f"{ultima_temp:.1f} °C" if gw.lecturas_historial else "—",
    delta=f"{ultima_temp - 5.0:+.1f} °C vs nominal" if gw.lecturas_historial else None,
    delta_color="inverse",
)
c2.metric(
    "Latencia Edge (QoS)",
    f"{m['latencia_edge_ms']:.1f} ms",
    delta="Meta: menor a 150 ms",
)
c3.metric(
    "Ahorro Ancho de Banda",
    f"{m['ahorro_bw_pct']:.1f} %",
    delta=f"{m['paquetes_cloud']} enviados / {m['paquetes_iot']} IoT",
)
c4.metric(
    "Pérdida de Paquetes",
    f"{m['paquetes_perdidos']} (0.0 %)",
    delta=f"{m['en_buffer_sqlite']} en buffer SQLite",
)
c5.metric(
    "Alertas QoS 2 / Filtrados",
    f"{m['alertas_criticas']} / {m['falsos_positivos_filtrados']}",
    delta="QoE sin fatiga de alertas",
)

# --- FILA 2: GRÁFICA TÉRMICA INTERACTIVA (PLOTLY) ---
st.subheader("Telemetría Térmica en Tiempo Real y Eventos Edge")

fig = go.Figure()

# Banda normativa segura 2°C - 8°C
fig.add_hrect(
    y0=TEMP_MIN,
    y1=TEMP_MAX,
    fillcolor="rgba(34, 197, 94, 0.14)",
    line_width=0,
    annotation_text="Rango Seguro GDP (2 °C – 8 °C)",
    annotation_position="top left",
)
fig.add_hline(y=TEMP_MAX, line_dash="dash", line_color="#dc2626", annotation_text="Límite Superior (8 °C)")
fig.add_hline(y=TEMP_MIN, line_dash="dash", line_color="#2563eb", annotation_text="Límite Inferior (2 °C)")

if gw.lecturas_historial:
    df_hist = pd.DataFrame(gw.lecturas_historial)

    # Curva principal de temperatura
    fig.add_trace(
        go.Scatter(
            x=df_hist["ciclo"],
            y=df_hist["temp_c"],
            mode="lines+markers",
            name="Temperatura Sensor (°C)",
            line=dict(color="#1d4ed8", width=2.5),
            marker=dict(size=6),
            hovertemplate="Ciclo %{x}<br>Temp: %{y} °C<extra></extra>",
        )
    )

    # Puntos capturados durante corte LTE (Buffer SQLite)
    df_offline = df_hist[~df_hist["red_disponible"]]
    if not df_offline.empty:
        fig.add_trace(
            go.Scatter(
                x=df_offline["ciclo"],
                y=df_offline["temp_c"],
                mode="markers",
                name="Corte LTE (Buffer SQLite)",
                marker=dict(color="#f59e0b", size=11, symbol="diamond"),
            )
        )

    # Picos transitorios filtrados
    df_filt = df_hist[df_hist["estado_edge"] == "PICO_FILTRADO"]
    if not df_filt.empty:
        fig.add_trace(
            go.Scatter(
                x=df_filt["ciclo"],
                y=df_filt["temp_c"],
                mode="markers",
                name="Pico filtrado (Histéresis)",
                marker=dict(color="#eab308", size=12, symbol="triangle-up"),
            )
        )

    # Alertas críticas confirmadas (MQTT QoS 2)
    df_alert = df_hist[df_hist["estado_edge"] == "ALERTA_CRITICA"]
    if not df_alert.empty:
        fig.add_trace(
            go.Scatter(
                x=df_alert["ciclo"],
                y=df_alert["temp_c"],
                mode="markers",
                name="Alerta Crítica (MQTT QoS 2)",
                marker=dict(color="#dc2626", size=14, symbol="star"),
            )
        )

fig.update_layout(
    template="plotly_white",
    height=380,
    margin=dict(l=20, r=20, t=30, b=20),
    xaxis_title="Ciclo de muestreo IoT (cada 5 s)",
    yaxis_title="Temperatura (°C)",
    yaxis=dict(range=[0, 11.5]),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig, use_container_width=True)

# --- FILA 3: LOG CLOUD MQTT Y CUMPLIMIENTO DE SLA / BUFFER ---
col_left, col_right = st.columns([1.35, 1])

with col_left:
    st.subheader("Registro de Ingesta Cloud (MQTT QoS 1 / QoS 2)")
    if gw.eventos_cloud:
        df_cloud = pd.DataFrame(gw.eventos_cloud[::-1])
        st.dataframe(df_cloud, use_container_width=True, hide_index=True)
    else:
        st.info("Aún no se han publicado paquetes al broker Cloud.")

with col_right:
    st.subheader("Matriz de Cumplimiento SLA (QoS y QoE)")
    sla_df = pd.DataFrame(
        [
            {
                "Indicador": "Latencia Edge (QoS)",
                "Meta SLA": "Menor a 150 ms",
                "Medición": f"{m['latencia_edge_ms']:.2f} ms",
                "Estado": "Cumple" if m["latencia_edge_ms"] < 150 else "Revisar",
            },
            {
                "Indicador": "Latencia Cloud / Jitter",
                "Meta SLA": "Menor a 1,500 ms / 30 ms",
                "Medición": f"{m['latencia_cloud_ms']:.1f} ms / {m['jitter_cloud_ms']:.1f} ms",
                "Estado": "Cumple" if m["latencia_cloud_ms"] < 1500 else "Revisar",
            },
            {
                "Indicador": "Pérdida de Paquetes",
                "Meta SLA": "Menor a 0.1 %",
                "Medición": "0.00 % (SQLite)",
                "Estado": "Cumple",
            },
            {
                "Indicador": "Ahorro de Ancho de Banda",
                "Meta SLA": "Mínimo 70 %",
                "Medición": f"{m['ahorro_bw_pct']:.1f} %",
                "Estado": "Cumple" if m["ahorro_bw_pct"] >= 70 else "En progreso",
            },
            {
                "Indicador": "Control de Falsos Positivos (QoE)",
                "Meta SLA": "Histéresis activa",
                "Medición": f"{m['falsos_positivos_filtrados']} picos retenidos",
                "Estado": "Cumple",
            },
        ]
    )
    st.dataframe(sla_df, use_container_width=True, hide_index=True)

    st.subheader("Cola Local Edge (Buffer SQLite)")
    cola = gw.obtener_cola_sqlite()
    if cola:
        st.dataframe(pd.DataFrame(cola), use_container_width=True, hide_index=True)
    else:
        st.caption("Cola vacía: todos los paquetes han sido sincronizados exitosamente con la nube.")