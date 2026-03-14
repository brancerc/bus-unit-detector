"""
CISA - Modulo de alertas y notificaciones
Flujo de validacion:
  1. Deteccion → enviar_a_validador() con botones OK/NO
  2. Validador presiona boton
  3. OK → reenviar al grupo MONITOR JETSON + Swagger/JSON
  4. NO → guardar en /revisar, descartar
"""

import json
import os
import shutil
import requests
import threading
import time
from config import TG_TOKEN, TG_CHAT_ID, TG_VALIDADOR_ID, REVISAR_DIR

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# Detecciones pendientes de validacion: {msg_id: {datos}}
_pendientes = {}
_pendientes_lock = threading.Lock()

# Offset para polling de callbacks
_update_offset = 0


# ==============================================================================
# FUNCIONES BASE
# ==============================================================================

def _enviar_foto(chat_id, frame_path, caption, reply_markup=None):
    """Envia una foto con caption a un chat especifico."""
    try:
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            data["reply_markup"] = reply_markup

        with open(frame_path, 'rb') as foto:
            resp = requests.post(
                f"{TG_API}/sendPhoto",
                data=data,
                files={"photo": foto},
                timeout=15
            )
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
    """Envia un mensaje de texto."""
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            data={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"},
            timeout=15
        )
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def _responder_callback(callback_id, texto):
    """Responde a un callback query (quita el relojito del boton)."""
    try:
        requests.post(
            f"{TG_API}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": texto},
            timeout=10
        )
    except Exception as e:
        print(f"[TELEGRAM ERROR callback] {e}")


# ==============================================================================
# VALIDACION: Enviar al validador con botones
# ==============================================================================

def enviar_a_validador(frame_path, numero, confianza, ts, estado):
    """
    Envia la deteccion al validador con botones inline.
    El validador decide si es correcta o no.
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
                "numero": numero,
                "confianza": confianza,
                "ts": ts,
                "estado": estado,
                "frame_path": frame_path,
            }
        print(f"[VALIDADOR] Enviado {numero} para validacion (msg_id={msg_id})")
    else:
        # Fallback: enviar directo al grupo
        print(f"[VALIDADOR] Fallo envio, fallback a grupo directo")
        alerta_unidad_detectada(frame_path, numero, confianza, ts)


# ==============================================================================
# CALLBACK LISTENER: Escucha respuestas del validador
# ==============================================================================

def _procesar_callbacks():
    """Hilo que escucha las respuestas del validador via long polling."""
    global _update_offset

    while True:
        try:
            resp = requests.get(
                f"{TG_API}/getUpdates",
                params={
                    "offset": _update_offset,
                    "timeout": 30,
                    "allowed_updates": '["callback_query"]'
                },
                timeout=35
            )
            updates = resp.json().get("result", [])

            for update in updates:
                _update_offset = update["update_id"] + 1

                cb = update.get("callback_query")
                if not cb:
                    continue

                cb_id = cb["id"]
                cb_data = cb.get("data", "")
                msg_id = cb.get("message", {}).get("message_id")
                user_id = str(cb.get("from", {}).get("id", ""))

                # Solo aceptar del validador autorizado
                if user_id != TG_VALIDADOR_ID:
                    _responder_callback(cb_id, "No autorizado")
                    continue

                with _pendientes_lock:
                    datos = _pendientes.pop(msg_id, None)

                if not datos:
                    _responder_callback(cb_id, "Ya procesada")
                    continue

                if cb_data.startswith("ok:"):
                    _responder_callback(cb_id, f"\u2713 {datos['numero']} verificado")
                    _on_correcto(datos)

                elif cb_data.startswith("no:"):
                    _responder_callback(cb_id, f"\u2717 {datos['numero']} descartado")
                    _on_incorrecto(datos)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[CALLBACK ERROR] {e}")
            time.sleep(5)


def _on_correcto(datos):
    """Validador confirmo: enviar al grupo + swagger."""
    numero = datos["numero"]
    confianza = datos["confianza"]
    ts = datos["ts"]
    frame_path = datos["frame_path"]

    caption = (
        f"\u2705 *Unidad VERIFICADA*\n"
        f"\U0001f522 Numero: `{numero}`\n"
        f"\U0001f4ca Confianza: `{round(confianza * 100)}%`\n"
        f"\U0001f550 Hora: `{ts}`\n"
        f"\U0001f464 _Validado manualmente_"
    )
    _enviar_foto(TG_CHAT_ID, frame_path, caption)
    print(f"[VERIFICADO] {numero} enviado al grupo")

    # Swagger/JSON (descomentar cuando tengas endpoint)
    # enviar_a_swagger(datos)
    print(f"[SWAGGER] {numero} listo para enviar (endpoint pendiente)")


def _on_incorrecto(datos):
    """Validador rechazo: mover a /revisar."""
    numero = datos["numero"]
    frame_path = datos["frame_path"]

    if os.path.exists(frame_path):
        dest = os.path.join(REVISAR_DIR, os.path.basename(frame_path))
        shutil.copy2(frame_path, dest)
        print(f"[RECHAZADO] {numero} movido a revisar/")
    else:
        print(f"[RECHAZADO] {numero} frame no encontrado")

    _enviar_mensaje(TG_VALIDADOR_ID,
        f"\U0001f5d1 `{numero}` descartado y guardado en `revisar/`")


# ==============================================================================
# SWAGGER / JSON API (placeholder)
# ==============================================================================

def enviar_a_swagger(datos):
    """Descomentar y configurar cuando tengas el endpoint."""
    # payload = {
    #     "no_economico": datos["numero"],
    #     "confianza": datos["confianza"],
    #     "hora_paso": datos["ts"],
    #     "id_puerta": 1,
    # }
    # try:
    #     resp = requests.post("https://tu-api.com/api/eventoPaso",
    #         json=payload, headers={"Authorization": "Bearer TOKEN"}, timeout=15)
    #     print(f"[SWAGGER] {datos['numero']} -> {resp.status_code}")
    # except Exception as e:
    #     print(f"[SWAGGER ERROR] {e}")
    pass


# ==============================================================================
# ALERTAS DIRECTAS (sin validacion)
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