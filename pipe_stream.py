"""
CISA - Sistema de Detección y Monitoreo de Unidades de Transporte
Pipeline: RTSP → GStreamer → YOLO → OCR → Validación PG → Tracking → Alertas
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
    PIPELINE, HLS_DIR, FRAMES_DIR, DESCONOCIDAS_DIR, CROPS_DIR, CLEAN_DIR,
    CLEAN_LATERAL_DIR, CLEAN_TRASERO_DIR, DB_PATH,
    PG_CONFIG, MODEL_PATH, MODEL_CONF, COOLDOWN_SEG, N_VOTOS, VOTO_WINDOW,
    OCR_TARGET_H, OCR_MIN_SIZE, OCR_MAX_DIGITS, OCR_MIN_DIGITS,
    LEVENSHTEIN_MAX, ID_PUERTA, HLS_TIMEOUT, HLS_RESOLUTION, HLS_FPS,
    FLASK_HOST, FLASK_PORT, PG_REFRESH_INTERVAL
)
from alertas import (
    alerta_unidad_detectada,
    alerta_unidad_desconocida,
)

app = Flask(__name__)

# Cargar templates HTML
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

with open(os.path.join(TEMPLATE_DIR, "home.html"), encoding="utf-8") as f:
    HOME = f.read()
with open(os.path.join(TEMPLATE_DIR, "dashboard.html"), encoding="utf-8") as f:
    DASHBOARD = f.read()

# ==============================================================================
# Tracker simple por IoU
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
        area_a = (a[2]-a[0]) * (a[3]-a[1])
        area_b = (b[2]-b[0]) * (b[3]-b[1])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0

    def update(self, bbox, numero, estado, conf):
        now = time.time()
        if self.active:
            overlap  = self.iou(bbox, self.active['bbox'])
            same_num = (numero == self.active['numero'])
            if overlap > self.IOU_THRESHOLD or same_num:
                self.active['bbox']      = bbox
                self.active['last_seen'] = now
                if conf > self.active['conf']:
                    self.active['conf'] = conf
                return 'TRACKING'
        self.active = {
            'numero': numero, 'estado': estado, 'conf': conf,
            'bbox': bbox, 'first_seen': now, 'last_seen': now,
        }
        return 'NEW'

    def check_gone(self):
        if self.active is None: return None, 0
        elapsed = time.time() - self.active['last_seen']
        if elapsed > self.GONE_TIMEOUT:
            duration = self.active['last_seen'] - self.active['first_seen']
            track = self.active.copy()
            self.active = None
            return track, round(duration, 1)
        return None, 0

    def get_duration(self):
        return round(time.time() - self.active['first_seen'], 1) if self.active else 0

# ==============================================================================
# Gestión de Base de Datos (PG y SQLite)
# ==============================================================================

UNIDADES = set()
_unidades_lock = threading.Lock()
_pg_connected  = False

def cargar_unidades_pg():
    global UNIDADES, _pg_connected
    try:
        con = psycopg2.connect(**PG_CONFIG)
        cur = con.cursor()
        cur.execute("SELECT no_economico FROM unidad WHERE estado = true")
        nuevas = set(str(row[0]).strip() for row in cur.fetchall())
        cur.close(); con.close()
        with _unidades_lock: UNIDADES = nuevas
        _pg_connected = True
        return True
    except Exception as e:
        _pg_connected = False
        print(f"[PG ERROR] {e}"); return False

def _refresh_unidades_loop():
    while True:
        cargar_unidades_pg()
        time.sleep(PG_REFRESH_INTERVAL)

threading.Thread(target=_refresh_unidades_loop, daemon=True).start()

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS evento_paso (
            id_evento       INTEGER PRIMARY KEY AUTOINCREMENT,
            no_detectado    TEXT    NOT NULL,
            no_economico    TEXT,
            direccion       TEXT    DEFAULT 'entrada',
            hora_paso       TEXT    NOT NULL,
            id_puerta       INTEGER DEFAULT 1,
            hora_registro   TEXT    NOT NULL,
            captura_url     TEXT,
            estado          TEXT    DEFAULT 'VERIFICADO',
            confianza       REAL    DEFAULT 0,
            duracion_camara REAL    DEFAULT 0
        )
    """)
    con.commit(); con.close()

init_db()

def db_insert(no_detectado, no_economico, confianza, captura_url=None, estado="VERIFICADO", duracion=0):
    now = datetime.now()
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO evento_paso (no_detectado, no_economico, direccion, hora_paso, id_puerta, hora_registro, captura_url, estado, confianza, duracion_camara) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (no_detectado, no_economico, "entrada", now.strftime("%Y-%m-%d %H:%M:%S"), ID_PUERTA, now.strftime("%Y-%m-%d %H:%M:%S"), captura_url, estado, round(confianza, 3), round(duracion, 1)))
        con.commit(); con.close()
    except Exception as e: print(f"[DB ERROR] {e}")

# ==============================================================================
# Lógica OCR y Validación
# ==============================================================================

def levenshtein(a, b):
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            dp[j] = prev[j-1] if a[i-1] == b[j-1] else 1 + min(prev[j], dp[j-1], prev[j-1])
    return dp[n]

def validar_numero(leido):
    leido = leido.strip()
    with _unidades_lock: unidades_snapshot = UNIDADES.copy()
    if not unidades_snapshot: return None, None
    if leido in unidades_snapshot: return leido, "VERIFICADO"
    
    candidatos = [u for u in unidades_snapshot if len(u) == len(leido)]
    mejor, mejor_dist = None, 999
    for u in candidatos:
        d = levenshtein(leido, u)
        if d < mejor_dist: mejor_dist = d; mejor = u
    if mejor_dist <= LEVENSHTEIN_MAX: return mejor, "CORREGIDO"
    return None, None

