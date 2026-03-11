"""
CISA - Sistema de Detección y Monitoreo de Unidades de Transporte
"""

import cv2
import glob
import threading
import time
import subprocess
import os
import sqlite3
import re
import pytesseract
import psycopg2
from collections import Counter
from datetime import datetime
from ultralytics import YOLO
from flask import Flask, send_file, jsonify, request, make_response

from config import (
    PIPELINE, HLS_DIR, FRAMES_DIR, DB_PATH,
    PG_CONFIG, MODEL_PATH, MODEL_CONF, N_VOTOS, VOTO_WINDOW,
    OCR_TARGET_H, OCR_MIN_SIZE, OCR_MAX_DIGITS, OCR_MIN_DIGITS,
    LEVENSHTEIN_MAX, ID_PUERTA, HLS_TIMEOUT, HLS_RESOLUTION, HLS_FPS,
    FLASK_HOST, FLASK_PORT, PG_REFRESH_INTERVAL
)
from alertas import alerta_unidad_detectada

app = Flask(__name__)

# Cargar templates HTML
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
with open(os.path.join(TEMPLATE_DIR, "home.html"), encoding="utf-8") as f: 
    HOME = f.read()
with open(os.path.join(TEMPLATE_DIR, "dashboard.html"), encoding="utf-8") as f: 
    DASHBOARD = f.read()

# ==============================================================================
# Tracker Simple
# ==============================================================================
class SimpleTracker:
    IOU_THRESHOLD = 0.20
    GONE_TIMEOUT  = 3.0
    def __init__(self): 
        self.active = None

    @staticmethod
    def iou(a, b):
        xi1 = max(a[0], b[0]); yi1 = max(a[1], b[1])
        xi2 = min(a[2], b[2]); yi2 = min(a[3], b[3])
        inter = max(0, xi2-xi1) * max(0, yi2-yi1)
        area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0

    def update(self, bbox, numero, estado, conf):
        now = time.time()
        if self.active:
            overlap = self.iou(bbox, self.active['bbox'])
            if overlap > self.IOU_THRESHOLD or numero == self.active['numero']:
                self.active.update({'bbox': bbox, 'last_seen': now})
                if conf > self.active['conf']: self.active['conf'] = conf
                return 'TRACKING'
        self.active = {'numero': numero, 'estado': estado, 'conf': conf, 'bbox': bbox, 'first_seen': now, 'last_seen': now}
        return 'NEW'

    def check_gone(self):
        if self.active and (time.time() - self.active['last_seen'] > self.GONE_TIMEOUT):
            duration = self.active['last_seen'] - self.active['first_seen']
            track = self.active.copy(); self.active = None
            return track, round(duration, 1)
        return None, 0

# ==============================================================================
# Bases de Datos (Validación Estricta)
# ==============================================================================
UNIDADES = set()
_unidades_lock = threading.Lock()
_pg_connected = False

def cargar_unidades_pg():
    global UNIDADES, _pg_connected
    try:
        con = psycopg2.connect(**PG_CONFIG)
        cur = con.cursor()
        cur.execute("SELECT no_economico FROM unidad WHERE estado = true")
        nuevas = set(str(row[0]).strip() for row in cur.fetchall())
        cur.close(); con.close()
        with _unidades_lock:
            UNIDADES = nuevas
        _pg_connected = True
        return True
    except Exception as e:
        _pg_connected = False
        print(f"[PG ERROR] {e}")
        return False

def _refresh_unidades_loop():
    while True:
        cargar_unidades_pg()
        time.sleep(PG_REFRESH_INTERVAL)

threading.Thread(target=_refresh_unidades_loop, daemon=True).start()

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS evento_paso (id_evento INTEGER PRIMARY KEY AUTOINCREMENT, no_detectado TEXT NOT NULL, no_economico TEXT, direccion TEXT DEFAULT 'entrada', hora_paso TEXT NOT NULL, id_puerta INTEGER DEFAULT 1, hora_registro TEXT NOT NULL, captura_url TEXT, estado TEXT DEFAULT 'VERIFICADO', confianza REAL DEFAULT 0, duracion_camara REAL DEFAULT 0)")
    con.commit(); con.close()

