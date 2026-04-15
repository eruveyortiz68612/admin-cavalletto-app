"""Agente Administrativo Cavalletto — App Streamlit.

Chat con tool use + sidebar con KPIs en vivo.
"""

import base64
import json
import streamlit as st
import anthropic

from system_prompt import get_system_prompt
from tools import (
    ANTHROPIC_TOOLS, TOOL_HANDLERS,
    actualizar_foto_item, subir_factura,
    _foto_bytes, _factura_bytes,
)
from db import db_info, db_exists, fmt_money, current_period
from kpis import (
    kpi_cobranza_mes, kpi_morosos_count, kpi_gastos_mes,
    kpi_stock_bajo, kpi_alertas, kpi_utilidad_mes,
)
from doc_generator import generate as generate_doc

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 4096

# ── Configuracion de pagina ─────────────────────────────────────────
st.set_page_config(
    page_title="Agente Administrativo Cavalletto",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.main-header h1 { color: #2E5C3F; font-size: 1.8rem; margin-bottom: 0.2rem; }
.main-header p { color: #7B4F2E; font-size: 0.95rem; }
[data-testid="stSidebar"] { background-color: #EFF5ED; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { color: #2E5C3F; }
.kpi-card {
    background: #FDF8F0; padding: 10px 12px; border-radius: 10px;
    border-left: 4px solid #2E5C3F; margin-bottom: 8px;
}
.kpi-label { color: #7B4F2E; font-size: 0.75rem; text-transform: uppercase; }
.kpi-value { color: #2E5C3F; font-size: 1.15rem; font-weight: 600; }
.kpi-sub { color: #666; font-size: 0.75rem; }
.stDownloadButton > button {
    background-color: #2E5C3F; color: white; border: none; border-radius: 8px;
}
.alert-warn { color: #B45309; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)

# ── Estado de sesion ────────────────────────────────────────────────
ss = st.session_state
ss.setdefault("messages", [])
ss.setdefault("api_key", "")
ss.setdefault("api_key_valid", False)
ss.setdefault("user_name", "")
ss.setdefault("pending_downloads", [])
ss.setdefault("pending_factura", None)  # {factura_id, bytes, mime, nombre}
ss.setdefault("fotos_to_show", [])       # [{item_id, nombre}]


# ── Ejecucion del tool loop ─────────────────────────────────────────

def _run_tool(name, tool_input):
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Tool '{name}' no existe"}
    try:
        return handler(**(tool_input or {}))
    except Exception as e:
        return {"error": f"Fallo ejecutando {name}: {e}"}


def _collect_download(tool_name, result):
    """Si el tool genero un reporte o foto, lo anade a downloads/fotos_to_show."""
    if not isinstance(result, dict) or result.get("error"):
        return
    # Foto de inventario — marcar para mostrar en sidebar
    if result.get("tipo") == "foto" and result.get("tiene_foto"):
        item_id = result.get("item_id")
        if item_id is not None:
            ss.fotos_to_show.append({
                "item_id": item_id,
                "nombre": result.get("nombre", f"Item {item_id}"),
            })
        return
    # Reporte generado
    reporte = result.get("reporte")
    if not reporte:
        return
    try:
        doc = generate_doc(reporte, mes=result.get("mes"), pago_id=result.get("pago_id"))
    except Exception as e:
        ss.pending_downloads.append({
            "error": f"Error generando {reporte}: {e}"
        })
        return
    if doc is None:
        return
    data, filename, mime = doc
    ss.pending_downloads.append({
        "data": data, "filename": filename, "mime": mime,
        "label": f"📥 {filename}",
    })


def _build_user_content(user_text, pending_factura=None):
    """Construye el content del mensaje user.

    Si hay una factura pendiente, la inyecta como bloque multimodal (PDF o
    imagen) junto con el texto. Si no, retorna un string.
    """
    if not pending_factura:
        return user_text
    mime = pending_factura["mime"]
    b64 = base64.standard_b64encode(pending_factura["bytes"]).decode("utf-8")
    if mime == "application/pdf":
        attachment = {
            "type": "document",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }
    else:
        attachment = {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }
    annotation = (
        f"[Adjunto la factura con ID {pending_factura['factura_id']} "
        f"({pending_factura['nombre']}). Analizala y procesa con `procesar_factura` "
        f"si extraes monto + fecha con confianza. Si no, rechazala.] "
        f"{user_text}"
    )
    return [attachment, {"type": "text", "text": annotation}]


def _chat_turn(client, user_message, factura=None):
    """Ejecuta un turno de chat con loop de tool use. Retorna texto final.

    Si `factura` no es None, se inyecta como adjunto multimodal en el mensaje
    del usuario enviado a la API. El historial guardado en session_state usa
    solo texto para evitar acumular base64 entre reruns.
    """
    # Historial: guardamos texto (con una marca si hubo adjunto)
    display_text = user_message
    if factura:
        display_text = f"📄 _[Factura adjunta: {factura['nombre']}]_\n\n{user_message}"
    ss.messages.append({"role": "user", "content": display_text})

    api_msgs = [{"role": m["role"], "content": m["content"]}
                for m in ss.messages[:-1]]
    # Ultimo mensaje con potencial adjunto
    api_msgs.append({
        "role": "user",
        "content": _build_user_content(user_message, factura),
    })
    final_text_parts = []

    for _ in range(10):  # max 10 iteraciones de tool use
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=get_system_prompt(),
            tools=ANTHROPIC_TOOLS,
            messages=api_msgs,
        )

        # Recolectar texto y tool_uses
        assistant_blocks = []
        tool_uses = []
        for block in resp.content:
            if block.type == "text":
                assistant_blocks.append({"type": "text", "text": block.text})
                if block.text:
                    final_text_parts.append(block.text)
            elif block.type == "tool_use":
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                tool_uses.append(block)

        # Siempre guardar el turno del assistant con bloques originales
        api_msgs.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason != "tool_use" or not tool_uses:
            break

        # Ejecutar tools y responder
        tool_results = []
        for tu in tool_uses:
            result = _run_tool(tu.name, tu.input)
            _collect_download(tu.name, result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        api_msgs.append({"role": "user", "content": tool_results})

    final_text = "\n\n".join(final_text_parts).strip() or "(sin respuesta)"
    ss.messages.append({"role": "assistant", "content": final_text})
    return final_text


# ── Sidebar ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("# 📊 Cavalletto")
    st.markdown("*Administracion*")
    st.divider()

    ss.user_name = st.text_input("Tu nombre", value=ss.user_name,
                                  placeholder="Ej: Maru, Ale, Pepe...")

    st.divider()
    st.markdown("#### 🔑 API Key Anthropic")
    api_input = st.text_input(
        "API key", type="password", value=ss.api_key,
        placeholder="sk-ant-...",
        help="https://console.anthropic.com/settings/keys",
        label_visibility="collapsed",
    )
    if api_input != ss.api_key:
        ss.api_key = api_input
        ss.api_key_valid = False

    if ss.api_key and not ss.api_key_valid:
        if st.button("Verificar API key", type="primary", use_container_width=True):
            with st.spinner("Verificando..."):
                try:
                    c = anthropic.Anthropic(api_key=ss.api_key)
                    c.messages.create(model=MODEL, max_tokens=10,
                                      messages=[{"role": "user", "content": "hola"}])
                    ss.api_key_valid = True
                    st.rerun()
                except anthropic.AuthenticationError:
                    st.error("❌ API key invalida")
                except Exception as e:
                    st.error(f"❌ {e}")

    if ss.api_key_valid:
        st.success("✅ Conectado")

    # Estado de la DB (visible siempre para debug)
    info = db_info()
    if not db_exists():
        st.error(f"⚠️ DB no encontrada en:\n`{info.get('path', '?')}`")
    else:
        st.caption(f"🗄️ DB: **{info['backend']}**")
        if info["backend"] == "cloud":
            st.caption(f"☁️ {info.get('url', '')}")
        else:
            st.caption(f"💾 {info.get('path', '')}")

    st.divider()

    # KPIs del mes
    mes = current_period()
    st.markdown(f"### 📈 KPIs — {mes}")

    if db_exists():
        cob = kpi_cobranza_mes(mes)
        gas = kpi_gastos_mes(mes)
        util = kpi_utilidad_mes(mes)
        mor = kpi_morosos_count()
        stk = kpi_stock_bajo()
        alertas = kpi_alertas(mes)

        st.markdown(
            f'<div class="kpi-card"><div class="kpi-label">Cobranza</div>'
            f'<div class="kpi-value">{cob["porcentaje"]}%</div>'
            f'<div class="kpi-sub">{fmt_money(cob["cobrado"])} de {fmt_money(cob["facturado"])}</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-label">Gastos del mes</div>'
            f'<div class="kpi-value">{fmt_money(gas["gastado"])}</div>'
            f'<div class="kpi-sub">Presupuesto: {fmt_money(gas["presupuesto_total"])}</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-label">Utilidad</div>'
            f'<div class="kpi-value">{fmt_money(util["utilidad"])}</div>'
            f'<div class="kpi-sub">Ingresos {fmt_money(util["ingresos"])} − Egresos {fmt_money(util["egresos"])}</div></div>',
            unsafe_allow_html=True,
        )
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">Morosos</div>'
                f'<div class="kpi-value">{mor["count"]}</div></div>',
                unsafe_allow_html=True,
            )
        with col_b:
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">Stock bajo</div>'
                f'<div class="kpi-value">{stk["count"]}</div></div>',
                unsafe_allow_html=True,
            )

        alertas_list = alertas.get("alertas", []) if isinstance(alertas, dict) else alertas
        if alertas_list:
            st.markdown("#### ⚠️ Alertas")
            for a in alertas_list[:5]:
                st.markdown(f'<div class="alert-warn">• {a}</div>',
                            unsafe_allow_html=True)

    st.divider()

    # ── Archivos: fotos inventario + facturas ──
    st.markdown("### 📎 Archivos")

    with st.expander("📸 Foto de item", expanded=False):
        foto = st.file_uploader("Subir foto", type=["jpg", "jpeg", "png"],
                                key="foto_uploader",
                                label_visibility="collapsed")
        item_id_foto = st.number_input("ID del item", min_value=1, step=1,
                                        key="foto_item_id", value=1)
        if foto and st.button("Guardar foto", key="btn_foto", use_container_width=True):
            if db_exists():
                r = actualizar_foto_item(
                    item_id=int(item_id_foto),
                    foto_bytes=foto.getvalue(),
                    foto_mime=foto.type or "image/jpeg",
                )
                if r.get("error"):
                    st.error(r["error"])
                else:
                    st.success(r.get("mensaje", "Foto guardada"))
            else:
                st.error("DB no disponible")

    with st.expander("📄 Subir factura (IA)", expanded=False):
        fact = st.file_uploader("PDF o imagen",
                                type=["pdf", "jpg", "jpeg", "png"],
                                key="fact_uploader",
                                label_visibility="collapsed")
        if fact and st.button("Subir y analizar", key="btn_fact",
                               type="primary", use_container_width=True):
            if not ss.api_key_valid:
                st.error("Primero verifica tu API key")
            elif not db_exists():
                st.error("DB no disponible")
            else:
                r = subir_factura(
                    nombre_archivo=fact.name,
                    mime=fact.type or "application/pdf",
                    archivo_bytes=fact.getvalue(),
                )
                if r.get("error"):
                    st.error(r["error"])
                else:
                    ss.pending_factura = {
                        "factura_id": r["factura_id"],
                        "bytes": fact.getvalue(),
                        "mime": fact.type or "application/pdf",
                        "nombre": fact.name,
                    }
                    st.success(f"Factura subida (ID {r['factura_id']}). "
                               "Escribe 'procesala' en el chat o dame contexto.")

    # Fotos de items recientemente solicitadas
    if ss.fotos_to_show:
        st.markdown("#### 🖼️ Fotos")
        for i, f in enumerate(list(ss.fotos_to_show)):
            res = _foto_bytes(f["item_id"])
            if not res:
                continue
            data, mime = res
            st.caption(f"Item {f['item_id']} — {f['nombre']}")
            try:
                st.image(data, use_container_width=True)
            except Exception:
                st.download_button(
                    f"Descargar foto item {f['item_id']}",
                    data=data,
                    file_name=f"foto_item_{f['item_id']}.jpg",
                    mime=mime,
                    key=f"foto_dl_{i}",
                    use_container_width=True,
                )

    st.divider()

    # Descargas pendientes
    if ss.pending_downloads:
        st.markdown("### 📥 Descargas")
        for i, d in enumerate(list(ss.pending_downloads)):
            if d.get("error"):
                st.caption(f"⚠️ {d['error']}")
                continue
            st.download_button(
                d["label"], data=d["data"], file_name=d["filename"],
                mime=d["mime"], key=f"dl_{i}_{d['filename']}",
                use_container_width=True,
            )

    st.divider()
    if st.button("🔄 Nueva conversacion", use_container_width=True):
        ss.messages = []
        ss.pending_downloads = []
        ss.fotos_to_show = []
        ss.pending_factura = None
        st.rerun()

    st.caption("Agente Administrativo Cavalletto v1.0")


# ── Area principal ──────────────────────────────────────────────────
st.markdown(
    '<div class="main-header">'
    '<h1>📊 Agente Administrativo Cavalletto</h1>'
    '<p>Cobranza · Gastos · Inventario · Nomina · Reportes</p>'
    '</div>',
    unsafe_allow_html=True,
)

for msg in ss.messages:
    avatar = "📊" if msg["role"] == "assistant" else None
    with st.chat_message(msg["role"], avatar=avatar):
        st.markdown(msg["content"])

if not ss.messages:
    with st.chat_message("assistant", avatar="📊"):
        greet = (
            f"¡Hola{', ' + ss.user_name if ss.user_name else ''}! "
            "Soy el **Agente Administrativo de Cavalletto**. "
            "Puedo ayudarte con:\n\n"
            "- **Cobranza** — familias, alumnos, cargos, pagos, morosos\n"
            "- **Gastos** — registrar compras, presupuesto por categoria\n"
            "- **Inventario** — materiales y consumibles, stock bajo\n"
            "- **Nomina** — empleados y pagos\n"
            "- **Presupuesto** — resumen financiero, alertas, proyeccion\n"
            "- **Reportes** — mensual, contador, recibos PDF\n"
            "- **Facturas con IA** — sube un PDF desde el panel y la leo automaticamente\n\n"
            "Cuentame que necesitas."
        )
        st.markdown(greet)

placeholder_text = ("Procesa la factura" if ss.pending_factura
                    else "Escribe tu solicitud...")
prompt = st.chat_input(
    placeholder_text,
    disabled=not (ss.api_key_valid and db_exists()),
)

# Auto-enviar si hay factura pendiente y el usuario solo le dio enter vacio
if prompt is None and ss.pending_factura and ss.api_key_valid:
    pass  # espera al usuario — no auto-enviamos

if prompt:
    factura_for_this_turn = ss.pending_factura
    ss.pending_factura = None  # consumir antes de llamar para evitar reenvio
    with st.chat_message("user"):
        if factura_for_this_turn:
            st.caption(f"📄 Factura adjunta: {factura_for_this_turn['nombre']}")
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="📊"):
        try:
            client = anthropic.Anthropic(api_key=ss.api_key)
            with st.spinner("Pensando..."):
                text = _chat_turn(client, prompt, factura=factura_for_this_turn)
            st.markdown(text)
            st.rerun()
        except anthropic.AuthenticationError:
            st.error("❌ API key invalida")
            ss.api_key_valid = False
        except anthropic.RateLimitError:
            st.error("⏳ Rate limit alcanzado")
        except Exception as e:
            st.error(f"❌ Error: {e}")
