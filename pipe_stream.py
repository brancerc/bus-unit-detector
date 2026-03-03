import cv2
import threading
import time
import subprocess
import os
import sqlite3
import re
import pytesseract
import requests
import psycopg2
from ultralytics import YOLO

# ── Telegram ─────────────────────────────────────────────────────────────────
TG_TOKEN   = "8450858285:AAFNh2MIYuZsCR7LNvE-KlYPTlUEEaX7YPo"
TG_CHAT_ID = "5463123453"

def enviar_telegram_frame(frame_path, numero, confianza, ts, sospechoso=False):
    try:
        if sospechoso:
            caption = (
                f"⚠️ *UNIDAD SOSPECHOSA*\n"
                f"🔢 OCR leyó: `{numero}`\n"
                f"❓ No está en lista de unidades\n"
                f"📊 Confianza: `{round(confianza*100)}%`\n"
                f"🕐 Hora: `{ts}`\n"
                f"_Revisar frame para mejorar detección_"
            )
        else:
            caption = (
                f"🚌 *Unidad detectada*\n"
                f"🔢 Número: `{numero}`\n"
                f"📊 Confianza: `{round(confianza*100)}%`\n"
                f"🕐 Hora: `{ts}`"
            )
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
        with open(frame_path, 'rb') as foto:
            requests.post(url, data={
                "chat_id": TG_CHAT_ID,
                "caption": caption,
                "parse_mode": "Markdown"
            }, files={"photo": foto}, timeout=15)
        print(f"[TELEGRAM] Enviado: {numero} ({'SOSPECHOSO' if sospechoso else 'OK'})")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