def db_insert(no_det, no_eco, conf, url, est, dur=0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO evento_paso (no_detectado, no_economico, direccion, hora_paso, id_puerta, hora_registro, captura_url, estado, confianza, duracion_camara) VALUES (?,?,?,?,?,?,?,?,?,?)", (no_det, no_eco, "entrada", now, ID_PUERTA, now, url, est, round(conf, 3), round(dur, 1)))
        con.commit(); con.close()
    except Exception as e: 
        print(f"[DB ERROR] {e}")

init_db()

# ==============================================================================
# OCR y Validación
# ==============================================================================
def leer_numero(crop):
    try:
        h, w = crop.shape[:2]
        if h < OCR_MIN_SIZE: return None
        scale = max(1, OCR_TARGET_H // h)
        img = cv2.resize(crop, (w*scale, h*scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.fastNlMeansDenoising(gray, h=10)
        gray = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8,8)).apply(gray)
        
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
        _, g1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        g1 = cv2.morphologyEx(g1, cv2.MORPH_OPEN, kernel)
        g2 = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)

        res = []
        for p in [g1, g2]:
            t = pytesseract.image_to_string(p, config='--psm 7 -c tessedit_char_whitelist=0123456789').strip()
            clean = re.sub(r'[^0-9]', '', t)[:OCR_MAX_DIGITS]
            if len(clean) >= OCR_MIN_DIGITS: res.append(clean)
        return Counter(res).most_common(1)[0][0] if res else None
    except: 
        return None

def validar_numero(leido):
    with _unidades_lock: 
        snapshot = UNIDADES.copy()
    if not snapshot: 
        return None, None
    if leido in snapshot: 
        return leido, "VERIFICADO"
    
    match = next((u for u in snapshot if len(u) == len(leido) and levenshtein(leido, u) <= LEVENSHTEIN_MAX), None)
    return (match, "CORREGIDO") if match else (None, None)

def recuperar_ocr(leido):
    with _unidades_lock: 
        snapshot = UNIDADES.copy()
    if not snapshot: 
        return None, None
    if len(leido) == 4 and leido[0] == '4':
        c = '5' + leido[1:]
        if c in snapshot: return c, "CORREGIDO"
    if len(leido) == 3:
        c = '5' + leido
        if c in snapshot: return c, "CORREGIDO"
    return None, None

def levenshtein(a, b):
    m, n = len(a), len(b); dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]; dp[0] = i
        for j in range(1, n + 1): 
            dp[j] = prev[j-1] if a[i-1] == b[j-1] else 1 + min(prev[j], dp[j-1], prev[j-1])
    return dp[n]

# ==============================================================================
# HLS y Video
# ==============================================================================
hls_lock = threading.Lock(); hls_last_ping = 0; hls_proc = None; hls_active = False

def hls_start():
    global hls_proc, hls_active
    if hls_active: return
    for f in glob.glob(os.path.join(HLS_DIR, "*")):
        try: os.remove(f)
        except: pass
    cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{HLS_RESOLUTION[0]}x{HLS_RESOLUTION[1]}', '-pix_fmt', 'bgr24', '-r', str(HLS_FPS), '-i', '-', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '10', f'{HLS_DIR}/stream.m3u8']
    hls_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hls_active = True

def hls_watchdog():
    global hls_active, hls_proc # CORRECCIÓN: Declaración global al inicio
    while True:
        time.sleep(5)
        with hls_lock:
            if hls_active and (time.time() - hls_last_ping > HLS_TIMEOUT):
                if hls_proc: 
                    hls_proc.kill()
                    hls_proc = None
                hls_active = False
                print("[HLS] Stream detenido por inactividad.")

