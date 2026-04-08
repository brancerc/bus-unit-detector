"""
CISA – pipe_stream_v2.py  (dual-camera + parámetros por cámara)
===============================================================
Cam 1 (H.264, 192.168.10.2): detección principal + HLS streaming
Cam 2 (H.265, 192.168.10.4): detección secundaria, ventana más amplia

Parámetros por cámara:
  CAMERA_CONF     — confianza mínima YOLO (Cam2 más permisiva)
  CAMERA_BBOX_MIN — bbox mínimo en px   (Cam2 acepta detecciones más lejanas)
  CAMERA_PUERTA   — id_puerta en SQLite
  CAMERA_LABEL    — etiqueta en Telegram y dashboard

Arquitectura de hilos:
  Hilo 1:  VideoStream        — captura Cam 1 (H.264)
  Hilo 2:  VideoStream2       — captura Cam 2 (H.265), arranca 8s después
  Hilo 3:  hls_frame_producer — frames RAW → cola HLS
  Hilo 4:  hls_frame_writer   — cola HLS → ffmpeg stdin
  Hilo 5:  hls_watchdog       — mata ffmpeg si no hay clientes
  Hilo 6:  InferenceStream    — YOLO alterna Cam1/Cam2 frame a frame
  Hilo 7:  _ocr_worker        — CNN OCR + validación + tracker por cámara
  Hilo 8:  _refresh_unidades  — refresco PostgreSQL cada 60s
"""

import cv2
import glob
import threading
import queue
import time
import subprocess
import os
import sqlite3
import re
import pytesseract
import psycopg2
from collections import Counter, deque
from datetime import datetime
from ultralytics import YOLO
from flask import Flask, send_file, jsonify, request, make_response, render_template

from config import (
    PIPELINE, HLS_DIR, FRAMES_DIR, DESCONOCIDAS_DIR, CROPS_DIR, CLEAN_DIR,
    CLEAN_LATERAL_DIR, CLEAN_TRASERO_DIR, DB_PATH,
    PG_CONFIG, MODEL_PATH, MODEL_CONF, COOLDOWN_SEG, N_VOTOS, VOTO_WINDOW,
    OCR_TARGET_H, OCR_MIN_SIZE, OCR_MAX_DIGITS, OCR_MIN_DIGITS,
    LEVENSHTEIN_MAX, ID_PUERTA, HLS_TIMEOUT, HLS_RESOLUTION, HLS_FPS,
    FLASK_HOST, FLASK_PORT, PG_REFRESH_INTERVAL
)
from alertas import (
    alerta_unidad_desconocida,
    enviar_a_validador,
    actualizar_pendiente,
    set_validation_callback,
)

app = Flask(__name__, template_folder=os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "templates"))
print(f"[INFO] Templates: {app.template_folder}")


# ══════════════════════════════════════════════════════════════════════════════
# PARÁMETROS POR CÁMARA
# Ajusta aquí según el ángulo y distancia de cada cámara.
# ══════════════════════════════════════════════════════════════════════════════

# Confianza mínima YOLO por cámara.
# Cam 1: buses pasan cerca → 0.70 elimina falsos positivos.
# Cam 2: buses más lejos → 0.60 captura detecciones válidas que Cam1 descartaría.
CAMERA_CONF = {
    1: 0.70,
    2: 0.60,
}

# Tamaño mínimo del bbox (ancho Y alto) en píxeles antes de pasar al OCR.
# Cam 1: 50px (original, buses cerca).
# Cam 2: 25px — permite leer números a mayor distancia.
# Si hay muchos falsos positivos en Cam 2, subir a 30-35px.
CAMERA_BBOX_MIN = {
    1: 50,
    2: 15,
}

# ID de puerta en SQLite (debe coincidir con la tabla `puerta` en PostgreSQL).
CAMERA_PUERTA = {1: 1, 2: 2}

# Etiqueta visual en Telegram y dashboard.
CAMERA_LABEL = {1: "Cam 1", 2: "Cam 2"}

# ── Debug de bbox (útil para calibrar CAMERA_BBOX_MIN en Cam 2) ───────────────
# Poner True para ver en logs el tamaño de cada bbox detectado por YOLO.
# Deja False en producción para no saturar los logs.
BBOX_DEBUG = True

# Confianza mínima global para model.predict() — siempre el mínimo entre cámaras,
# el filtro por cámara se aplica después sobre cada box individualmente.
_CONF_GLOBAL = min(CAMERA_CONF.values())   # 0.60


# ── Pipelines por cámara ──────────────────────────────────────────────────────

def _build_pipeline_h264(ip):
    rtsp = f"rtsp://admin:PatioCCA_@{ip}:554/cam/realmonitor?channel=1&subtype=1"
    return (
        f"rtspsrc location={rtsp} latency=100 ! "
        "rtph264depay ! h264parse ! nvv4l2decoder ! "
        "nvvidconv ! video/x-raw,format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! appsink"
    )


def _build_pipeline_h265(ip):
    # H.265/HEVC con parámetros de baja latencia para Cam 2
    rtsp = f"rtsp://admin:PatioCCA_@{ip}:554/cam/realmonitor?channel=1&subtype=1"
    return (
        f"rtspsrc location={rtsp} latency=200 drop-on-latency=true ! "
        "rtph265depay ! h265parse ! nvv4l2decoder ! "
        "nvvidconv ! video/x-raw,format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! "
        "appsink sync=false max-buffers=1 drop=true"
    )


CAMERA_PIPELINES = {
    1: PIPELINE,
    2: _build_pipeline_h265("192.168.10.4"),
}

_active_camera = 1   # cámara activa para HLS


# ── Helpers de clase ──────────────────────────────────────────────────────────

def _es_lateral(c):   return 'lateral'   in c.lower()
def _es_trasero(c):   return 'trasero'   in c.lower() or 'trasera' in c.lower()
def _es_delantero(c): return 'delantero' in c.lower() or 'delantera' in c.lower()