def enviar_telegram_desconocida(frame_path, numero, confianza, ts):
    """Envía alerta de unidad desconocida a Telegram."""
    try:
        caption = (
            f"🚫 *NÚMERO DESCONOCIDO*\n"
            f"🔢 OCR leyó: `{numero}`\n"
            f"❌ No existe en la base de datos\n"
            f"📊 Confianza YOLO: `{round(confianza*100)}%`\n"
            f"🕐 Hora: `{ts}`\n"
            f"📁 Guardado en: `unidades_desconocidas/`\n"
            f"_⚠️ Revisar frame — número desconocido_"
        )
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
        with open(frame_path, 'rb') as foto:
            requests.post(url, data={
                "chat_id": TG_CHAT_ID,
                "caption": caption,
                "parse_mode": "Markdown"
            }, files={"photo": foto}, timeout=15)
        print(f"[TELEGRAM] Alerta desconocida enviada: {numero}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
from flask import Flask, send_file, jsonify, request
from datetime import datetime

app = Flask(__name__)

# ── Rutas ────────────────────────────────────────────────────────────────────
PIPELINE = (
    "rtspsrc location=rtsp://admin:PatioCCA_@192.168.10.2:554/cam/realmonitor?channel=1&subtype=1 "
    "protocols=tcp latency=200 ! "
    "rtph264depay ! h264parse ! nvv4l2decoder ! "
    "nvvidconv ! video/x-raw,format=BGRx ! "
    "videoconvert ! video/x-raw,format=BGR ! "
    "appsink emit-signals=True max-buffers=1 drop=True sync=False"
)

HLS_DIR    = "/tmp/hls"
FRAMES_DIR = "/media/cisa/JETSON_SD/cisa_frames"
DB_PATH    = "/home/cisa/Documents/ProyectoIA/detecciones.db"

os.makedirs(HLS_DIR,    exist_ok=True)
os.makedirs(FRAMES_DIR, exist_ok=True)

# ── Carpeta para lecturas descartadas (solo debug/revisión) ──────────────────
DESCONOCIDAS_DIR = os.path.join(FRAMES_DIR, "unidades_desconocidas")
os.makedirs(DESCONOCIDAS_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# ── PostgreSQL — Carga dinámica de unidades ──────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
PG_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "postgres",
    "user":     "postgres",
    "password": "",
}

UNIDADES = set()
_unidades_lock = threading.Lock()
_pg_connected  = False

def cargar_unidades_pg():
    """Consulta PostgreSQL y actualiza el set UNIDADES con no_economico activos."""
    global UNIDADES, _pg_connected
    try:
        con = psycopg2.connect(**PG_CONFIG)
        cur = con.cursor()
        cur.execute("SELECT no_economico FROM unidad WHERE estado = true")
        nuevas = set(str(row[0]).strip() for row in cur.fetchall())
        cur.close()
        con.close()

        with _unidades_lock:
            if nuevas != UNIDADES:
                agregadas  = nuevas - UNIDADES
                eliminadas = UNIDADES - nuevas
                if agregadas:
                    print(f"[PG ✓] Unidades nuevas detectadas: {sorted(agregadas)}")
                if eliminadas:
                    print(f"[PG ✓] Unidades removidas: {sorted(eliminadas)}")
                UNIDADES = nuevas

        _pg_connected = True
        print(f"[PG ✓] Conectado a 'postgres' — {len(nuevas)} unidades activas: {sorted(nuevas)[:10]}{'...' if len(nuevas)>10 else ''}")
        return True
    except Exception as e:
        _pg_connected = False
        print(f"[PG ✗ ERROR] No se pudo conectar a PostgreSQL: {e}")
        print(f"[PG ✗ ERROR] Verifica: sudo systemctl status postgresql")
        print(f"[PG ✗ ERROR] Verifica pg_hba.conf → local all all trust")
        return False

def _refresh_unidades_loop():
    """Hilo que refresca la lista de unidades cada 60 segundos."""
    while True:
        cargar_unidades_pg()
        time.sleep(60)

# ── Carga inicial con verificación explícita ─────────────────────────────────
print("=" * 60)
print("[PG] Conectando a PostgreSQL → postgres...")
if cargar_unidades_pg():
    print(f"[PG ✓] ¡Base de datos ACTIVA! {len(UNIDADES)} unidades cargadas")
    print(f"[PG ✓] Lista completa: {sorted(UNIDADES)}")
else:
    print("[PG ✗] ¡¡ FALLO CONEXIÓN !! El sistema NO puede verificar unidades")
    print("[PG ✗] Todas las detecciones serán DESCARTADAS hasta que PG responda")
print("=" * 60)

# Hilo de refresco automático
threading.Thread(target=_refresh_unidades_loop, daemon=True).start()


# ── SQLite ───────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS detecciones (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            numero    TEXT    NOT NULL,
            confianza REAL    NOT NULL,
            fecha     TEXT    NOT NULL,
            hora      TEXT    NOT NULL,
            ts        TEXT    NOT NULL,
            frame     TEXT,
            estado    TEXT    DEFAULT 'VERIFICADO'
        )
    """)
    con.commit(); con.close()
    print("[DB] Base de datos SQLite lista.")

def db_insert(numero, confianza, frame_path=None, estado="VERIFICADO"):
    now = datetime.now()
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO detecciones (numero,confianza,fecha,hora,ts,frame,estado) VALUES (?,?,?,?,?,?,?)",
            (numero, round(confianza,3),
             now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
             now.strftime("%Y-%m-%d %H:%M:%S"), frame_path, estado))
        con.commit(); con.close()
    except Exception as e:
        print(f"[DB ERROR] {e}")

def db_query(fecha=None, limit=50):
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        if fecha:
            rows = con.execute(
                "SELECT * FROM detecciones WHERE fecha=? ORDER BY id DESC LIMIT ?",
                (fecha, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM detecciones ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB ERROR] {e}"); return []

def db_stats():
    try:
        con = sqlite3.connect(DB_PATH)
        hoy   = datetime.now().strftime("%Y-%m-%d")
        total = con.execute("SELECT COUNT(*) FROM detecciones").fetchone()[0]
        hoy_c = con.execute("SELECT COUNT(*) FROM detecciones WHERE fecha=?", (hoy,)).fetchone()[0]
        ult   = con.execute("SELECT * FROM detecciones ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        return {"total": total, "hoy": hoy_c, "ultima": dict(ult) if ult else None}
    except Exception as e:
        print(f"[DB ERROR] {e}"); return {"total":0,"hoy":0,"ultima":None}

init_db()


# ══════════════════════════════════════════════════════════════════════════════
# ── Validación ESTRICTA contra PostgreSQL ────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
# REGLA: Solo se aceptan números que EXISTAN en la BD.
#
#   OCR lee "5647" → existe exacto en PG        → VERIFICADO ✓ (se guarda)
#   OCR lee "5547" → no existe, pero 5647 dist=1 → CORREGIDO a 5647 ✓ (se guarda)
#   OCR lee "1234" → no existe, sin candidato    → DESCARTADO ✗ (se ignora)
#   OCR lee "5547" → no existe, dist a todos > 1 → DESCARTADO ✗ (se ignora)
#
# Si PG está desconectada → TODO se descarta (no se adivina nada)
# ══════════════════════════════════════════════════════════════════════════════

def levenshtein(a, b):
    """Distancia de edición entre dos strings."""
    m, n = len(a), len(b)
    dp = list(range(n+1))
    for i in range(1, m+1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n+1):
            dp[j] = prev[j-1] if a[i-1]==b[j-1] else 1 + min(prev[j], dp[j-1], prev[j-1])
    return dp[n]

def validar_numero(leido):
    """
    Valida contra la BD de PostgreSQL.
    Retorna:
      (numero, estado)  si es válido   → ("5647", "VERIFICADO") o ("5647", "CORREGIDO")
      (None,   None)    si no es válido → se descarta completamente
    """
    leido = leido.strip()

    with _unidades_lock:
        unidades_snapshot = UNIDADES.copy()

    # Si PG no está conectada o la lista está vacía → no aceptar nada
    if not unidades_snapshot:
        print(f"[DESCARTADO] '{leido}' — lista PG vacía, no se puede verificar")
        return None, None

    # 1. Coincidencia EXACTA
    if leido in unidades_snapshot:
        print(f"[VERIFICADO] '{leido}' existe en PostgreSQL ✓")
        return leido, "VERIFICADO"

    # 2. Busca el más parecido (misma longitud, distancia ≤ 1)
    candidatos = [u for u in unidades_snapshot if len(u) == len(leido)]
    mejor, mejor_dist = None, 999
    for u in candidatos:
        d = levenshtein(leido, u)
        if d < mejor_dist:
            mejor_dist = d
            mejor = u

    if mejor_dist <= 1:
        print(f"[CORREGIDO] OCR leyó '{leido}' → BD tiene '{mejor}' (dist={mejor_dist}) ✓")
        return mejor, "CORREGIDO"

    # 3. NO existe → DESCARTAR
    print(f"[DESCARTADO] '{leido}' no coincide con ninguna unidad en PG (mejor: {mejor}, dist={mejor_dist}) ✗")
    return None, None


# ── Tesseract OCR ────────────────────────────────────────────────────────────
print("[INFO] Tesseract OCR listo.")

def leer_numero(crop):
    try:
        h, w = crop.shape[:2]
        if h < 15 or w < 15:
            return None

        target_h = 120
        scale = max(1, target_h // h)
        crop_up = cv2.resize(crop, (w*scale, h*scale), interpolation=cv2.INTER_CUBIC)

        gray = cv2.cvtColor(crop_up, cv2.COLOR_BGR2GRAY)

        resultados = []

        # 1. CLAHE + Otsu
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4,4))
        g1 = clahe.apply(gray)
        _, g1 = cv2.threshold(g1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 2. Umbral adaptativo
        g2 = cv2.adaptiveThreshold(gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 8)

        # 3. Invertido
        g3 = cv2.bitwise_not(g1)

        config = '--psm 7 -c tessedit_char_whitelist=0123456789'
        for img_proc in [g1, g2, g3]:
            texto = pytesseract.image_to_string(img_proc, config=config).strip()
            limpio = re.sub(r'[^0-9]', '', texto)
            limpio = limpio[:4]
            if len(limpio) >= 3:
                resultados.append(limpio)

        if not resultados:
            config8 = '--psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(g1, config=config8).strip()
            limpio = re.sub(r'[^0-9]', '', texto)[:4]
            if len(limpio) >= 3:
                resultados.append(limpio)

        if not resultados:
            return None

        from collections import Counter
        return Counter(resultados).most_common(1)[0][0]

    except Exception as e:
        print(f"[OCR ERROR] {e}"); return None

# ── Modelo TensorRT ──────────────────────────────────────────────────────────
print("[INFO] Cargando modelo TensorRT...")
model = YOLO("best.engine", task="detect")
print("[INFO] Modelo cargado.")

# ── Estado global ────────────────────────────────────────────────────────────
STATE = {
    "numero": None, "conf": None, "ts": None,
    "fps": 0.0, "pipeline": False, "total_detecciones": 0,
    "unidades_registradas": 0, "pg_conectada": False,
    "descartadas": 0
}
state_lock = threading.Lock()

# ── Control HLS bajo demanda ──────────────────────────────────────────────────
hls_lock        = threading.Lock()
hls_last_ping   = 0
HLS_TIMEOUT     = 15
hls_proc        = None
hls_active      = False


def hls_start():
    global hls_proc, hls_active
    if hls_active:
        return
    print("[HLS] Iniciando stream bajo demanda...")
    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', '704x480', '-pix_fmt', 'bgr24', '-r', '15',
        '-i', '-',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-f', 'hls', '-hls_time', '2', '-hls_list_size', '10',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_segment_type', 'mpegts',
        '-hls_segment_filename', f'{HLS_DIR}/seg%03d.ts',
        f'{HLS_DIR}/stream.m3u8'
    ]
    hls_proc   = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hls_active = True


def hls_stop():
    global hls_proc, hls_active
    if not hls_active:
        return
    print("[HLS] Deteniendo stream (sin clientes).")
    try:
        hls_proc.stdin.close()
        hls_proc.wait(timeout=3)
    except Exception:
        hls_proc.kill()
    hls_proc   = None
    hls_active = False


def hls_watchdog():
    while True:
        time.sleep(5)
        with hls_lock:
            if hls_active and (time.time() - hls_last_ping) > HLS_TIMEOUT:
                hls_stop()

threading.Thread(target=hls_watchdog, daemon=True).start()


def hls_push_frame(frame):
    global hls_proc, hls_active
    if not hls_active or hls_proc is None:
        return
    try:
        resized = cv2.resize(frame, (704, 480))
        hls_proc.stdin.write(resized.tobytes())
    except BrokenPipeError:
        hls_stop()
    except Exception:
        pass


# ── Hilo 1: Captura ──────────────────────────────────────────────────────────
class VideoStream:
    def __init__(self):
        self.lock = threading.Lock(); self.frame = None; self.running = True
        self.cap = None
        print("[INFO] VideoStream listo.")

    def _abrir_pipeline(self):
        print("[INFO] Abriendo pipeline GStreamer...")
        if self.cap:
            try: self.cap.release()
            except: pass
        time.sleep(2)
        self.cap = cv2.VideoCapture(PIPELINE, cv2.CAP_GSTREAMER)
        if not self.cap.isOpened():
            print("[ERROR] No se pudo abrir el pipeline. Reintentando en 5s...")
            with state_lock: STATE["pipeline"] = False
        else:
            print("[INFO] Pipeline abierto correctamente.")
            with state_lock: STATE["pipeline"] = True

    def update(self):
        time.sleep(3)
        self._abrir_pipeline()
        while self.running:
            if not self.cap or not self.cap.isOpened():
                time.sleep(5)
                self._abrir_pipeline()
                continue
            ret, frame = self.cap.read()
            if ret:
                with state_lock: STATE["pipeline"] = True
                with self.lock: self.frame = frame
            else:
                with state_lock: STATE["pipeline"] = False
                time.sleep(5)
                self._abrir_pipeline()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False; self.cap.release()


# ══════════════════════════════════════════════════════════════════════════════
# ── Hilo 2: Inferencia + OCR + validación ESTRICTA contra PG ─────────────────
# ══════════════════════════════════════════════════════════════════════════════
class InferenceStream:
    def __init__(self, vs):
        self.lock = threading.Lock(); self.result = None; self.running = True
        self.vs = vs; self.fps = 0.0; self._cnt = 0; self._t = time.time()
        self._last_ts = 0.0; self.COOLDOWN = 2.0
        self._votos = []
        self._N_VOTOS = 1
        self._VOTO_WINDOW = 2.0

    def update(self):
        while self.running:
            img = self.vs.get_frame()
            if img is None:
                time.sleep(0.05); continue
            try:
                results   = model.predict(img, conf=0.50, device=0, verbose=False)
                annotated = results[0].plot()

                # FPS
                self._cnt += 1
                elapsed = time.time() - self._t
                if elapsed >= 1.0:
                    self.fps = self._cnt / elapsed
                    self._cnt = 0; self._t = time.time()
                    with state_lock:
                        STATE["fps"] = round(self.fps, 1)
                        STATE["pg_conectada"] = _pg_connected
                        with _unidades_lock:
                            STATE["unidades_registradas"] = len(UNIDADES)

                for box in results[0].boxes:
                    conf = float(box.conf[0])
                    now  = time.time()
                    if (now - self._last_ts) < self.COOLDOWN:
                        continue

                    # Recorte con padding generoso
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    pad_x = 20
                    pad_y = 10
                    h_img, w_img = img.shape[:2]
                    crop = img[max(0,y1-pad_y):min(h_img,y2+pad_y),
                               max(0,x1-pad_x):min(w_img,x2+pad_x)]

                    numero_leido = leer_numero(crop)
                    if not numero_leido:
                        continue

                    # ── Votación por mayoría ──────────────────────────────
                    self._votos = [(t,n,c) for t,n,c in self._votos
                                   if now - t < self._VOTO_WINDOW]
                    self._votos.append((now, numero_leido, conf))

                    from collections import Counter
                    conteo = Counter(n for _,n,_ in self._votos)
                    numero_ganador, veces = conteo.most_common(1)[0]

                    if veces < self._N_VOTOS:
                        cv2.putText(annotated, f"? {numero_leido} ({veces}/{self._N_VOTOS})",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255,165,0), 2)
                        continue

                    # Consenso alcanzado
                    numero_leido = numero_ganador
                    self._votos = []

                    # ══════════════════════════════════════════════════════
                    # VALIDACIÓN ESTRICTA: solo acepta si está en PG
                    # ══════════════════════════════════════════════════════
                    numero_valido, estado = validar_numero(numero_leido)

                    if numero_valido is None:
                        # ── NO EXISTE EN LA BD → DESCARTAR pero guardar evidencia ──
                        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

                        # Frame completo anotado
                        desc_frame_name = f"{ts_str}_DESCONOCIDA_{numero_leido}.jpg"
                        desc_frame_path = os.path.join(DESCONOCIDAS_DIR, desc_frame_name)
                        desc_frame_save = annotated.copy()
                        cv2.putText(desc_frame_save, f"DESCONOCIDA: {numero_leido}",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX,
                            1.2, (0,0,255), 3)
                        cv2.imwrite(desc_frame_path, desc_frame_save)

                        # Crop limpio
                        desc_crop_name = f"{ts_str}_DESCONOCIDA_{numero_leido}_crop.jpg"
                        cv2.imwrite(os.path.join(DESCONOCIDAS_DIR, desc_crop_name), crop)

                        with state_lock:
                            STATE["descartadas"] += 1

                        print(f"[DESCONOCIDA] '{numero_leido}' guardada en unidades_desconocidas/")

                        # Envía alerta a Telegram
                        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        threading.Thread(
                            target=enviar_telegram_desconocida,
                            args=(desc_frame_path, numero_leido, conf, ts_now),
                            daemon=True
                        ).start()

                        # Muestra en ROJO en el stream
                        cv2.putText(annotated, f"X {numero_leido}",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX,
                            0.9, (0,0,255), 2)

                        # NO activa cooldown → sigue intentando leer el correcto
                        continue

                    # ══════════════════════════════════════════════════════
                    # NÚMERO VÁLIDO → Guardar y notificar
                    # ══════════════════════════════════════════════════════
                    self._last_ts = now   # Cooldown solo con detección válida

                    ts_str     = datetime.now().strftime("%Y%m%d_%H%M%S")
                    frame_name = f"{ts_str}_{numero_valido}.jpg"
                    frame_path = os.path.join(FRAMES_DIR, frame_name)
                    frame_save = annotated.copy()
                    cv2.putText(frame_save, numero_valido,
                        (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (0,255,0), 3)
                    cv2.imwrite(frame_path, frame_save)

                    # Crop limpio para reentrenamiento
                    crop_dir  = os.path.join(FRAMES_DIR, "crops")
                    os.makedirs(crop_dir, exist_ok=True)
                    crop_name = f"{ts_str}_{numero_valido}_crop.jpg"
                    cv2.imwrite(os.path.join(crop_dir, crop_name), crop)

                    with state_lock:
                        STATE["numero"] = numero_valido
                        STATE["conf"]   = round(conf, 3)
                        STATE["ts"]     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        STATE["total_detecciones"] += 1

                    db_insert(numero_valido, conf, frame_name, estado)
                    print(f"[✓ {estado}] {numero_valido} | Conf: {conf:.2f} | Frame: {frame_name}")

                    # Telegram (solo unidades válidas)
                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    threading.Thread(
                        target=enviar_telegram_frame,
                        args=(frame_path, numero_valido, conf, ts_now, False),
                        daemon=True
                    ).start()

                    # Dibuja en verde sobre el stream
                    cv2.putText(annotated, numero_valido,
                        (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (0,255,0), 3)

                # Overlay FPS + hora + estado PG
                cv2.putText(annotated, f"FPS: {self.fps:.1f}",
                    (10,30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
                cv2.putText(annotated, datetime.now().strftime("%H:%M:%S"),
                    (10,60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,0), 2)
                # Indicador PG en el video
                pg_color = (0,255,0) if _pg_connected else (0,0,255)
                pg_text  = f"PG: {len(UNIDADES)} uds" if _pg_connected else "PG: DESCONECTADA"
                cv2.putText(annotated, pg_text,
                    (10,90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, pg_color, 2)

                with self.lock: self.result = annotated

                with hls_lock:
                    hls_push_frame(annotated)

            except Exception as e:
                print(f"[ERROR] Inferencia: {e}"); time.sleep(0.1)

    def get_result(self):
        with self.lock:
            return self.result.copy() if self.result is not None else None

    def stop(self): self.running = False


# ── Init hilos ───────────────────────────────────────────────────────────────
stream    = VideoStream()
inference = InferenceStream(stream)

threading.Thread(target=stream.update,    daemon=True).start()
threading.Thread(target=inference.update, daemon=True).start()
print("[INFO] Hilos iniciados. HLS arrancará solo cuando abras /livevideo")


# ── Páginas HTML ─────────────────────────────────────────────────────────────
HOME = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CISA · Número económico</title>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;900&display=swap" rel="stylesheet">
  <style>
    :root{--verde:#00ff88;--fondo:#060809;}
    *{margin:0;padding:0;box-sizing:border-box;}
    body{background:var(--fondo);min-height:100vh;display:flex;flex-direction:column;
         align-items:center;justify-content:center;font-family:'Barlow Condensed',sans-serif;}
    .label{font-size:11px;font-weight:700;letter-spacing:5px;text-transform:uppercase;
           color:#2a3a2a;margin-bottom:16px;}
    .numero{font-family:'Share Tech Mono',monospace;font-size:clamp(100px,18vw,220px);
            line-height:1;color:var(--verde);text-shadow:0 0 80px rgba(0,255,136,.2);
            letter-spacing:8px;text-align:center;word-break:break-all;padding:0 20px;}
    .numero.vacio{color:#0d1f0d;}
    .numero.flash{animation:flash .35s ease forwards;}
    @keyframes flash{0%{color:#fff;text-shadow:0 0 120px #fff;}
                     100%{color:var(--verde);text-shadow:0 0 80px rgba(0,255,136,.2);}}
    .conf{margin-top:18px;font-family:'Share Tech Mono',monospace;font-size:13px;
          color:#1a2f1a;letter-spacing:3px;text-transform:uppercase;transition:color .3s;}
    .conf.visible{color:#2a5a2a;}
    .ts{margin-top:8px;font-family:'Share Tech Mono',monospace;font-size:12px;
        color:#141e14;letter-spacing:2px;}
    .live-link{position:fixed;bottom:28px;right:32px;font-family:'Share Tech Mono',monospace;
               font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#1c2c1c;
               text-decoration:none;border:1px solid #1c2c1c;padding:8px 16px;
               border-radius:20px;transition:all .25s;}
    .live-link:hover{color:var(--verde);border-color:var(--verde);text-shadow:0 0 10px var(--verde);}
    .dot{position:fixed;top:24px;right:28px;width:8px;height:8px;border-radius:50%;background:#0d1f0d;}
    .dot.ok{background:var(--verde);box-shadow:0 0 8px var(--verde);animation:pulse 2s ease-in-out infinite;}
    @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}
  </style>
</head>
<body>
  <div class="dot" id="dot"></div>
  <div class="label">CISA · número económico</div>
  <div class="numero vacio" id="numero">----</div>
  <div class="conf" id="conf">confianza —</div>
  <div class="ts"   id="ts"></div>
  <a class="live-link" href="/livevideo">▶ live video</a>
  <script>
    let prev=null;
    async function poll(){
      try{
        const d=await fetch('/api/estado').then(r=>r.json());
        document.getElementById('dot').className='dot '+(d.pipeline?'ok':'');
        if(d.numero){
          const el=document.getElementById('numero');
          if(d.numero!==prev){
            el.classList.remove('flash','vacio');void el.offsetWidth;
            el.classList.add('flash');prev=d.numero;
          }else el.classList.remove('vacio');
          el.textContent=d.numero;
          const pct=Math.round((d.conf||0)*100);
          const cEl=document.getElementById('conf');
          cEl.textContent='confianza  '+pct+'%';cEl.className='conf visible';
          document.getElementById('ts').textContent=d.ts||'';
        }
      }catch(e){}
    }
    poll();setInterval(poll,1000);
  </script>
</body>
</html>"""

