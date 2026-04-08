import cv2
import numpy as np
import time
import requests
from datetime import datetime
from config import TG_TOKEN, TG_VALIDADOR_ID
from alertas import enviar_a_validador, procesar_callback_validador

print("==========================================")
print("🤖 INICIANDO PRUEBA DE FLUJO DE VALIDACIÓN")
print("==========================================")

print("[1/3] Generando frame de prueba...")
img = np.zeros((300, 500, 3), dtype=np.uint8)
cv2.putText(img, "TEST VALIDACION", (30, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
cv2.imwrite("test_validador.jpg", img)

print("[2/3] Enviando alerta con botones a Amanda...")
ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
# Enviamos la unidad "9999" como prueba
enviar_a_validador("test_validador.jpg", "9999", 0.99, ts)

print("[3/3] 🎧 Escuchando respuesta de Amanda...")
print("      (Dile que presione '✅ Correcto' en su Telegram. Tienes 60 segundos.)")

last_update_id = 0
url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
timeout = time.time() + 60

while time.time() < timeout:
    try:
        res = requests.get(f"{url}?offset={last_update_id + 1}&timeout=5", timeout=10).json()
        for update in res.get("result", []):
            last_update_id = update["update_id"]
            if "callback_query" in update:
                cb = update["callback_query"]
                data = cb["data"]
                msg_id = cb["message"]["message_id"]
                chat_id = cb["message"]["chat"]["id"]
                file_id = cb["message"]["photo"][-1]["file_id"] if "photo" in cb["message"] else None
                
                print(f"\n👉 ¡Amanda presionó un botón! (Acción: {data})")
                procesar_callback_validador(data, msg_id, file_id, chat_id)
                
                print("✅ Flujo completado. Verifica que haya llegado al grupo general y que los botones de Amanda hayan desaparecido.")
                exit(0)
    except Exception as e:
        pass

print("\n⏳ Tiempo de espera agotado. No se detectó clic en los botones.")
