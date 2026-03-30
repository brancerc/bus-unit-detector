"""
CISA - Módulo de alertas y notificaciones
Flujo de validación:
  1. Detección → enviar_a_validador() con botones ✓/✗
  2. Validador presiona botón (frame permanece en el chat de Telegram hasta entonces)
  3. ✓ Correcto  → INSERT SQLite + foto al grupo MONITOR JETSON + borra frame del disco
  4. ✗ Incorrecto → mueve frame a /revisar/. NO guarda en SQLite.
"""

import json
import os
import shutil
import sqlite3
import requests
import threading
import time
from datetime import datetime
from config import (
    TG_TOKEN, TG_CHAT_ID, TG_VALIDADOR_ID, REVISAR_DIR,
    DB_PATH, ID_PUERTA
)

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

_pendientes      = {}
_pendientes_lock = threading.Lock()
_update_offset   = 0

# Callback opcional registrado por pipe_stream para actualizar tiles en tiempo real.
# Firma: fn(msg_id: int, nuevo_estado: str) → None
_on_validated_callback = None


def set_validation_callback(fn):
    """pipe_stream_v2 llama esto al arrancar para recibir confirmaciones/rechazos."""
    global _on_validated_callback
    _on_validated_callback = fn


# ==============================================================================
# SQLite — solo cuando el validador aprueba
# ==============================================================================