DASHBOARD = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CISA · Monitor Metrobús</title>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;700;900&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <style>
    :root{--verde:#00ff88;--amarillo:#ffe600;--rojo:#ff3b3b;--fondo:#090c10;--panel:#0d1117;--borde:#1c2330;--texto:#cdd9e5;}
    *{margin:0;padding:0;box-sizing:border-box;}
    body{background:var(--fondo);color:var(--texto);font-family:'Barlow Condensed',sans-serif;
         min-height:100vh;display:grid;grid-template-rows:auto auto 1fr auto;overflow-x:hidden;}
    header{display:flex;align-items:center;justify-content:space-between;
           padding:14px 28px;border-bottom:1px solid var(--borde);background:var(--panel);}
    .brand{display:flex;align-items:center;gap:14px;}
    .brand-dot{width:10px;height:10px;border-radius:50%;background:var(--verde);
               box-shadow:0 0 10px var(--verde);animation:pulse 1.8s ease-in-out infinite;}
    @keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.4;transform:scale(1.3);}}
    .brand-name{font-size:22px;font-weight:900;letter-spacing:4px;text-transform:uppercase;color:#fff;}
    .brand-sub{font-size:11px;font-weight:300;letter-spacing:3px;color:#556;text-transform:uppercase;}
    .header-right{display:flex;align-items:center;gap:24px;}
    .pill{font-family:'Share Tech Mono',monospace;font-size:12px;padding:4px 12px;
          border-radius:20px;border:1px solid var(--borde);background:#111820;color:#556;}
    .pill.live{border-color:var(--verde);color:var(--verde);}
    .pill.pg-ok{border-color:var(--verde);color:var(--verde);}
    .pill.pg-err{border-color:var(--rojo);color:var(--rojo);animation:pulse 1s infinite;}
    .back-link{font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:2px;
               text-decoration:none;color:#445;border:1px solid #1c2330;padding:5px 12px;
               border-radius:20px;transition:all .2s;}
    .back-link:hover{color:var(--verde);border-color:var(--verde);}
    #clock{font-family:'Share Tech Mono',monospace;font-size:14px;color:#445;}
    .tabs{display:flex;background:var(--panel);border-bottom:1px solid var(--borde);}
    .tab{padding:10px 28px;font-size:11px;font-weight:700;letter-spacing:3px;
         text-transform:uppercase;cursor:pointer;border-bottom:2px solid transparent;color:#445;transition:all .2s;}
    .tab:hover{color:var(--texto);}
    .tab.active{color:var(--verde);border-bottom-color:var(--verde);}
    .page{display:none;}
    .page.active{display:grid;}
    #page-live{grid-template-columns:1fr 340px;height:calc(100vh - 125px);}
    .video-panel{position:relative;background:#000;display:flex;
                 align-items:center;justify-content:center;border-right:1px solid var(--borde);}
    video{width:100%;height:100%;object-fit:contain;display:block;}
    .video-overlay{position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;}
    .corner{position:absolute;width:20px;height:20px;border-color:var(--verde);border-style:solid;opacity:.6;}
    .corner.tl{top:12px;left:12px;border-width:2px 0 0 2px;}
    .corner.tr{top:12px;right:12px;border-width:2px 2px 0 0;}
    .corner.bl{bottom:12px;left:12px;border-width:0 0 2px 2px;}
    .corner.br{bottom:12px;right:12px;border-width:0 2px 2px 0;}
    .scanline{position:absolute;top:0;left:0;right:0;height:2px;
              background:linear-gradient(90deg,transparent,var(--verde),transparent);
              opacity:.15;animation:scan 4s linear infinite;}
    @keyframes scan{0%{top:0}100%{top:100%}}
    .side{display:flex;flex-direction:column;overflow-y:auto;background:var(--panel);}
    .section{padding:20px 24px;border-bottom:1px solid var(--borde);}
    .section-label{font-size:10px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#445;margin-bottom:14px;}
    .numero-display{font-family:'Share Tech Mono',monospace;font-size:64px;font-weight:400;
                    line-height:1;color:var(--verde);text-shadow:0 0 40px rgba(0,255,136,.25);
                    letter-spacing:4px;transition:all .3s ease;min-height:70px;
                    display:flex;align-items:center;overflow:hidden;word-break:break-all;}
    .numero-display.nueva{animation:flash .4s ease;}
    @keyframes flash{0%{color:#fff;text-shadow:0 0 60px #fff;}100%{color:var(--verde);text-shadow:0 0 40px rgba(0,255,136,.25);}}
    .numero-vacio{color:#223;font-size:48px;}
    .conf-bar-wrap{margin-top:12px;}
    .conf-label{font-size:11px;font-weight:300;letter-spacing:2px;color:#445;margin-bottom:6px;display:flex;justify-content:space-between;}
    .conf-bar-bg{height:4px;background:var(--borde);border-radius:2px;overflow:hidden;}
    .conf-bar-fill{height:100%;background:linear-gradient(90deg,var(--verde),var(--amarillo));border-radius:2px;transition:width .5s ease;width:0%;}
    .ts-text{font-family:'Share Tech Mono',monospace;font-size:13px;color:#334;margin-top:10px;letter-spacing:1px;}
    .metrics-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
    .metric-card{background:#0a0f16;border:1px solid var(--borde);border-radius:6px;padding:12px 14px;}
    .metric-val{font-family:'Share Tech Mono',monospace;font-size:26px;color:var(--amarillo);line-height:1;}
    .metric-lbl{font-size:10px;letter-spacing:2px;color:#334;margin-top:4px;text-transform:uppercase;}
    .metric-val.rojo{color:var(--rojo);}
    .status-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #111820;font-size:12px;letter-spacing:1px;}
    .status-row:last-child{border-bottom:none;}
    .status-key{color:#334;}
    .status-val{font-family:'Share Tech Mono',monospace;color:#556;}
    .status-val.ok{color:var(--verde);}
    .status-val.err{color:var(--rojo);}
    #page-historial{height:calc(100vh - 125px);overflow-y:auto;}
    #page-historial.active{display:flex;flex-direction:column;gap:20px;padding:28px;}
    .hist-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}
    .stat-card{background:var(--panel);border:1px solid var(--borde);border-radius:8px;padding:16px 20px;}
    .stat-val{font-family:'Share Tech Mono',monospace;font-size:32px;color:var(--amarillo);}
    .stat-lbl{font-size:11px;letter-spacing:2px;color:#445;margin-top:4px;text-transform:uppercase;}
    .hist-toolbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
    .hist-toolbar input[type=date]{background:var(--panel);border:1px solid var(--borde);
      color:var(--texto);padding:8px 14px;border-radius:6px;font-family:'Share Tech Mono',monospace;font-size:13px;}
    .btn{padding:8px 18px;border-radius:6px;border:1px solid var(--verde);background:transparent;
         color:var(--verde);font-family:'Barlow Condensed',sans-serif;font-size:13px;
         font-weight:700;letter-spacing:2px;cursor:pointer;transition:all .2s;}
    .btn:hover{background:var(--verde);color:#000;}
    .btn.danger{border-color:var(--rojo);color:var(--rojo);}
    .btn.danger:hover{background:var(--rojo);color:#fff;}
    .hist-table-wrap{overflow-x:auto;}
    table{width:100%;border-collapse:collapse;font-size:13px;}
    th{text-align:left;padding:10px 14px;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#445;border-bottom:1px solid var(--borde);}
    td{padding:10px 14px;border-bottom:1px solid #0d1117;}
    tr:hover td{background:#0d1117;}
    .badge-num{font-family:'Share Tech Mono',monospace;color:var(--verde);font-size:16px;letter-spacing:2px;}
    .badge-conf{font-family:'Share Tech Mono',monospace;font-size:12px;padding:2px 8px;border-radius:4px;background:#0a1a10;color:var(--verde);}
    .thumb{width:60px;height:40px;object-fit:cover;border-radius:3px;cursor:pointer;border:1px solid var(--borde);}
    .thumb:hover{border-color:var(--verde);}
    .empty-state{text-align:center;padding:60px;color:#334;font-size:14px;letter-spacing:2px;}
    footer{padding:8px 28px;border-top:1px solid var(--borde);background:var(--panel);
           display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#334;letter-spacing:2px;}
    footer a{color:#445;text-decoration:none;}
    footer a:hover{color:var(--verde);}
    @media(max-width:900px){
      #page-live{grid-template-columns:1fr;grid-template-rows:auto 1fr;}
      .video-panel{min-height:240px;border-right:none;border-bottom:1px solid var(--borde);}
      .numero-display{font-size:48px;}
      .hist-stats{grid-template-columns:1fr 1fr;}
    }
  </style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-dot"></div>
    <div>
      <div class="brand-name">CISA</div>
      <div class="brand-sub">Monitor Metrobús · Jetson Orin Nano</div>
    </div>
  </div>
  <div class="header-right">
    <span class="pill live">● EN VIVO</span>
    <span class="pill" id="hls-pill">HLS · iniciando...</span>
    <span class="pill" id="pg-pill">PG · ...</span>
    <a class="back-link" href="/">← número</a>
    <span id="clock">--:--:--</span>
  </div>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('live')">▶ EN VIVO</div>
  <div class="tab"        onclick="showTab('historial')">☰ HISTORIAL</div>
</div>

<div id="page-live" class="page active">
  <div class="video-panel">
    <video id="video" autoplay muted playsinline></video>
    <div class="video-overlay">
      <div class="corner tl"></div><div class="corner tr"></div>
      <div class="corner bl"></div><div class="corner br"></div>
      <div class="scanline"></div>
    </div>
  </div>
  <div class="side">
    <div class="section">
      <div class="section-label">Número económico</div>
      <div class="numero-display numero-vacio" id="numero">_ _ _ _</div>
      <div class="conf-bar-wrap">
        <div class="conf-label"><span>CONFIANZA YOLO</span><span id="conf-pct">—</span></div>
        <div class="conf-bar-bg"><div class="conf-bar-fill" id="conf-bar"></div></div>
      </div>
      <div class="ts-text" id="ts">Esperando detección...</div>
    </div>
    <div class="section">
      <div class="section-label">Rendimiento</div>
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-val" id="fps-val">--</div>
          <div class="metric-lbl">FPS · GPU</div>
        </div>
        <div class="metric-card">
          <div class="metric-val" id="total-val">0</div>
          <div class="metric-lbl">Aceptadas ✓</div>
        </div>
        <div class="metric-card">
          <div class="metric-val" id="pg-count">--</div>
          <div class="metric-lbl">Unidades PG</div>
        </div>
        <div class="metric-card">
          <div class="metric-val rojo" id="desc-val">0</div>
          <div class="metric-lbl">Descartadas ✗</div>
        </div>
      </div>
    </div>
    <div class="section">
      <div class="section-label">Sistema</div>
      <div class="status-row">
        <span class="status-key">Pipeline</span>
        <span class="status-val" id="s-pipeline">--</span>
      </div>
      <div class="status-row">
        <span class="status-key">Modelo</span>
        <span class="status-val ok">best.engine</span>
      </div>
      <div class="status-row">
        <span class="status-key">OCR</span>
        <span class="status-val ok">Tesseract · CPU</span>
      </div>
      <div class="status-row">
        <span class="status-key">PostgreSQL</span>
        <span class="status-val" id="s-pg">verificando...</span>
      </div>
      <div class="status-row">
        <span class="status-key">Validación</span>
        <span class="status-val ok">ESTRICTA · solo BD</span>
      </div>
      <div class="status-row">
        <span class="status-key">HLS</span>
        <span class="status-val ok" id="s-hls">bajo demanda</span>
      </div>
      <div class="status-row">
        <span class="status-key">Jetson IP</span>
        <span class="status-val">100.101.67.41</span>
      </div>
    </div>
  </div>
</div>

<div id="page-historial" class="page">
  <div class="hist-stats">
    <div class="stat-card"><div class="stat-val" id="stat-total">--</div><div class="stat-lbl">Total registros</div></div>
    <div class="stat-card"><div class="stat-val" id="stat-hoy">--</div><div class="stat-lbl">Hoy</div></div>
    <div class="stat-card"><div class="stat-val" id="stat-ultima">--</div><div class="stat-lbl">Última detección</div></div>
  </div>
  <div class="hist-toolbar">
    <input type="date" id="filtro-fecha">
    <button class="btn" onclick="cargarHistorial()">BUSCAR</button>
    <button class="btn" onclick="cargarHistorial(null,200)">ÚLTIMAS 200</button>
    <button class="btn danger" onclick="exportarCSV()">↓ EXPORTAR CSV</button>
  </div>
  <div class="hist-table-wrap">
    <table>
      <thead><tr><th>#</th><th>Número económico</th><th>Estado</th><th>Confianza</th><th>Fecha</th><th>Hora</th><th>Frame</th></tr></thead>
      <tbody id="tabla-body"><tr><td colspan="7" class="empty-state">Cargando...</td></tr></tbody>
    </table>
  </div>
</div>

<footer>
  <span>CISA · Validación ESTRICTA contra PostgreSQL (postgres)</span>
  <span><a href="/api/estado">API Estado</a> · <a href="/api/detecciones">API Detecciones</a> · <a href="/api/unidades">API Unidades</a></span>
</footer>

<script>
function tick(){document.getElementById('clock').textContent=new Date().toTimeString().slice(0,8);}
tick();setInterval(tick,1000);

function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.toggle('active',['live','historial'][i]===name);});
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  if(name==='historial'){cargarStats();cargarHistorial();}
}

const video=document.getElementById('video');
let hlsObj=null;
let lastTime=0, stallCount=0;

function initHLS(){
  if(hlsObj){ hlsObj.destroy(); hlsObj=null; }
  if(Hls.isSupported()){
    hlsObj=new Hls({
      liveSyncDurationCount:3, liveMaxLatencyDurationCount:8, lowLatencyMode:false,
      manifestLoadingMaxRetry:999, levelLoadingMaxRetry:999, fragLoadingMaxRetry:999,
      manifestLoadingRetryDelay:1000, fragLoadingRetryDelay:1000,
    });
    hlsObj.loadSource('/hls/stream.m3u8');
    hlsObj.attachMedia(video);
    hlsObj.on(Hls.Events.MANIFEST_PARSED,()=>{ video.play(); stallCount=0; });
    hlsObj.on(Hls.Events.ERROR,(e,d)=>{ if(d.fatal) setTimeout(initHLS, 3000); });
    document.getElementById('hls-pill').textContent='HLS · activo';
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src='/hls/stream.m3u8'; video.play();
  }
}

setInterval(()=>{
  if(!video.paused && video.readyState >= 2){
    if(video.currentTime === lastTime){ stallCount++; if(stallCount >= 2){ stallCount=0; initHLS(); } }
    else { stallCount=0; lastTime=video.currentTime; }
  }
}, 4000);

async function pingHLS(){ try{ await fetch('/api/hls-ping'); }catch(e){} }
fetch('/api/hls-start').then(()=>{ setTimeout(initHLS, 3000); });
setInterval(pingHLS, 5000);

let prevNumero=null;
async function poll(){
  try{
    const d=await fetch('/api/estado').then(r=>r.json());
    document.getElementById('fps-val').textContent=d.fps||'--';
    document.getElementById('total-val').textContent=d.total_detecciones||0;
    document.getElementById('desc-val').textContent=d.descartadas||0;
    document.getElementById('pg-count').textContent=d.unidades_registradas||'--';

    const pgPill=document.getElementById('pg-pill');
    const sPg=document.getElementById('s-pg');
    if(d.pg_conectada){
      pgPill.textContent='PG ✓ '+d.unidades_registradas+' uds';
      pgPill.className='pill pg-ok';
      sPg.textContent='postgres · '+d.unidades_registradas+' uds';
      sPg.className='status-val ok';
    }else{
      pgPill.textContent='PG ✗ DESCONECTADA';
      pgPill.className='pill pg-err';
      sPg.textContent='ERROR · sin conexión';
      sPg.className='status-val err';
    }

    const sp=document.getElementById('s-pipeline');
    sp.textContent=d.pipeline?'ACTIVO':'ERROR';sp.className='status-val '+(d.pipeline?'ok':'err');
    const numEl=document.getElementById('numero');
    if(d.numero){
      if(d.numero!==prevNumero){numEl.classList.remove('nueva');void numEl.offsetWidth;numEl.classList.add('nueva');prevNumero=d.numero;}
      numEl.textContent=d.numero;numEl.classList.remove('numero-vacio');
      const pct=Math.round((d.conf||0)*100);
      document.getElementById('conf-pct').textContent=pct+'%';
      document.getElementById('conf-bar').style.width=pct+'%';
      document.getElementById('ts').textContent=d.ts||'';
    }
  }catch(e){}
}
poll();setInterval(poll,2000);

async function cargarStats(){
  try{
    const d=await fetch('/api/stats').then(r=>r.json());
    document.getElementById('stat-total').textContent=d.total;
    document.getElementById('stat-hoy').textContent=d.hoy;
    document.getElementById('stat-ultima').textContent=d.ultima?d.ultima.numero:'--';
  }catch(e){}
}

async function cargarHistorial(fecha=null,limit=50){
  const f=fecha||document.getElementById('filtro-fecha').value||null;
  let url='/api/detecciones?limit='+limit;if(f)url+='&fecha='+f;
  try{
    const rows=await fetch(url).then(r=>r.json());
    const tbody=document.getElementById('tabla-body');
    if(!rows.length){tbody.innerHTML='<tr><td colspan="7" class="empty-state">Sin detecciones</td></tr>';return;}
    tbody.innerHTML=rows.map(r=>{
      const estadoColor = r.estado==='VERIFICADO'?'#00ff88': r.estado==='CORREGIDO'?'#ffe600':'#ff3b3b';
      const estadoIcon  = r.estado==='VERIFICADO'?'✓': r.estado==='CORREGIDO'?'~':'⚠';
      return `<tr>
        <td style="color:#334;font-family:'Share Tech Mono',monospace">${r.id}</td>
        <td><span class="badge-num">${r.numero}</span></td>
        <td><span style="font-family:'Share Tech Mono',monospace;font-size:11px;color:${estadoColor}">${estadoIcon} ${r.estado||'—'}</span></td>
        <td><span class="badge-conf">${Math.round(r.confianza*100)}%</span></td>
        <td style="color:#556;font-family:'Share Tech Mono',monospace">${r.fecha}</td>
        <td style="color:#556;font-family:'Share Tech Mono',monospace">${r.hora}</td>
        <td>${r.frame?`<img class="thumb" src="/frames/${r.frame}" onclick="window.open('/frames/${r.frame}')" title="${r.frame}">`:'—'}</td>
      </tr>`;
    }).join('');
  }catch(e){}
}

async function exportarCSV(){
  const f=document.getElementById('filtro-fecha').value||null;
  let url='/api/detecciones?limit=9999';if(f)url+='&fecha='+f;
  const rows=await fetch(url).then(r=>r.json());
  if(!rows.length){alert('Sin datos');return;}
  const csv=['id,numero,estado,confianza,fecha,hora,frame',...rows.map(r=>`${r.id},${r.numero},${r.estado||''},${r.confianza},${r.fecha},${r.hora},${r.frame||''}`)].join('\\n');
  const a=document.createElement('a');
  a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download='cisa_'+(f||'todas')+'.csv';a.click();
}

document.getElementById('filtro-fecha').value=new Date().toISOString().slice(0,10);
</script>
</body>
</html>"""


# ── Rutas Flask ───────────────────────────────────────────────────────────────
@app.route('/')
def index(): return HOME

@app.route('/livevideo')
def livevideo(): return DASHBOARD

@app.route('/api/hls-start')
def api_hls_start():
    global hls_last_ping
    with hls_lock:
        hls_last_ping = time.time()
        hls_start()
    return jsonify({"hls": "started"})

@app.route('/api/hls-ping')
def api_hls_ping():
    global hls_last_ping
    with hls_lock:
        hls_last_ping = time.time()
        if not hls_active:
            hls_start()
    return jsonify({"hls": "alive"})

@app.route('/hls/<path:filename>')
def hls_files(filename):
    filepath = os.path.join(HLS_DIR, filename)
    if not os.path.exists(filepath): return '', 404
    mt = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    return send_file(filepath, mimetype=mt)

@app.route('/frames/<path:filename>')
def serve_frame(filename):
    filepath = os.path.join(FRAMES_DIR, filename)
    if not os.path.exists(filepath): return '', 404
    return send_file(filepath, mimetype='image/jpeg')

@app.route('/api/estado')
def api_estado():
    with state_lock: return jsonify(dict(STATE))

@app.route('/api/detecciones')
def api_detecciones():
    fecha = request.args.get('fecha')
    limit = int(request.args.get('limit', 50))
    return jsonify(db_query(fecha=fecha, limit=limit))

@app.route('/api/stats')
def api_stats(): return jsonify(db_stats())

@app.route('/api/unidades')
def api_unidades():
    """Muestra las unidades cargadas desde PostgreSQL."""
    with _unidades_lock:
        return jsonify({
            "pg_conectada": _pg_connected,
            "total": len(UNIDADES),
            "unidades": sorted(list(UNIDADES))
        })

@app.route('/health')
def health():
    with state_lock:
        return jsonify({
            "status": "ok",
            "fps": STATE["fps"],
            "pipeline": STATE["pipeline"],
            "hls": hls_active,
            "pg_conectada": _pg_connected,
            "unidades_pg": len(UNIDADES),
            "descartadas": STATE["descartadas"]
        })


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("[INFO] Servidor en http://0.0.0.0:5001")
    print(f"[INFO] PostgreSQL: {'✓ CONECTADA' if _pg_connected else '✗ DESCONECTADA'} — {len(UNIDADES)} unidades")
    print(f"[INFO] Modo: VALIDACIÓN ESTRICTA — solo acepta unidades que existan en PG")
    print(f"[INFO] Lecturas incorrectas se DESCARTAN y el sistema sigue intentando")
    try:
        app.run(host='0.0.0.0', port=5001, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo...")
        with hls_lock: hls_stop()
        stream.stop(); inference.stop()