threading.Thread(target=hls_watchdog, daemon=True).start()

def hls_push_frame(frame):
    if not hls_active or hls_proc is None: return
    try:
        resized = cv2.resize(frame, HLS_RESOLUTION)
        hls_proc.stdin.write(resized.tobytes())
    except: 
        pass

# ==============================================================================
# Hilos de Procesamiento
# ==============================================================================
STATE = {"numero":None, "conf":0, "fps":0, "pipeline":False, "tracking":None, "tracking_duracion":0}
state_lock = threading.Lock()

class VideoStream:
    def __init__(self): 
        self.lock = threading.Lock(); self.frame = None; self.running = True; self.cap = None
    def update(self):
        while self.running:
            if not self.cap or not self.cap.isOpened():
                self.cap = cv2.VideoCapture(PIPELINE, cv2.CAP_GSTREAMER)
                time.sleep(2); continue
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.frame = frame
                with state_lock:
                    STATE["pipeline"] = True
            else:
                with state_lock:
                    STATE["pipeline"] = False

class InferenceStream:
    def __init__(self, vs): 
        self.vs = vs; self.running = True; self.model = YOLO(MODEL_PATH, task="detect"); self.tracker = SimpleTracker(); self._votos = []
    def update(self):
        t_fps = time.time(); cnt = 0
        while self.running:
            img = self.vs.get_frame()
            if img is None: continue
            res = self.model.predict(img, conf=MODEL_CONF, device=0, verbose=False)
            annotated = res[0].plot()
            
            cnt += 1
            if time.time() - t_fps >= 1.0:
                with state_lock: STATE["fps"] = round(cnt/(time.time()-t_fps), 1)
                cnt = 0; t_fps = time.time()

            for box in res[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                crop = img[max(0, y1-10):y2+10, max(0, x1-20):x2+20]
                leido = leer_numero(crop)
                if not leido: continue

                self._votos = [v for v in self._votos if time.time() - v[0] < VOTO_WINDOW]
                self._votos.append((time.time(), leido))
                ganador, veces = Counter(v[1] for v in self._votos).most_common(1)[0]
                
                if veces >= N_VOTOS:
                    self._votos = []
                    valido, est = validar_numero(ganador)
                    if not valido: valido, est = recuperar_ocr(ganador)
                    
                    if valido:
                        if self.tracker.update((x1,y1,x2,y2), valido, est, float(box.conf[0])) == 'NEW':
                            fn = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{valido}.jpg"
                            cv2.imwrite(os.path.join(FRAMES_DIR, fn), annotated)
                            db_insert(ganador, valido, float(box.conf[0]), fn, est)
                            threading.Thread(target=alerta_unidad_detectada, args=(os.path.join(FRAMES_DIR, fn), valido, float(box.conf[0]), datetime.now().strftime("%H:%M:%S")), daemon=True).start()

            with hls_lock:
                if hls_active:
                    hls_push_frame(annotated)

vs = VideoStream(); inf = InferenceStream(vs)
threading.Thread(target=vs.update, daemon=True).start()
threading.Thread(target=inf.update, daemon=True).start()

# ==============================================================================
# Rutas Flask
# ==============================================================================
@app.route('/')
def index(): return HOME
@app.route('/livevideo')
def livevideo(): return DASHBOARD
@app.route('/api/hls-ping')
def hls_ping():
    global hls_last_ping
    with hls_lock: 
        hls_last_ping = time.time()
        hls_start()
    return jsonify({"hls": "alive"})
@app.route('/hls/<path:f>')
def hls_files(f): return send_file(os.path.join(HLS_DIR, f))
@app.route('/api/estado')
def api_estado():
    with state_lock: return jsonify(dict(STATE))
@app.route('/api/detecciones')
def api_detecciones(): return jsonify(db_query(limit=20))

if __name__ == '__main__':
    app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True)