def _clean_dir_for_class(cls_name):
    if _es_lateral(cls_name): return CLEAN_LATERAL_DIR
    if _es_trasero(cls_name): return CLEAN_TRASERO_DIR
    return None


# ── Tracker IoU ───────────────────────────────────────────────────────────────

class SimpleTracker:
    IOU_THRESHOLD = 0.20
    GONE_TIMEOUT  = 30.0

    def __init__(self):
        self.active = None

    @staticmethod
    def iou(a, b):
        xi1, yi1 = max(a[0], b[0]), max(a[1], b[1])
        xi2, yi2 = min(a[2], b[2]), min(a[3], b[3])
        inter  = max(0, xi2 - xi1) * max(0, yi2 - yi1)
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        area_b = (b[2] - b[0]) * (b[3] - b[1])
        union  = area_a + area_b - inter
        return inter / union if union > 0 else 0

    def update(self, bbox, numero, estado, conf):
        now = time.time()
        if self.active:
            same_bus = (self.iou(bbox, self.active['bbox']) > self.IOU_THRESHOLD
                        or numero == self.active['numero'])
            if same_bus:
                self.active['bbox']      = bbox
                self.active['last_seen'] = now
                if conf > self.active['conf']:
                    self.active['conf'] = conf
                return 'TRACKING'
            print(f"[TRACKER] Ignorando '{numero}' — activo: {self.active['numero']}")
            self.active['last_seen'] = now
            return 'TRACKING'
        self.active = {
            'numero': numero, 'estado': estado, 'conf': conf,
            'bbox': bbox, 'first_seen': now, 'last_seen': now,
        }
        return 'NEW'

    def check_gone(self):
        if self.active is None:
            return None, 0
        if time.time() - self.active['last_seen'] > self.GONE_TIMEOUT:
            duration = round(self.active['last_seen'] - self.active['first_seen'], 1)
            track = self.active.copy()
            self.active = None
            return track, duration
        return None, 0

    def get_duration(self):
        return round(time.time() - self.active['first_seen'], 1) if self.active else 0


# ── PostgreSQL ────────────────────────────────────────────────────────────────

UNIDADES       = set()
_unidades_lock = threading.Lock()
_pg_connected  = False


def cargar_unidades_pg():
    global UNIDADES, _pg_connected
    try:
        con = psycopg2.connect(**PG_CONFIG)
        cur = con.cursor()
        cur.execute("SELECT no_economico FROM unidad WHERE estado = true")
        nuevas = {str(r[0]).strip() for r in cur.fetchall()}
        cur.close(); con.close()
        with _unidades_lock:
            if nuevas != UNIDADES:
                agr = nuevas - UNIDADES; eli = UNIDADES - nuevas
                if agr: print(f"[PG] +{sorted(agr)}")
                if eli: print(f"[PG] -{sorted(eli)}")
                UNIDADES = nuevas
        _pg_connected = True
        print(f"[PG] {len(nuevas)} unidades activas")
        return True
    except Exception as e:
        _pg_connected = False; print(f"[PG ERROR] {e}"); return False


def _refresh_unidades_loop():
    while True:
        cargar_unidades_pg()
        time.sleep(PG_REFRESH_INTERVAL)


print("=" * 60)
cargar_unidades_pg()
threading.Thread(target=_refresh_unidades_loop, daemon=True).start()
print("=" * 60)


# ── SQLite ────────────────────────────────────────────────────────────────────

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
    print("[DB] SQLite lista.")


