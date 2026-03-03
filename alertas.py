"""
CISA - Módulo de alertas y notificaciones
Centraliza todo el envío de mensajes a Telegram.
Agregar aquí futuros canales: WhatsApp, email, Slack, etc.
"""

import requests
from config import TG_TOKEN, TG_CHAT_ID

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


# ══════════════════════════════════════════════════════════════════════════════
# FUNCIONES BASE
# ══════════════════════════════════════════════════════════════════════════════

def _enviar_foto(frame_path, caption):
    """Envía una foto con caption a Telegram."""
    try:
        with open(frame_path, 'rb') as foto:
            requests.post(
                f"{TG_API}/sendPhoto",
                data={
                    "chat_id": TG_CHAT_ID,
                    "caption": caption,
                    "parse_mode": "Markdown"
                },
                files={"photo": foto},
                timeout=15
            )
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def _enviar_mensaje(texto):
    """Envía un mensaje de texto a Telegram."""
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            data={
                "chat_id": TG_CHAT_ID,
                "text": texto,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def _enviar_documento(filepath, caption=""):
    """Envía un archivo como documento a Telegram."""
    try:
        with open(filepath, 'rb') as doc:
            requests.post(
                f"{TG_API}/sendDocument",
                data={
                    "chat_id": TG_CHAT_ID,
                    "caption": caption
                },
                files={"document": doc},
                timeout=30
            )
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ALERTAS DE DETECCIÓN (usadas por pipe_stream.py)
# ══════════════════════════════════════════════════════════════════════════════

def alerta_unidad_detectada(frame_path, numero, confianza, ts):
    """Unidad válida detectada y verificada contra la BD."""
    caption = (
        f"🚌 *Unidad detectada*\n"
        f"🔢 Número: `{numero}`\n"
        f"📊 Confianza: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`"
    )
    if _enviar_foto(frame_path, caption):
        print(f"[TELEGRAM] Enviado: {numero} (OK)")


def alerta_unidad_sospechosa(frame_path, numero, confianza, ts):
    """Unidad detectada pero no está en la lista de registradas."""
    caption = (
        f"⚠️ *UNIDAD SOSPECHOSA*\n"
        f"🔢 OCR leyó: `{numero}`\n"
        f"❓ No está en lista de unidades\n"
        f"📊 Confianza: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`\n"
        f"_Revisar frame para mejorar detección_"
    )
    if _enviar_foto(frame_path, caption):
        print(f"[TELEGRAM] Enviado: {numero} (SOSPECHOSO)")


def alerta_unidad_desconocida(frame_path, numero, confianza, ts):
    """Número OCR que no coincide con ninguna unidad en la BD."""
    caption = (
        f"🚫 *NÚMERO DESCONOCIDO*\n"
        f"🔢 OCR leyó: `{numero}`\n"
        f"❌ No existe en la base de datos\n"
        f"📊 Confianza YOLO: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`\n"
        f"📁 Guardado en: `unidades_desconocidas/`\n"
        f"_⚠️ Revisar frame — número desconocido_"
    )
    if _enviar_foto(frame_path, caption):
        print(f"[TELEGRAM] Alerta desconocida enviada: {numero}")


# ══════════════════════════════════════════════════════════════════════════════
# ALERTAS DE SISTEMA (usadas por el script de diagnóstico o monitoreo futuro)
# ══════════════════════════════════════════════════════════════════════════════

def alerta_servicio_caido(servicio, detalles=""):
    """El servicio de monitoreo se detuvo."""
    texto = (
        f"🔴 *SERVICIO CAÍDO*\n"
        f"⚙️ Servicio: `{servicio}`\n"
        f"🕐 Detectado: ahora\n"
    )
    if detalles:
        texto += f"📋 Detalles: {detalles}\n"
    texto += "\n_Verificar estado del equipo_"
    _enviar_mensaje(texto)
    print(f"[TELEGRAM] Alerta servicio caído: {servicio}")


def alerta_camara_caida(ip, nombre="Cámara"):
    """Una cámara no responde al ping."""
    texto = (
        f"📷 *{nombre.upper()} SIN RESPUESTA*\n"
        f"🌐 IP: `{ip}`\n"
        f"🕐 Detectado: ahora\n"
        f"_Verificar conexión física y alimentación_"
    )
    _enviar_mensaje(texto)
    print(f"[TELEGRAM] Alerta cámara caída: {ip}")


def alerta_switch_caido(ip, nombre="Switch"):
    """Un switch no responde al ping."""
    texto = (
        f"🔴 *{nombre.upper()} SIN RESPUESTA*\n"
        f"🌐 IP: `{ip}`\n"
        f"🕐 Detectado: ahora\n"
        f"_Verificar alimentación y conexión del switch_"
    )
    _enviar_mensaje(texto)
    print(f"[TELEGRAM] Alerta switch caído: {ip}")


def alerta_pipeline_error():
    """El pipeline de video GStreamer falló."""
    texto = (
        f"🔴 *PIPELINE DE VIDEO CAÍDO*\n"
        f"📹 No se puede conectar a la cámara RTSP\n"
        f"🕐 Detectado: ahora\n"
        f"_Reintentando conexión automáticamente..._"
    )
    _enviar_mensaje(texto)
    print("[TELEGRAM] Alerta pipeline caído")


def alerta_postgres_desconectada():
    """PostgreSQL no responde."""
    texto = (
        f"🔴 *POSTGRESQL DESCONECTADA*\n"
        f"🗄️ No se pudo conectar a la base de datos\n"
        f"⚠️ Todas las detecciones serán DESCARTADAS\n"
        f"🕐 Detectado: ahora\n"
        f"_Verificar: sudo systemctl status postgresql_"
    )
    _enviar_mensaje(texto)
    print("[TELEGRAM] Alerta PostgreSQL desconectada")


def alerta_temperatura_alta(temp_c, zona=""):
    """Temperatura del equipo por encima del umbral."""
    texto = (
        f"🌡️ *TEMPERATURA ALTA*\n"
        f"🔥 {temp_c}°C"
    )
    if zona:
        texto += f" en `{zona}`"
    texto += (
        f"\n🕐 Detectado: ahora\n"
        f"_Verificar ventilación del equipo_"
    )
    _enviar_mensaje(texto)
    print(f"[TELEGRAM] Alerta temperatura: {temp_c}°C")


def enviar_reporte_diagnostico(filepath, resumen_alertas, sha256_hash):
    """Envía el reporte de diagnóstico completo con resumen."""
    texto = (
        f"🚨 *ALERTA CISA — DIAGNÓSTICO*\n\n"
        f"{resumen_alertas}\n"
        f"🔐 SHA256: `{sha256_hash[:16]}...`"
    )
    _enviar_mensaje(texto)
    _enviar_documento(filepath, "📎 Reporte diagnóstico CISA")
    print("[TELEGRAM] Reporte de diagnóstico enviado")