"""
CISA - Modulo de alertas y notificaciones
Flujo de validacion:
  1. Deteccion → enviar_a_validador() con botones OK/NO
  2. Validador presiona boton
  3. OK → db_insert SQLite + envia foto al grupo + borra frame del disco
  4. NO → mueve frame limpio a /revisar + borra de FRAMES_DIR + NO toca SQLite
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

_pendientes = {}
_pendientes_lock = threading.Lock()
_update_offset = 0


# ==============================================================================
# SQLITE — insert solo cuando el validador aprueba
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
        print(f"[DB] Insertado: {no_economico} | duracion: {duracion}s")
    except Exception as e:
        print(f"[DB ERROR] {e}")


# ==============================================================================
# FUNCIONES BASE
# ==============================================================================

def _enviar_foto(chat_id, frame_path, caption, reply_markup=None):
    try:
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if reply_markup:
            data["reply_markup"] = reply_markup
        with open(frame_path, 'rb') as foto:
            resp = requests.post(
                f"{TG_API}/sendPhoto",
                data=data, files={"photo": foto}, timeout=15)
        result = resp.json()
        if result.get("ok"):
            return result["result"]["message_id"]
        else:
            print(f"[TELEGRAM ERROR] {result.get('description', 'unknown')}")
            return None
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return None


def _enviar_mensaje(chat_id, texto):
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            data={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"},
            timeout=15)
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def _responder_callback(callback_id, texto):
    try:
        requests.post(
            f"{TG_API}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": texto},
            timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR callback] {e}")


def _borrar_frame(frame_path):
    """Borra el frame temporal del disco de forma segura."""
    try:
        if frame_path and os.path.exists(frame_path):
            os.remove(frame_path)
            print(f"[CLEANUP] Frame borrado: {os.path.basename(frame_path)}")
    except Exception as e:
        print(f"[CLEANUP ERROR] {e}")


# ==============================================================================
# VALIDACION
# ==============================================================================

def enviar_a_validador(frame_path, numero, confianza, ts, estado, no_detectado=None):
    """
    Envia la deteccion al validador con botones inline.
    Retorna msg_id para que pipe_stream pueda actualizar el pendiente
    con el numero ganador (multi-lectura) y la duracion cuando el bus salga.
    """
    caption = (
        f"\U0001f50d *Validar deteccion*\n"
        f"\U0001f522 Numero: `{numero}`\n"
        f"\U0001f4ca Confianza: `{round(confianza * 100)}%`\n"
        f"\U0001f4cb Estado: `{estado}`\n"
        f"\U0001f550 Hora: `{ts}`\n\n"
        f"_Presiona un boton para validar_"
    )
    keyboard = json.dumps({
        "inline_keyboard": [[
            {"text": "\u2713 Correcto", "callback_data": f"ok:{numero}"},
            {"text": "\u2717 Incorrecto", "callback_data": f"no:{numero}"}
        ]]
    })
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
        print(f"[VALIDADOR] Enviado {numero} para validacion (msg_id={msg_id})")
        return msg_id
    else:
        print(f"[VALIDADOR] Fallo envio, fallback a grupo directo")
        alerta_unidad_detectada(frame_path, numero, confianza, ts)
        return None


def actualizar_pendiente(msg_id, numero_ganador, confianza_ganadora, estado_ganador, duracion):
    """
    Llamado desde pipe_stream._cerrar_track cuando el bus sale de camara.
    Actualiza numero ganador (multi-lectura) y duracion real.
    Si el validador ya respondio, no hace nada.
    """
    if msg_id is None:
        return
    with _pendientes_lock:
        if msg_id in _pendientes:
            _pendientes[msg_id]["numero"]    = numero_ganador
            _pendientes[msg_id]["confianza"] = confianza_ganadora
            _pendientes[msg_id]["estado"]    = estado_ganador
            _pendientes[msg_id]["duracion"]  = duracion
            print(f"[VALIDADOR] Pendiente {msg_id} actualizado → {numero_ganador} | dur: {duracion}s")
        else:
            print(f"[VALIDADOR] Pendiente {msg_id} ya procesado antes de que el bus saliera")


# ==============================================================================
# CALLBACK LISTENER
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
            updates = resp.json().get("result", [])
            for update in updates:
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
                    # Pendiente no encontrado — servicio reiniciado entre deteccion y validacion
                    _responder_callback(cb_id, "\u26a0\ufe0f Deteccion expirada (servicio reiniciado)")
                    continue

                if cb_data.startswith("ok:"):
                    _responder_callback(cb_id, f"\u2713 {datos['numero']} verificado")
                    _on_correcto(datos)
                elif cb_data.startswith("no:"):
                    _responder_callback(cb_id, f"\u2717 {datos['numero']} descartado y guardado en revisar/")
                    _on_incorrecto(datos)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[CALLBACK ERROR] {e}")
            time.sleep(5)


def _on_correcto(datos):
    """
    Validador confirmo:
    1. Inserta en SQLite
    2. Envia foto limpia al grupo MONITOR JETSON
    3. Borra frame del disco (ya esta en Telegram, no hace falta guardarlo)
    """
    numero     = datos["numero"]
    confianza  = datos["confianza"]
    ts         = datos["ts"]
    estado     = datos["estado"]
    frame_path = datos["frame_path"]
    no_det     = datos["no_detectado"]
    duracion   = datos.get("duracion", 0)

    # 1. Guardar en SQLite — unico lugar donde se inserta
    _db_insert(no_det, numero, confianza, None, estado, duracion)

    # 2. Enviar al grupo MONITOR JETSON
    caption = (
        f"\u2705 *Unidad VERIFICADA*\n"
        f"\U0001f522 Numero: `{numero}`\n"
        f"\U0001f4ca Confianza: `{round(confianza * 100)}%`\n"
        f"\U0001f550 Hora: `{ts}`\n"
        f"\u23f1 Duracion: `{duracion}s`\n"
        f"\U0001f464 _Validado manualmente_"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[VERIFICADO] {numero} guardado en SQLite y enviado al grupo")

    # 3. Borrar frame del disco — ya no se necesita
    _borrar_frame(frame_path)

    # enviar_a_swagger(datos)
    print(f"[SWAGGER] {numero} listo para enviar (endpoint pendiente)")


def _on_incorrecto(datos):
    """
    Validador rechazo:
    1. Mueve frame limpio a /revisar
    2. Borra frame de FRAMES_DIR
    3. NO toca SQLite
    """
    numero     = datos["numero"]
    frame_path = datos["frame_path"]

    if frame_path and os.path.exists(frame_path):
        dest = os.path.join(REVISAR_DIR, os.path.basename(frame_path))
        shutil.copy2(frame_path, dest)
        print(f"[RECHAZADO] {numero} movido a revisar/ — no guardado en SQLite")
        # Borrar el original de FRAMES_DIR
        _borrar_frame(frame_path)
    else:
        print(f"[RECHAZADO] {numero} frame no encontrado")

    _enviar_mensaje(TG_VALIDADOR_ID,
        f"\U0001f5d1 `{numero}` descartado y guardado en `revisar/`")


# ==============================================================================
# SWAGGER placeholder
# ==============================================================================

def enviar_a_swagger(datos):
    pass


# ==============================================================================
# ALERTAS DIRECTAS
# ==============================================================================

def alerta_unidad_detectada(frame_path, numero, confianza, ts):
    """Fallback: envia directo al grupo sin validacion."""
    caption = (
        f"\U0001f68c *Unidad detectada*\n"
        f"\U0001f522 Numero: `{numero}`\n"
        f"\U0001f4ca Confianza: `{round(confianza * 100)}%`\n"
        f"\U0001f550 Hora: `{ts}`"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[TELEGRAM] Enviado: {numero}")


def alerta_unidad_desconocida(frame_path, numero, confianza, ts):
    """Numero OCR desconocido — va directo al grupo."""
    caption = (
        f"\U0001f6ab *NUMERO DESCONOCIDO*\n"
        f"\U0001f522 OCR leyo: `{numero}`\n"
        f"\u274c No existe en la base de datos\n"
        f"\U0001f4ca Confianza YOLO: `{round(confianza * 100)}%`\n"
        f"\U0001f550 Hora: `{ts}`\n"
        f"\U0001f4c1 Guardado en: `unidades_desconocidas/`\n"
        f"_\u26a0\ufe0f Revisar frame_"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[TELEGRAM] Desconocida: {numero}")


# ==============================================================================
# ALERTAS DE SISTEMA
# ==============================================================================

def alerta_servicio_caido(servicio, detalles=""):
    texto = f"\U0001f534 *SERVICIO CAIDO*\n\u2699\ufe0f `{servicio}`\n"
    if detalles:
        texto += f"{detalles}\n"
    _enviar_mensaje(TG_CHAT_ID, texto)

def alerta_pipeline_error():
    _enviar_mensaje(TG_CHAT_ID,
        "\U0001f534 *PIPELINE CAIDO*\n_Reintentando conexion..._")

def alerta_postgres_desconectada():
    _enviar_mensaje(TG_CHAT_ID,
        "\U0001f534 *POSTGRESQL DESCONECTADA*\n_Detecciones descartadas_")


# ==============================================================================
# INICIAR LISTENER
# ==============================================================================
_callback_thread = threading.Thread(target=_procesar_callbacks, daemon=True)
_callback_thread.start()
print("[VALIDADOR] Listener de callbacks iniciado")