def db_query(fecha=None, limit=50):
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        q = ("SELECT * FROM evento_paso WHERE date(hora_paso)=? ORDER BY id_evento DESC LIMIT ?"
             if fecha else
             "SELECT * FROM evento_paso ORDER BY id_evento DESC LIMIT ?")
        rows = con.execute(q, (fecha, limit) if fecha else (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB ERROR] {e}"); return []


def db_stats():
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        hoy   = datetime.now().strftime("%Y-%m-%d")
        total = con.execute("SELECT COUNT(*) FROM evento_paso").fetchone()[0]
        hoy_c = con.execute("SELECT COUNT(*) FROM evento_paso WHERE date(hora_paso)=?", (hoy,)).fetchone()[0]
        ult   = con.execute("SELECT * FROM evento_paso ORDER BY id_evento DESC LIMIT 1").fetchone()
        con.close()
        return {"total": total, "hoy": hoy_c, "ultima": dict(ult) if ult else None}
    except Exception as e:
        print(f"[DB ERROR] {e}"); return {"total": 0, "hoy": 0, "ultima": None}


init_db()


# ── Validación PG ─────────────────────────────────────────────────────────────

def levenshtein(a, b):
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            dp[j] = prev[j-1] if a[i-1] == b[j-1] else 1 + min(prev[j], dp[j-1], prev[j-1])
    return dp[n]


def _mejor_lev(candidato, conjunto):
    mejor, dist = None, 999
    for u in conjunto:
        d = levenshtein(candidato, u)
        if d < dist: dist = d; mejor = u
    return (mejor, "CORREGIDO") if dist <= LEVENSHTEIN_MAX else (None, None)


def _corregir_trasposicion(leido, uds):
    for i in range(len(leido) - 1):
        t = leido[:i] + leido[i+1] + leido[i] + leido[i+2:]
        if t in uds:
            print(f"[CORREGIDO] '{leido}' → '{t}' (trasposición)")
            return t, "CORREGIDO"
    return None, None


def _corregir_digito_duplicado(leido, uds):
    for i in range(1, len(leido) - 1):
        if leido[i] == leido[i+1]:
            s = leido[:i+1] + leido[i+2:]
            if len(s) == 3:
                c = s[0] + '0' + s[1:]
                if c in uds:
                    print(f"[CORREGIDO] '{leido}' → '{c}' (dígito duplicado)")
                    return c, "CORREGIDO"
    return None, None


def validar_numero(leido):
    leido = leido.strip()
    with _unidades_lock:
        snap = UNIDADES.copy()
    if not snap:
        return None, None
    u4d5 = {u for u in snap if len(u) == 4 and u.startswith('5')}

    if len(leido) == 4 and leido.startswith('5'):
        if leido in u4d5: return leido, "VERIFICADO"
        r, e = _mejor_lev(leido, u4d5)
        if r: return r, e
        r, e = _corregir_trasposicion(leido, u4d5)
        if r: return r, e
        r, e = _corregir_digito_duplicado(leido, u4d5)
        if r: return r, e
        return None, None

    if len(leido) == 3 and leido[0] == '5':
        if leido in snap: return leido, "VERIFICADO"
        c4 = leido[0] + '0' + leido[1:]
        if c4 in u4d5: return c4, "CORREGIDO"
        return _mejor_lev(c4, u4d5)

    if len(leido) == 4 and leido[0] != '5':
        n = '5' + leido[1:]
        if n in u4d5: return n, "CORREGIDO"
        r, e = _mejor_lev(n, u4d5)
        if r: return r, e
        return _corregir_trasposicion(n, u4d5)

    if len(leido) == 3 and leido[0] != '5':
        c = '5' + leido
        if c in u4d5: return c, "CORREGIDO"
        r, e = _mejor_lev(c, u4d5)
        if r: return r, e
        c50 = '50' + leido[1:]
        if c50 in u4d5: return c50, "CORREGIDO"
        if leido in snap: return leido, "VERIFICADO"
        return None, None

    if leido in snap: return leido, "VERIFICADO"
    return None, None


def recuperar_ocr(leido):
    leido = leido.strip()
    with _unidades_lock:
        snap = UNIDADES.copy()
    if not snap:
        return None, None
    u4d5 = {u for u in snap if len(u) == 4 and u.startswith('5')}
    if   len(leido) == 3 and leido[0] == '5': n = leido[0] + '0' + leido[1:]
    elif len(leido) == 3 and leido[0] != '5': n = '50' + leido[1:]
    elif len(leido) == 4 and leido[0] != '5': n = '5' + leido[1:]
    else:                                       n = leido
    if len(n) != 4 or not n.startswith('5'):
        return None, None
    if n[1] != '0':
        c = '50' + n[2:]
        if c in u4d5: return c, "CORREGIDO"
        r, e = _mejor_lev(c, u4d5)
        if r: return r, e
    return None, None


# ── OCR: CNN TensorRT (primario) + Tesseract (fallback) ──────────────────────
print("[INFO] Tesseract OCR listo.")


def _es_noche():
    h = datetime.now().hour
    return h >= 22 or h <= 6


def leer_numero(crop, cls_name='numero_delantero'):
    """
    Primero CNN TensorRT (~0.35ms), luego Tesseract como fallback (~100ms).
    cls_name disponible para zoom por clase en versiones futuras.
    """
    if ocr_engine is not None:
        try:
            resultado = ocr_engine.leer(crop)
            if resultado:
                return resultado
        except Exception as e:
            print(f"[OCR-CNN] Error, usando Tesseract: {e}")

    try:
        h, w = crop.shape[:2]
        if h < OCR_MIN_SIZE or w < OCR_MIN_SIZE:
            return None

        scale  = max(1, OCR_TARGET_H // h)
        crop_u = cv2.resize(crop, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray   = cv2.cvtColor(crop_u, cv2.COLOR_BGR2GRAY)

        noche = _es_noche()
        gray  = cv2.fastNlMeansDenoising(
            gray, h=(18 if noche else 12), templateWindowSize=7, searchWindowSize=21)
        alpha = 2.2 if noche else 1.8
        blur  = cv2.GaussianBlur(gray, (0, 0), 2.0)
        gray  = cv2.addWeighted(gray, alpha, blur, -(alpha - 1), 0)

        kern  = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        clahe = cv2.createCLAHE(clipLimit=(4.0 if noche else 3.0), tileGridSize=(4, 4))
        g1 = clahe.apply(gray)
        _, g1 = cv2.threshold(g1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        g1 = cv2.morphologyEx(g1, cv2.MORPH_CLOSE, kern)
        g1 = cv2.morphologyEx(g1, cv2.MORPH_OPEN,  kern)

        g2 = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 8)
        g2 = cv2.morphologyEx(g2, cv2.MORPH_CLOSE, kern)

        cfg7 = '--psm 7 -c tessedit_char_whitelist=0123456789'
        resultados = []
        for img_proc in [g1, g2]:
            txt   = pytesseract.image_to_string(img_proc, config=cfg7).strip()
            clean = re.sub(r'[^0-9]', '', txt)[:OCR_MAX_DIGITS]
            if len(clean) >= OCR_MIN_DIGITS:
                resultados.append(clean)

        if not resultados:
            cfg8 = '--psm 8 -c tessedit_char_whitelist=0123456789'
            for img_proc in [g1, g2]:
                txt   = pytesseract.image_to_string(img_proc, config=cfg8).strip()
                clean = re.sub(r'[^0-9]', '', txt)[:OCR_MAX_DIGITS]
                if len(clean) >= OCR_MIN_DIGITS:
                    resultados.append(clean)

        return Counter(resultados).most_common(1)[0][0] if resultados else None

    except Exception as e:
        print(f"[OCR ERROR] {e}"); return None


# ── Motor OCR CNN TensorRT ────────────────────────────────────────────────────
ocr_engine = None
try:
    from inferencia_trt import OcrEngine
    ocr_engine = OcrEngine('ocr_cnn.engine')
    print("[INFO] Motor OCR CNN listo — ~0.35ms/crop (Tesseract: ~100ms)")
except Exception as e:
    print(f"[INFO] OCR CNN no disponible, usando Tesseract: {e}")


# ── Modelo TensorRT YOLO ──────────────────────────────────────────────────────
print("[INFO] Cargando modelo TensorRT...")
model = YOLO(MODEL_PATH, task="detect")
print(f"[INFO] Modelo: {model.names}")
print(f"[INFO] YOLO conf global: {_CONF_GLOBAL} "
      f"(Cam1={CAMERA_CONF[1]}, Cam2={CAMERA_CONF[2]})")
print(f"[INFO] Bbox mínimo: Cam1={CAMERA_BBOX_MIN[1]}px, Cam2={CAMERA_BBOX_MIN[2]}px")


# ── Frames compartidos ────────────────────────────────────────────────────────

_latest_frame_raw      = None
_latest_frame_raw_lock = threading.Lock()

_latest_frames      = {1: None, 2: None}
_latest_frames_lock = threading.Lock()


def set_latest_frame_raw(frame):
    global _latest_frame_raw
    with _latest_frame_raw_lock:
        _latest_frame_raw = frame

def get_latest_frame_raw():
    with _latest_frame_raw_lock:
        return _latest_frame_raw

def set_latest_frame_cam(cam_id, frame):
    with _latest_frames_lock:
        _latest_frames[cam_id] = frame

def get_latest_frame_cam(cam_id):
    with _latest_frames_lock:
        f = _latest_frames[cam_id]
        return f.copy() if f is not None else None


# ── Detecciones recientes (dashboard) ─────────────────────────────────────────

_detecciones_recent = deque(maxlen=30)
_detecciones_lock   = threading.Lock()


def _on_validation_update(msg_id, nuevo_estado):
    with _detecciones_lock:
        for entry in _detecciones_recent:
            if entry.get('msg_id') == msg_id:
                entry['estado'] = nuevo_estado
                print(f"[TILE] msg_id={msg_id} → {nuevo_estado}")
                return


set_validation_callback(_on_validation_update)


# ── Estado global ─────────────────────────────────────────────────────────────

STATE = {
    "numero": None, "conf": None, "ts": None,
    "fps": 0.0, "yolo_fps": 0.0,
    "pipeline": False, "pipeline_cam2": False,
    "total_detecciones": 0, "unidades_registradas": 0,
    "pg_conectada": False, "descartadas": 0,
    "tracking":           None, "tracking_duracion":      0,
    "tracking_cam2":      None, "tracking_duracion_cam2": 0,
    "active_camera": 1,
    "switching":     False,
}
state_lock = threading.Lock()


# ── HLS bajo demanda ──────────────────────────────────────────────────────────

hls_lock         = threading.Lock()
hls_last_ping    = 0
hls_proc         = None
hls_active       = False
_hls_write_queue = queue.Queue(maxsize=1)


def hls_start():
    global hls_proc, hls_active
    if hls_active: return
    print("[HLS] Iniciando stream...")
    for f in glob.glob(os.path.join(HLS_DIR, "*")):
        try: os.remove(f)
        except OSError: pass
    w, h = HLS_RESOLUTION
    cmd = [
        'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(HLS_FPS), '-i', '-',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-g', str(HLS_FPS), '-sc_threshold', '0',
        '-f', 'hls', '-hls_time', '1', '-hls_list_size', '4',
        '-hls_flags', 'delete_segments+append_list+omit_endlist',
        '-hls_segment_type', 'mpegts',
        '-hls_segment_filename', f'{HLS_DIR}/seg%03d.ts',
        f'{HLS_DIR}/stream.m3u8',
    ]
    hls_proc   = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hls_active = True


def hls_stop():
    global hls_proc, hls_active
    if not hls_active: return
    print("[HLS] Deteniendo stream.")
    try:
        hls_proc.stdin.close()
        hls_proc.wait(timeout=3)
    except Exception:
        hls_proc.kill()
    hls_proc = None; hls_active = False


def hls_watchdog():
    while True:
        time.sleep(5)
        with hls_lock:
            if hls_active and (time.time() - hls_last_ping) > HLS_TIMEOUT:
                hls_stop()

threading.Thread(target=hls_watchdog, daemon=True).start()


def hls_frame_producer():
    """Produce frames RAW a HLS_FPS sin bloquear nunca."""
    interval = 1.0 / HLS_FPS
    while True:
        t0 = time.time()
        with state_lock:
            is_switching = STATE.get("switching", False)
        if is_switching:
            while not _hls_write_queue.empty():
                try: _hls_write_queue.get_nowait()
                except queue.Empty: break
        if hls_active:
            frame = get_latest_frame_raw()
            if frame is not None:
                try:
                    _hls_write_queue.put_nowait(cv2.resize(frame, HLS_RESOLUTION).tobytes())
                except queue.Full:
                    pass
        rem = interval - (time.time() - t0)
        if rem > 0: time.sleep(rem)


def hls_frame_writer():
    """Escribe a ffmpeg stdin en hilo aislado."""
    while True:
        data = _hls_write_queue.get()
        if not hls_active or hls_proc is None:
            continue
        try:
            hls_proc.stdin.write(data)
        except BrokenPipeError:
            with hls_lock: hls_stop()
        except Exception:
            pass


threading.Thread(target=hls_frame_producer, daemon=True).start()
threading.Thread(target=hls_frame_writer,   daemon=True).start()


# ── VideoStream base ──────────────────────────────────────────────────────────

class VideoStream:
    """Captura de cámara genérica. Publica en _latest_frames[cam_id]."""

    def __init__(self, cam_id=1, start_delay=3):
        self.cam_id      = cam_id
        self.lock        = threading.Lock()
        self.frame       = None
        self.running     = True
        self.cap         = None
        self._pipeline   = CAMERA_PIPELINES[cam_id]
        self._start_delay = start_delay

    def _abrir_pipeline(self):
        print(f"[VS{self.cam_id}] Abriendo pipeline...")
        if self.cap:
            try: self.cap.release()
            except: pass
        time.sleep(2)
        self.cap = cv2.VideoCapture(self._pipeline, cv2.CAP_GSTREAMER)
        ok  = self.cap.isOpened()
        key = "pipeline" if self.cam_id == 1 else "pipeline_cam2"
        with state_lock: STATE[key] = ok
        print(f"[VS{self.cam_id}] Pipeline {'OK' if ok else 'FALLO'}")

    def switch_pipeline(self, nuevo_pipeline):
        """Cambio de fuente en caliente (solo afecta HLS)."""
        print(f"[VS{self.cam_id}] EXPERIMENTAL — switch de pipeline")
        self._pipeline = nuevo_pipeline
        with state_lock:
            STATE["pipeline"] = False
            STATE["switching"] = True
        if self.cap:
            try: self.cap.release()
            except: pass
        self.cap = None

    def update(self):
        time.sleep(self._start_delay)
        self._abrir_pipeline()
        while self.running:
            if not self.cap or not self.cap.isOpened():
                time.sleep(2); self._abrir_pipeline(); continue
            ret, frame = self.cap.read()
            if ret:
                key = "pipeline" if self.cam_id == 1 else "pipeline_cam2"
                with state_lock:
                    STATE[key] = True
                    if self.cam_id == 1:
                        STATE["switching"] = False
                with self.lock: self.frame = frame
                set_latest_frame_cam(self.cam_id, frame)
                if self.cam_id == _active_camera:
                    set_latest_frame_raw(frame)
            else:
                key = "pipeline" if self.cam_id == 1 else "pipeline_cam2"
                with state_lock: STATE[key] = False
                time.sleep(2); self._abrir_pipeline()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False
        if self.cap: self.cap.release()


# ── Estado por cámara ─────────────────────────────────────────────────────────

def _init_cam_state(cam_id):
    return {
        'cam_id':               cam_id,
        'puerta_id':            CAMERA_PUERTA[cam_id],
        'label':                CAMERA_LABEL[cam_id],
        'tracker':              SimpleTracker(),
        'track_votes':          [],
        'track_numero_inicial': None,
        'track_no_detectado':   None,
        'track_msg_id':         None,
        'track_ts':             None,
        'track_frame':          None,
        'best_train_area':      0,
        'best_train_frame':     None,
        'best_train_cls':       None,
        'pending_desc':         None,
        'pending_desc_timer':   None,
    }


# ── InferenceStream ───────────────────────────────────────────────────────────

class InferenceStream:
    """
    YOLO compartido que alterna frames Cam1↔Cam2.
    Confianza y bbox mínimo se aplican por cámara después de la inferencia.
    """

    def __init__(self, vs1, vs2):
        self.running   = True
        self.vs1       = vs1
        self.vs2       = vs2
        self.yolo_fps  = 0.0
        self._cnt      = 0
        self._t        = time.time()
        self._cam_turn = 1

        # Cola compartida: (img, crop, bbox, conf, cls_name, cam_id)
        self._ocr_queue = queue.Queue(maxsize=10)

        # Votos temporales por cámara
        self._votos = {1: [], 2: []}

        # Estado completo por cámara
        self._cam_state = {1: _init_cam_state(1), 2: _init_cam_state(2)}
        self._cam_lock  = threading.Lock()

        threading.Thread(target=self._ocr_worker, daemon=True).start()
        print("[INFO] Hilo OCR dual-cam iniciado.")

    # ── Cerrar track ──────────────────────────────────────────────────────────

    def _cerrar_track(self, track, duracion, cam_id):
        with self._cam_lock:
            st = self._cam_state[cam_id]
            if not track or not st['track_ts']:
                return
            votes          = st['track_votes'].copy()
            numero_inicial = st['track_numero_inicial']
            msg_id         = st['track_msg_id']
            best_frame     = st['best_train_frame']
            best_cls       = st['best_train_cls']

        winner_num    = numero_inicial or track['numero']
        winner_conf   = track['conf']
        winner_estado = track['estado']

        if votes and numero_inicial:
            conteo = Counter(n for n, c, e in votes)
            w_num, w_count = conteo.most_common(1)[0]
            total = len(votes)
            print(f"[MULTI-LECTURA Cam{cam_id}] {total} votos → {w_num} ({w_count}/{total})")
            winner_entries = [(c, e) for n, c, e in votes if n == w_num]
            winner_num    = w_num
            winner_conf   = max(c for c, e in winner_entries)
            winner_estado = winner_entries[0][1]
            if w_num != numero_inicial:
                print(f"[MULTI-LECTURA Cam{cam_id}] Corrección: {numero_inicial} → {w_num}")

        actualizar_pendiente(msg_id, winner_num, winner_conf, winner_estado, duracion)

        if best_frame is not None and best_cls is not None:
            dest = _clean_dir_for_class(best_cls)
            if dest:
                def _save(fr, d, num, cls_n, cid):
                    name = (f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                            f"_CAM{cid}_{num}_{cls_n}.jpg")
                    cv2.imwrite(os.path.join(d, name), fr)
                    print(f"[TRAIN Cam{cid}] {cls_n}/{num} → {d}")
                threading.Thread(target=_save,
                    args=(best_frame, dest, winner_num, best_cls, cam_id),
                    daemon=True).start()

        with self._cam_lock:
            st['track_votes']          = []
            st['track_numero_inicial'] = None
            st['track_no_detectado']   = None
            st['track_msg_id']         = None
            st['track_ts']             = None
            st['track_frame']          = None
            st['best_train_area']      = 0
            st['best_train_frame']     = None
            st['best_train_cls']       = None

        tk  = "tracking"          if cam_id == 1 else "tracking_cam2"
        dtk = "tracking_duracion" if cam_id == 1 else "tracking_duracion_cam2"
        with state_lock:
            STATE[tk]  = None
            STATE[dtk] = 0

    # ── OCR worker ────────────────────────────────────────────────────────────

    def _ocr_worker(self):
        while self.running:
            try:
                job = self._ocr_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            img, crop, bbox, conf, cls_name, cam_id = job

            try:
                numero_leido = leer_numero(crop, cls_name)
                if not numero_leido:
                    continue

                # Votación temporal por cámara
                now = time.time()
                self._votos[cam_id] = [
                    (t, n, c) for t, n, c in self._votos[cam_id]
                    if now - t < VOTO_WINDOW]
                self._votos[cam_id].append((now, numero_leido, conf))
                ganador, veces = Counter(
                    n for _, n, _ in self._votos[cam_id]).most_common(1)[0]

                n_req = 3 if _es_noche() else N_VOTOS
                if veces < n_req:
                    continue

                numero_leido = ganador
                self._votos[cam_id] = []

                numero_valido, estado = validar_numero(numero_leido)
                if numero_valido is None:
                    numero_valido, estado = recuperar_ocr(numero_leido)

                with self._cam_lock:
                    st = self._cam_state[cam_id]

                # ── DESCONOCIDO ──────────────────────────────────────────────
                if numero_valido is None:
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fp = os.path.join(DESCONOCIDAS_DIR,
                         f"{ts_str}_CAM{cam_id}_DESCONOCIDA_{numero_leido}.jpg")
                    threading.Thread(target=cv2.imwrite,
                        args=(fp, img.copy()), daemon=True).start()
                    with state_lock: STATE["descartadas"] += 1
                    print(f"[DESCONOCIDA Cam{cam_id}] '{numero_leido}'")

                    with self._cam_lock:
                        if st['pending_desc_timer']:
                            st['pending_desc_timer'].cancel()

                    ts_now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    pending = (fp, numero_leido, conf, ts_now, CAMERA_LABEL[cam_id])

                    def _enviar(datos, cid):
                        fp_, num_, conf_, ts_, lbl_ = datos
                        alerta_unidad_desconocida(fp_, num_, conf_, ts_,
                                                  camera_label=lbl_)
                        with self._cam_lock:
                            self._cam_state[cid]['pending_desc'] = None

                    timer = threading.Timer(4.0, _enviar, args=[pending, cam_id])
                    timer.daemon = True
                    timer.start()
                    with self._cam_lock:
                        st['pending_desc']       = pending
                        st['pending_desc_timer'] = timer
                    continue

                # ── NÚMERO VÁLIDO → TRACKER ───────────────────────────────────
                with self._cam_lock:
                    if st['pending_desc_timer']:
                        st['pending_desc_timer'].cancel()
                        st['pending_desc_timer'] = None
                        st['pending_desc']       = None
                    track_result = st['tracker'].update(bbox, numero_valido, estado, conf)

                if track_result == 'TRACKING':
                    with self._cam_lock:
                        st['track_votes'].append((numero_valido, conf, estado))
                        n_acum = len(st['track_votes'])
                    if n_acum % 5 == 0:
                        active = st['tracker'].active['numero'] if st['tracker'].active else '?'
                        print(f"[MULTI-LECTURA Cam{cam_id}] {n_acum} votos | {active}")
                    continue

                # ── NEW ───────────────────────────────────────────────────────
                ts_str     = datetime.now().strftime("%Y%m%d_%H%M%S")
                frame_name = f"{ts_str}_CAM{cam_id}_{numero_valido}.jpg"
                frame_path = os.path.join(FRAMES_DIR, frame_name)

                cv2.imwrite(frame_path, img.copy())   # sincrono

                track_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                thumb_name = f"{ts_str}_CAM{cam_id}_{numero_valido}_thumb.jpg"
                try:
                    th = cv2.resize(img, (320, 240))
                    cv2.imwrite(os.path.join(CROPS_DIR, thumb_name),
                                th, [cv2.IMWRITE_JPEG_QUALITY, 65])
                except Exception:
                    thumb_name = None

                with self._cam_lock:
                    st['track_votes']          = [(numero_valido, conf, estado)]
                    st['track_numero_inicial']  = numero_valido
                    st['track_no_detectado']    = numero_leido
                    st['track_msg_id']          = None
                    st['track_ts']              = track_ts
                    st['track_frame']           = frame_name

                tk  = "tracking"          if cam_id == 1 else "tracking_cam2"
                dtk = "tracking_duracion" if cam_id == 1 else "tracking_duracion_cam2"
                with state_lock:
                    STATE["numero"]           = numero_valido
                    STATE["conf"]             = round(conf, 3)
                    STATE["ts"]               = track_ts
                    STATE["total_detecciones"] += 1
                    STATE[tk]                 = numero_valido
                    STATE[dtk]                = 0

                print(f"[NEW {estado} Cam{cam_id}] {numero_valido} | "
                      f"conf={conf:.2f} | {cls_name} | "
                      f"{'NOCHE' if _es_noche() else 'DIA'}")

                det_entry = {
                    "msg_id":    None,
                    "numero":    numero_valido,
                    "leido":     numero_leido,
                    "estado":    "pendiente",
                    "conf":      round(conf, 3),
                    "ts":        track_ts,
                    "cls":       cls_name,
                    "cam_id":    cam_id,
                    "cam_label": CAMERA_LABEL[cam_id],
                    "frame_url": f"/frames/{frame_name}",
                    "thumb_url": f"/frames/crops/{thumb_name}" if thumb_name else None,
                }
                with _detecciones_lock:
                    _detecciones_recent.appendleft(det_entry)

                msg_id = enviar_a_validador(
                    frame_path, numero_valido, conf, track_ts, estado,
                    no_detectado=numero_leido,
                    thumb_name=thumb_name,
                    puerta_id=CAMERA_PUERTA[cam_id],
                    camera_label=CAMERA_LABEL[cam_id])

                if msg_id:
                    det_entry['msg_id'] = msg_id
                    with self._cam_lock:
                        st['track_msg_id'] = msg_id

            except Exception as e:
                print(f"[OCR ERROR Cam{cam_id}] {e}")

    # ── YOLO update — alterna Cam1 ↔ Cam2 ────────────────────────────────────

    def update(self):
        """
        Inferencia YOLO alternando cámaras.
        Confianza global = min(cámaras) para que YOLO no descarte antes de tiempo.
        El filtro fino por confianza y bbox se aplica por cámara después.
        """
        while self.running:
            cam_id = self._cam_turn
            self._cam_turn = 2 if self._cam_turn == 1 else 1

            img = get_latest_frame_cam(cam_id)
            if img is None:
                time.sleep(0.05)
                continue

            try:
                # Confianza global (mínimo entre cámaras) para que YOLO
                # no descarte detecciones lejanas de Cam 2 prematuramente
                results = model.predict(img, conf=_CONF_GLOBAL,
                                        device=0, verbose=False)

                self._cnt += 1
                elapsed = time.time() - self._t
                if elapsed >= 1.0:
                    self.yolo_fps = round(self._cnt / elapsed, 1)
                    self._cnt = 0; self._t = time.time()
                    with state_lock:
                        STATE["yolo_fps"]             = self.yolo_fps
                        STATE["pg_conectada"]         = _pg_connected
                        with _unidades_lock:
                            STATE["unidades_registradas"] = len(UNIDADES)

                # Verificar buses salidos en ambas cámaras
                for cid in [1, 2]:
                    with self._cam_lock:
                        gone, dur = self._cam_state[cid]['tracker'].check_gone()
                    if gone:
                        threading.Thread(target=self._cerrar_track,
                            args=(gone, dur, cid), daemon=True).start()

                # Actualizar tracking en STATE
                for cid in [1, 2]:
                    tk  = "tracking"          if cid == 1 else "tracking_cam2"
                    dtk = "tracking_duracion" if cid == 1 else "tracking_duracion_cam2"
                    with self._cam_lock:
                        tr = self._cam_state[cid]['tracker']
                        if tr.active:
                            with state_lock:
                                STATE[tk]  = tr.active['numero']
                                STATE[dtk] = tr.get_duration()

                img_snap     = img.copy()
                cam_conf_min = CAMERA_CONF[cam_id]
                cam_bbox_min = CAMERA_BBOX_MIN[cam_id]

                for box in results[0].boxes:
                    conf_b = float(box.conf[0])

                    # Filtro de confianza por cámara
                    if conf_b < cam_conf_min:
                        continue

                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    bbox     = (x1, y1, x2, y2)
                    cls_id   = int(box.cls[0])
                    cls_name = model.names.get(cls_id, "numero")

                    bw, bh = x2 - x1, y2 - y1

                    # Debug de bbox (activar con BBOX_DEBUG=True)
                    if BBOX_DEBUG:
                        print(f"[BBOX Cam{cam_id}] {cls_name} "
                              f"bw={bw} bh={bh} conf={conf_b:.2f} "
                              f"min={cam_bbox_min}px "
                              f"{'PASA' if bh >= cam_bbox_min and bw >= cam_bbox_min else 'DESCARTADO'}")

                    # Filtro de aspecto
                    if bw / max(bh, 1) > 3.0 or bw / max(bh, 1) < 0.2:
                        continue

                    # Filtro de bbox mínimo por cámara
                    if bh < cam_bbox_min or bw < cam_bbox_min:
                        continue

                    h_img, w_img = img.shape[:2]
                    crop = img[max(0, y1-10):min(h_img, y2+10),
                               max(0, x1-20):min(w_img, x2+20)]

                    if _es_lateral(cls_name) or _es_trasero(cls_name):
                        area = bw * bh
                        with self._cam_lock:
                            st = self._cam_state[cam_id]
                            if area > st['best_train_area']:
                                st['best_train_area']  = area
                                st['best_train_frame'] = img_snap
                                st['best_train_cls']   = cls_name

                    try:
                        self._ocr_queue.put_nowait(
                            (img_snap, crop.copy(), bbox, conf_b, cls_name, cam_id))
                    except queue.Full:
                        pass

            except Exception as e:
                print(f"[YOLO ERROR] {e}"); time.sleep(0.1)

    def stop(self):
        self.running = False


# ── Iniciar hilos ─────────────────────────────────────────────────────────────

vs1 = VideoStream(cam_id=1, start_delay=3)
vs2 = VideoStream(cam_id=2, start_delay=8)   # Cam 2 arranca 8s después
inference = InferenceStream(vs1, vs2)

threading.Thread(target=vs1.update,       daemon=True).start()
threading.Thread(target=vs2.update,       daemon=True).start()
threading.Thread(target=inference.update, daemon=True).start()

print("[INFO] Hilos iniciados — YOLO dual-cam activo.")
print(f"[INFO] Cam 1: conf≥{CAMERA_CONF[1]}, bbox≥{CAMERA_BBOX_MIN[1]}px")
print(f"[INFO] Cam 2: conf≥{CAMERA_CONF[2]}, bbox≥{CAMERA_BBOX_MIN[2]}px — mayor alcance")
print("[INFO] Cam 2 arranca en 8s para no competir con CUDA al inicio.")


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route('/')
def index(): return render_template('home.html')

@app.route('/livevideo')
def livevideo(): return render_template('dashboard_v2.html')

@app.route('/api/hls-start')
def api_hls_start():
    global hls_last_ping
    with hls_lock: hls_last_ping = time.time(); hls_start()
    return jsonify({"hls": "started"})

@app.route('/api/hls-ping')
def api_hls_ping():
    global hls_last_ping
    with hls_lock:
        hls_last_ping = time.time()
        if not hls_active: hls_start()
    return jsonify({"hls": "alive"})

@app.route('/hls/<path:filename>')
def hls_files(filename):
    filepath = os.path.join(HLS_DIR, filename)
    if not os.path.exists(filepath): return '', 404
    mt = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    resp = make_response(send_file(filepath, mimetype=mt))
    if filename.endswith('.m3u8'):
        resp.headers.update({'Cache-Control': 'no-cache, no-store, must-revalidate',
                             'Pragma': 'no-cache', 'Expires': '0'})
    return resp

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

@app.route('/api/detecciones-recent')
def api_detecciones_recent():
    with _detecciones_lock: pending = list(_detecciones_recent)
    hoy = datetime.now().strftime("%Y-%m-%d")
    return jsonify({"pending": pending, "approved": db_query(fecha=hoy, limit=20)})

@app.route('/api/stats')
def api_stats(): return jsonify(db_stats())

@app.route('/api/unidades')
def api_unidades():
    with _unidades_lock:
        return jsonify({"pg_conectada": _pg_connected,
                        "total": len(UNIDADES), "unidades": sorted(UNIDADES)})

# ── Switch de cámara (HLS únicamente — detección sigue en ambas) ──────────────
@app.route('/api/switch-camera', methods=['POST'])
def api_switch_camera():
    global _active_camera
    try:
        data = request.get_json(force=True) or {}
        num  = int(data.get('camera', 1))
        if num not in CAMERA_PIPELINES:
            return jsonify({"ok": False, "error": f"Cámara {num} no definida"})
        if num == _active_camera:
            return jsonify({"ok": True, "camera": num, "msg": "Ya activa"})
        _active_camera = num
        with state_lock:
            STATE["active_camera"] = num
            STATE["switching"]     = True
        print(f"[CAM] HLS → Cam {num} (detección sigue en ambas cámaras)")
        def _clear():
            time.sleep(2)
            with state_lock: STATE["switching"] = False
        threading.Thread(target=_clear, daemon=True).start()
        return jsonify({"ok": True, "camera": num})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── API de configuración en caliente (sin reiniciar) ─────────────────────────
@app.route('/api/cam-config', methods=['GET'])
def api_cam_config():
    """Ver configuración actual de detección por cámara."""
    return jsonify({
        "CAMERA_CONF":     CAMERA_CONF,
        "CAMERA_BBOX_MIN": CAMERA_BBOX_MIN,
        "BBOX_DEBUG":      BBOX_DEBUG,
        "CONF_GLOBAL":     _CONF_GLOBAL,
    })

@app.route('/api/cam-config', methods=['POST'])
def api_cam_config_update():
    """
    Ajustar parámetros de detección en caliente sin reiniciar el servicio.
    Ejemplo: POST {"cam_id": 2, "bbox_min": 30, "conf": 0.55, "bbox_debug": true}
    """
    global BBOX_DEBUG, _CONF_GLOBAL
    try:
        data    = request.get_json(force=True) or {}
        cam_id  = int(data.get('cam_id', 0))
        changed = []

        if cam_id in CAMERA_CONF and 'conf' in data:
            CAMERA_CONF[cam_id] = float(data['conf'])
            _CONF_GLOBAL = min(CAMERA_CONF.values())
            changed.append(f"Cam{cam_id} conf={CAMERA_CONF[cam_id]}")

        if cam_id in CAMERA_BBOX_MIN and 'bbox_min' in data:
            CAMERA_BBOX_MIN[cam_id] = int(data['bbox_min'])
            changed.append(f"Cam{cam_id} bbox_min={CAMERA_BBOX_MIN[cam_id]}px")

        if 'bbox_debug' in data:
            BBOX_DEBUG = bool(data['bbox_debug'])
            changed.append(f"bbox_debug={BBOX_DEBUG}")

        print(f"[CONFIG] Actualizado: {', '.join(changed)}")
        return jsonify({"ok": True, "changed": changed,
                        "CAMERA_CONF": CAMERA_CONF,
                        "CAMERA_BBOX_MIN": CAMERA_BBOX_MIN,
                        "BBOX_DEBUG": BBOX_DEBUG})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/health')
def health():
    with state_lock:
        return jsonify({
            "status":                 "ok",
            "yolo_fps":               STATE["yolo_fps"],
            "pipeline_cam1":          STATE["pipeline"],
            "pipeline_cam2":          STATE["pipeline_cam2"],
            "hls":                    hls_active,
            "active_camera":          STATE["active_camera"],
            "pg_conectada":           _pg_connected,
            "unidades_pg":            len(UNIDADES),
            "descartadas":            STATE["descartadas"],
            "tracking_cam1":          STATE["tracking"],
            "tracking_duracion_cam1": STATE["tracking_duracion"],
            "tracking_cam2":          STATE["tracking_cam2"],
            "tracking_duracion_cam2": STATE["tracking_duracion_cam2"],
            "ocr_engine":             "CNN+TRT" if ocr_engine else "Tesseract",
            "cam_conf":               dict(CAMERA_CONF),
            "cam_bbox_min":           dict(CAMERA_BBOX_MIN),
        })

@app.route('/cam2')
def cam2_browser():
    """Explorador de videos de Cam 2 para revisión."""
    import glob
    videos = sorted(glob.glob('/media/cisa/JETSON_SD/cam2_grabacion/cam2_2026*.mp4'))
    items  = [os.path.basename(v) for v in videos if os.path.getsize(v) > 10_000_000]
    html   = '<h2 style="font-family:sans-serif">Videos Cam 2</h2><ul style="font-family:monospace">'
    for v in items:
        html += f'<li><a href="/cam2/play/{v}">{v}</a></li>'
    html += '</ul>'
    return html

@app.route('/cam2/play/<filename>')
def cam2_play(filename):
    """Reproduce un video de Cam 2 directamente en el navegador."""
    path = f'/media/cisa/JETSON_SD/cam2_grabacion/{filename}'
    if not os.path.exists(path):
        return 'No encontrado', 404
    return f'''<html><body style="background:#000;margin:0">
    <video controls autoplay style="width:100%;height:100vh"
      src="/cam2/file/{filename}"></video></body></html>'''

@app.route('/cam2/file/<filename>')
def cam2_file(filename):
    path = f'/media/cisa/JETSON_SD/cam2_grabacion/{filename}'
    if not os.path.exists(path):
        return 'No encontrado', 404
    return send_file(path, mimetype='video/mp4')

if __name__ == '__main__':
    print(f"[INFO] http://{FLASK_HOST}:{FLASK_PORT}")
    try:
        app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo...")
        with hls_lock: hls_stop()
        vs1.stop(); vs2.stop(); inference.stop()
