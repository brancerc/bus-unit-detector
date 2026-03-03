import subprocess
import datetime
import os
import requests
import time
import sys

# ==========================================
# CONFIGURACIÓN
# ==========================================
TOKEN = "8450858285:AAFNh2MIYuZsCR7LNvE-KlYPTlUEEaX7YPo"
CHAT_ID = "5463123453"
IP_CAMARAS = ["10.0.0.2", "10.0.0.3"] 

LOG_DIR = os.path.expanduser('~/Documents/ProyectoIA/logs')
STATE_FILE = os.path.join(LOG_DIR, 'last_state.txt') 
LOG_FILE = os.path.join(LOG_DIR, 'tailscale_status.log')

if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except: pass

def monitorear(es_arranque=False):
    timestamp = datetime.datetime.now().strftime('%H:%M')
    
    # 1. Leer estado anterior
    estado_anterior = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            for line in f:
                parts = line.strip().split(',')
                if len(parts) == 2: estado_anterior[parts[0]] = parts[1]

    estado_actual = {}
    alertas = []

    # 2. Revisar cámaras
    for ip in IP_CAMARAS:
        online = subprocess.call(['ping', '-c', '2', '-W', '1', ip], stdout=subprocess.DEVNULL) == 0
        status_str = "ON" if online else "OFF"
        estado_actual[ip] = status_str
        
        # --- LÓGICA DE NOTIFICACIÓN INTELIGENTE ---
        if not es_arranque:
            last = estado_anterior.get(ip)
            
            # CASO A: Se acaba de caer
            if last == "ON" and status_str == "OFF":
                alertas.append(f"🔴 *CÁMARA CAÍDA*\n📍 IP: `{ip}`\n⏰ Hora: `{timestamp}`\n⚠️ Estado: Desconectada")
            
            # CASO B: Se acaba de recuperar
            elif last == "OFF" and status_str == "ON":
                alertas.append(f"🟢 *CÁMARA RECUPERADA*\n📍 IP: `{ip}`\n⏰ Hora: `{timestamp}`\n✅ Estado: En línea nuevamente")

    # 3. Guardar el nuevo estado
    with open(STATE_FILE, 'w') as f:
        for ip, st in estado_actual.items():
            f.write(f"{ip},{st}\n")

    # 4. Enviar mensajes
    if es_arranque:
        ip_ts = subprocess.getoutput('tailscale ip -4').strip()
        enviar_telegram(f"🚀 *Jetson Online*\n🔗 Tailscale: `{ip_ts}`\n✅ Monitor de cámaras activo.")
    
    for msg in alertas:
        enviar_telegram(msg)

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    monitorear(es_arranque=(arg == "boot"))