def _db_insert(no_detectado, no_economico, confianza, captura_url=None,
               estado="VERIFICADO", duracion=0):
    now = datetime.now()
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            """INSERT INTO evento_paso
               (no_detectado, no_economico, direccion, hora_paso, id_puerta,
                hora_registro, captura_url, estado, confianza, duracion_camara)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (no_detectado, no_economico, "entrada",
             now.strftime("%Y-%m-%d %H:%M:%S"), ID_PUERTA,
             now.strftime("%Y-%m-%d %H:%M:%S"), captura_url,
             estado, round(confianza, 3), round(duracion, 1)))
        con.commit(); con.close()
        print(f"[DB] Insertado: {no_economico} | dur: {duracion}s")
    except Exception as e:
        print(f"[DB ERROR] {e}")


# ==============================================================================
# Funciones base Telegram
# ==============================================================================

def _enviar_foto(chat_id, frame_path, caption, reply_markup=None):
    try:
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        with open(frame_path, 'rb') as foto:
            resp = requests.post(f"{TG_API}/sendPhoto",
                data=data, files={"photo": foto}, timeout=15)
        result = resp.json()
        if result.get("ok"):
            return result["result"]["message_id"]
        print(f"[TELEGRAM ERROR] {result.get('description', 'unknown')}")
        return None
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return None


def _enviar_mensaje(chat_id, texto):
    try:
        requests.post(f"{TG_API}/sendMessage",
            data={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"},
            timeout=15)
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def _responder_callback(callback_id, texto):
    try:
        requests.post(f"{TG_API}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": texto},
            timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR callback] {e}")


def _borrar_frame(frame_path):
    try:
        if frame_path and os.path.exists(frame_path):
            os.remove(frame_path)
            print(f"[CLEANUP] Frame borrado: {os.path.basename(frame_path)}")
    except Exception as e:
        print(f"[CLEANUP ERROR] {e}")


def _mover_a_revisar(frame_path):
    """Mueve el frame rechazado a /revisar/ para revisión posterior."""
    try:
        if frame_path and os.path.exists(frame_path):
            dest = os.path.join(REVISAR_DIR, os.path.basename(frame_path))
            shutil.move(frame_path, dest)
            print(f"[REVISAR] Frame movido: {os.path.basename(frame_path)}")
    except Exception as e:
        print(f"[REVISAR ERROR] {e}")


# ==============================================================================
# Validación
# ==============================================================================

def enviar_a_validador(frame_path, numero, confianza, ts, estado, no_detectado=None):
    caption = (
        f"\U0001f50d *Validar detección*\n"
        f"\U0001f522 Número: `{numero}`\n"
        f"\U0001f4ca Confianza: `{round(confianza * 100)}%`\n"
        f"\U0001f4cb Estado: `{estado}`\n"
        f"\U0001f550 Hora: `{ts}`\n\n"
        f"_Presiona un botón para validar_"
    )
    keyboard = json.dumps({"inline_keyboard": [[
        {"text": "✓ Correcto",   "callback_data": f"ok:{numero}"},
        {"text": "✗ Incorrecto", "callback_data": f"no:{numero}"},
    ]]})
    msg_id = _enviar_foto(TG_VALIDADOR_ID, frame_path, caption, reply_markup=keyboard)
    if msg_id:
        with _pendientes_lock:
            _pendientes[msg_id] = {
                "numero":       numero,
                "no_detectado": no_detectado or numero,
                "confianza":    confianza,
                "ts":           ts,
                "estado":       estado,
                "frame_path":   frame_path,
                "duracion":     0,
            }
        print(f"[VALIDADOR] Enviado {numero} (msg_id={msg_id}) — esperando respuesta")
        return msg_id
    # Fallback: enviar directo al grupo sin validación
    print(f"[VALIDADOR] Fallo envío → fallback al grupo")
    alerta_unidad_detectada(frame_path, numero, confianza, ts)
    return None


def actualizar_pendiente(msg_id, numero_ganador, confianza_ganadora, estado_ganador, duracion):
    """Actualiza el pendiente con el número ganador por multi-lectura al salir el bus."""
    if msg_id is None:
        return
    with _pendientes_lock:
        if msg_id in _pendientes:
            _pendientes[msg_id].update({
                "numero":    numero_ganador,
                "confianza": confianza_ganadora,
                "estado":    estado_ganador,
                "duracion":  duracion,
            })
            print(f"[VALIDADOR] Pendiente {msg_id} → {numero_ganador} | dur: {duracion}s")
        else:
            print(f"[VALIDADOR] Pendiente {msg_id} ya procesado antes de que saliera el bus")


# ==============================================================================
# Listener de callbacks
# ==============================================================================

def _procesar_callbacks():
    global _update_offset
    while True:
        try:
            resp = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": _update_offset, "timeout": 30,
                        "allowed_updates": '["callback_query"]'},
                timeout=35)
            for update in resp.json().get("result", []):
                _update_offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue

                cb_id   = cb["id"]
                cb_data = cb.get("data", "")
                msg_id  = cb.get("message", {}).get("message_id")
                user_id = str(cb.get("from", {}).get("id", ""))

                if user_id != TG_VALIDADOR_ID:
                    _responder_callback(cb_id, "No autorizado")
                    continue

                with _pendientes_lock:
                    datos = _pendientes.pop(msg_id, None)

                if not datos:
                    _responder_callback(cb_id, "⚠️ Detección expirada (servicio reiniciado)")
                    continue

                if cb_data.startswith("ok:"):
                    _responder_callback(cb_id, f"✓ {datos['numero']} verificado")
                    _on_correcto(msg_id, datos)
                elif cb_data.startswith("no:"):
                    _responder_callback(cb_id, f"✗ {datos['numero']} descartado")
                    _on_incorrecto(msg_id, datos)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[CALLBACK ERROR] {e}")
            time.sleep(5)


def _on_correcto(msg_id, datos):
    """
    Validador confirmó:
      1. INSERT en SQLite
      2. Foto al grupo MONITOR JETSON
      3. Borra frame del disco
      4. Notifica callback (actualiza tile en dashboard)
    """
    numero     = datos["numero"]
    confianza  = datos["confianza"]
    ts         = datos["ts"]
    estado     = datos["estado"]
    frame_path = datos["frame_path"]
    no_det     = datos["no_detectado"]
    duracion   = datos.get("duracion", 0)

    _db_insert(no_det, numero, confianza, None, estado, duracion)

    caption = (
        f"✅ *Unidad VERIFICADA*\n"
        f"🔢 Número: `{numero}`\n"
        f"📊 Confianza: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`\n"
        f"⏱ Duración: `{duracion}s`\n"
        f"👤 _Validado manualmente_"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[VERIFICADO] {numero} → SQLite + grupo")

    _borrar_frame(frame_path)

    if _on_validated_callback:
        try: _on_validated_callback(msg_id, "VERIFICADO")
        except Exception as e: print(f"[CALLBACK ERROR] {e}")

    # enviar_a_swagger(datos)   ← activar cuando llegue el endpoint


def _on_incorrecto(msg_id, datos):
    """
    Validador rechazó:
      1. Mueve frame a /revisar/ (para análisis posterior)
      2. NO guarda en SQLite
      3. Notifica callback (actualiza tile en dashboard)
    """
    numero     = datos["numero"]
    frame_path = datos["frame_path"]

    _mover_a_revisar(frame_path)
    print(f"[RECHAZADO] {numero} → frame movido a /revisar/")

    _enviar_mensaje(TG_VALIDADOR_ID, f"🗑 `{numero}` descartado → guardado en `/revisar/`")

    if _on_validated_callback:
        try: _on_validated_callback(msg_id, "RECHAZADO")
        except Exception as e: print(f"[CALLBACK ERROR] {e}")


# ==============================================================================
# Swagger placeholder
# ==============================================================================

def enviar_a_swagger(datos):
    pass


# ==============================================================================
# Alertas directas
# ==============================================================================

def alerta_unidad_detectada(frame_path, numero, confianza, ts):
    caption = (
        f"🚌 *Unidad detectada*\n"
        f"🔢 Número: `{numero}`\n"
        f"📊 Confianza: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[TELEGRAM] Detectada: {numero}")


def alerta_unidad_desconocida(frame_path, numero, confianza, ts):
    caption = (
        f"🚫 *NÚMERO DESCONOCIDO*\n"
        f"🔢 OCR leyó: `{numero}`\n"
        f"❌ No existe en la base de datos\n"
        f"📊 Confianza YOLO: `{round(confianza * 100)}%`\n"
        f"🕐 Hora: `{ts}`\n"
        f"📁 Guardado en: `unidades_desconocidas/`\n"
        f"_⚠️ Revisar frame_"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[TELEGRAM] Desconocida: {numero}")


# ==============================================================================
# Alertas de sistema
# ==============================================================================

def alerta_servicio_caido(servicio, detalles=""):
    texto = f"🔴 *SERVICIO CAÍDO*\n⚙️ `{servicio}`\n"
    if detalles: texto += f"{detalles}\n"
    _enviar_mensaje(TG_CHAT_ID, texto)

def alerta_pipeline_error():
    _enviar_mensaje(TG_CHAT_ID, "🔴 *PIPELINE CAÍDO*\n_Reintentando conexión..._")

def alerta_postgres_desconectada():
    _enviar_mensaje(TG_CHAT_ID, "🔴 *POSTGRESQL DESCONECTADA*\n_Detecciones descartadas_")


# ==============================================================================
# Arranque
# ==============================================================================

_callback_thread = threading.Thread(target=_procesar_callbacks, daemon=True)
_callback_thread.start()
print("[VALIDADOR] Listener de callbacks iniciado")