def recuperar_ocr(leido):
    """
    Intenta corregir patrones erróneos detectados en logs.
    """
    leido = leido.strip()
    with _unidades_lock: unidades_snapshot = UNIDADES.copy()
    if not unidades_snapshot: return None, None

    # Error común: Tesseract lee '4' en lugar de '5' al inicio
    if len(leido) == 4 and leido[0] == '4':
        candidato = '5' + leido[1:]
        if candidato in unidades_snapshot: return candidato, "CORREGIDO"

    # Caso 3 dígitos: Prender el 5 inicial que suele perderse
    if len(leido) == 3:
        candidato = '5' + leido
        if candidato in unidades_snapshot: return candidato, "CORREGIDO"

    return None, None

def leer_numero(crop):
    """
    Procesamiento avanzado para separar trazos y mejorar lectura.
    """
    try:
        h, w = crop.shape[:2]
        if h < OCR_MIN_SIZE or w < OCR_MIN_SIZE: return None
        
        scale = max(1, OCR_TARGET_H // h)
        crop_up = cv2.resize(crop, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(crop_up, cv2.COLOR_BGR2GRAY)
        
        # Limpieza de ruido y realce de contraste local
        gray = cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=15)
        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        kernel_sep = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        resultados = []

        # Estrategia 1: Otsu + Apertura (Separa trazos pegados)
        _, g1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        g1 = cv2.morphologyEx(g1, cv2.MORPH_OPEN, kernel_sep)

        # Estrategia 2: Adaptativo (Lidia con brillos LED)
        g2 = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)

        config = '--psm 7 -c tessedit_char_whitelist=0123456789'
        for img_proc in [g1, g2]:
            texto = pytesseract.image_to_string(img_proc, config=config).strip()
            limpio = re.sub(r'[^0-9]', '', texto)[:OCR_MAX_DIGITS]
            if len(limpio) >= OCR_MIN_DIGITS: resultados.append(limpio)

        return Counter(resultados).most_common(1)[0][0] if resultados else None
    except: return None

# ==============================================================================
# Hilos de Captura e Inferencia
# ==============================================================================

model = YOLO(MODEL_PATH, task="detect")
STATE = {"numero": None, "conf": None, "ts": None, "fps": 0.0, "pipeline": False, "total_detecciones": 0, "unidades_registradas": 0, "pg_conectada": False, "descartadas": 0, "tracking": None, "tracking_duracion": 0}
state_lock = threading.Lock()

class VideoStream:
    def __init__(self):
        self.lock = threading.Lock(); self.frame = None; self.running = True; self.cap = None
    def _abrir(self):
        if self.cap: self.cap.release()
        self.cap = cv2.VideoCapture(PIPELINE, cv2.CAP_GSTREAMER)
    def update(self):
        self._abrir()
        while self.running:
            if not self.cap or not self.cap.isOpened(): self._abrir(); time.sleep(2); continue
            ret, frame = self.cap.read()
            if ret: 
                with self.lock: self.frame = frame
                with state_lock: STATE["pipeline"] = True
            else: with state_lock: STATE["pipeline"] = False
    def get_frame(self):
        with self.lock: return self.frame.copy() if self.frame is not None else None

class InferenceStream:
    def __init__(self, vs):
        self.running = True; self.vs = vs; self.tracker = SimpleTracker(); self._votos = []
    def update(self):
        t_fps = time.time(); cnt = 0
        while self.running:
            img = self.vs.get_frame()
            if img is None: time.sleep(0.05); continue
            try:
                results = model.predict(img, conf=MODEL_CONF, device=0, verbose=False)
                annotated = results[0].plot()
                
                # Gestión de FPS
                cnt += 1
                if time.time() - t_fps >= 1.0:
                    with state_lock: STATE["fps"] = round(cnt / (time.time() - t_fps), 1)
                    cnt = 0; t_fps = time.time()

                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0]); conf = float(box.conf[0])
                    crop = img[max(0, y1-10):y2+10, max(0, x1-20):x2+20]
                    leido = leer_numero(crop)
                    if not leido: continue

                    # Lógica de Votación
                    self._votos = [v for v in self._votos if time.time() - v[0] < VOTO_WINDOW]
                    self._votos.append((time.time(), leido))
                    ganador, veces = Counter(v[1] for v in self._votos).most_common(1)[0]
                    if veces < N_VOTOS: continue
                    
                    valido, est = validar_numero(ganador)
                    if not valido: valido, est = recuperar_ocr(ganador)
                    
                    if valido:
                        res = self.tracker.update((x1,y1,x2,y2), valido, est, conf)
                        if res == 'NEW':
                            db_insert(ganador, valido, conf, estado=est)
                            alerta_unidad_detectada(None, valido, conf, datetime.now().strftime("%H:%M:%S"))
                
                with state_lock:
                    if self.tracker.active:
                        STATE["tracking"] = self.tracker.active['numero']
                        STATE["tracking_duracion"] = self.tracker.get_duration()

            except Exception as e: print(f"[ERROR INF] {e}")

vs = VideoStream(); inf = InferenceStream(vs)
threading.Thread(target=vs.update, daemon=True).start()
threading.Thread(target=inf.update, daemon=True).start()

if __name__ == '__main__':
    app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True)