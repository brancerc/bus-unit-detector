"""
CISA – pipe_stream_v2.py
Cambios clave vs v1:
  • HLS: productor + escritor en hilos separados → stdin.write() nunca bloquea el video
  • Thumbnail del frame completo guardado en CROPS_DIR (no se borra con validación)
  • Tiles del dashboard usan URL de thumbnail en vez de base64 del crop
  • Callback de validación actualiza estado de tiles en tiempo real
  • Desconocidas → validador (alertas.py), no al grupo directamente
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
    alerta_unidad_desconocida,
    enviar_a_validador,
    actualizar_pendiente,
    set_validation_callback,
)

app = Flask(__name__)
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

with open(os.path.join(TEMPLATE_DIR, "home.html"),         encoding="utf-8") as f: HOME      = f.read()
with open(os.path.join(TEMPLATE_DIR, "dashboard_v2.html"), encoding="utf-8") as f: DASHBOARD = f.read()

print(f"[INFO] Templates cargados desde {TEMPLATE_DIR}")


# ── Helpers de clase ──────────────────────────────────────────────────────────

def _es_lateral(c):   return 'lateral'   in c.lower()
def _es_trasero(c):   return 'trasero'   in c.lower() or 'trasera'   in c.lower()
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


# ── Tesseract OCR ─────────────────────────────────────────────────────────────
print("[INFO] Tesseract OCR listo.")


def _es_noche():
    h = datetime.now().hour
    return h >= 22 or h <= 6


def leer_numero(crop):
    """2 estrategias: CLAHE+Otsu y Adaptivo Gaussiano. PSM 8 solo si ambas fallan."""
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


# ── Modelo TensorRT ───────────────────────────────────────────────────────────
print("[INFO] Cargando modelo TensorRT...")
model = YOLO(MODEL_PATH, task="detect")
print(f"[INFO] Modelo: {model.names}")


# ── Frames compartidos ────────────────────────────────────────────────────────

_latest_frame_raw      = None
_latest_frame_raw_lock = threading.Lock()
_latest_frame          = None
_latest_frame_lock     = threading.Lock()


def set_latest_frame_raw(frame):
    global _latest_frame_raw
    with _latest_frame_raw_lock: _latest_frame_raw = frame

def get_latest_frame_raw():
    with _latest_frame_raw_lock: return _latest_frame_raw

def set_latest_frame(frame):
    global _latest_frame
    with _latest_frame_lock: _latest_frame = frame


# ── Detecciones recientes ─────────────────────────────────────────────────────

_detecciones_recent = deque(maxlen=20)
_detecciones_lock   = threading.Lock()


def _on_validation_update(msg_id, nuevo_estado):
    """Llamado por alertas.py al recibir respuesta del validador."""
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
    "fps": 0.0, "yolo_fps": 0.0, "pipeline": False,
    "total_detecciones": 0, "unidades_registradas": 0,
    "pg_conectada": False, "descartadas": 0,
    "tracking": None, "tracking_duracion": 0,
}
state_lock = threading.Lock()


# ── HLS bajo demanda ──────────────────────────────────────────────────────────

hls_lock      = threading.Lock()
hls_last_ping = 0
hls_proc      = None
hls_active    = False

# Cola entre productor (15fps) y escritor (stdin ffmpeg).
# maxsize=1 → si el escritor bloquea, el productor descarta el frame.
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
        '-g', str(HLS_FPS),       # keyframe cada segundo → menor latencia
        '-sc_threshold', '0',     # sin keyframes por cambio de escena
        '-f', 'hls',
        '-hls_time', '1',
        '-hls_list_size', '4',
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
    """
    Produce frames a HLS_FPS.
    Publica el frame en la cola; si el escritor está ocupado, descarta (put_nowait).
    NUNCA bloquea → video no se congela aunque ffmpeg tarde.
    """
    interval = 1.0 / HLS_FPS
    while True:
        t0 = time.time()
        if hls_active:
            frame = get_latest_frame_raw()
            if frame is not None:
                data = cv2.resize(frame, HLS_RESOLUTION).tobytes()
                try:
                    _hls_write_queue.put_nowait(data)
                except queue.Full:
                    pass   # Escritor ocupado → descartamos este frame
        rem = interval - (time.time() - t0)
        if rem > 0: time.sleep(rem)


def hls_frame_writer():
    """
    Escribe frames a stdin de ffmpeg. Puede bloquearse en write().
    Al estar aislado en su propio hilo, el bloqueo NO afecta al video ni a YOLO.
    """
    while True:
        data = _hls_write_queue.get()   # espera frame
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


# ── Hilo 1: VideoStream ───────────────────────────────────────────────────────

class VideoStream:
    def __init__(self):
        self.lock    = threading.Lock()
        self.frame   = None
        self.running = True
        self.cap     = None

    def _abrir_pipeline(self):
        print("[VS] Abriendo pipeline GStreamer...")
        if self.cap:
            try: self.cap.release()
            except: pass
        time.sleep(2)
        self.cap = cv2.VideoCapture(PIPELINE, cv2.CAP_GSTREAMER)
        ok = self.cap.isOpened()
        with state_lock: STATE["pipeline"] = ok
        print(f"[VS] Pipeline {'OK' if ok else 'FALLO'}")

    def update(self):
        time.sleep(3)
        self._abrir_pipeline()
        while self.running:
            if not self.cap or not self.cap.isOpened():
                time.sleep(5); self._abrir_pipeline(); continue
            ret, frame = self.cap.read()
            if ret:
                with state_lock: STATE["pipeline"] = True
                with self.lock:  self.frame = frame
                set_latest_frame_raw(frame)   # → HLS producer lo toma
                set_latest_frame(frame)        # → YOLO lo toma
            else:
                with state_lock: STATE["pipeline"] = False
                time.sleep(5); self._abrir_pipeline()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.running = False
        if self.cap: self.cap.release()


# ── Hilo 2: InferenceStream ───────────────────────────────────────────────────

class InferenceStream:
    def __init__(self, vs):
        self.running       = True
        self.vs            = vs
        self.yolo_fps      = 0.0
        self._cnt          = 0
        self._t            = time.time()
        self._ocr_queue    = queue.Queue(maxsize=5)
        self._votos        = []
        self.tracker       = SimpleTracker()
        self._tracker_lock = threading.Lock()
        self._track_ts     = None
        self._track_frame  = None
        self._track_msg_id = None
        self._track_votes          = []
        self._track_numero_inicial = None
        self._track_no_detectado   = None
        self._best_train_area  = 0
        self._best_train_frame = None
        self._best_train_cls   = None
        self._pending_desc       = None
        self._pending_desc_timer = None

        threading.Thread(target=self._ocr_worker, daemon=True).start()
        print("[INFO] Hilo OCR separado iniciado.")

    def _cerrar_track(self, track, duracion):
        if not track or not self._track_ts:
            return
        print(f"[TRACK FIN] {track['numero']} | {duracion}s")

        with self._tracker_lock:
            votes = self._track_votes.copy()
            numero_inicial = self._track_numero_inicial
            msg_id = self._track_msg_id
            best_frame = self._best_train_frame
            best_cls   = self._best_train_cls

        winner_num    = numero_inicial or track['numero']
        winner_conf   = track['conf']
        winner_estado = track['estado']

        if votes and numero_inicial:
            conteo = Counter(n for n, c, e in votes)
            w_num, w_count = conteo.most_common(1)[0]
            total = len(votes)
            print(f"[MULTI-LECTURA] {total} votos → {w_num} ({w_count}/{total})")
            winner_entries = [(c, e) for n, c, e in votes if n == w_num]
            winner_num    = w_num
            winner_conf   = max(c for c, e in winner_entries)
            winner_estado = winner_entries[0][1]
            if w_num != numero_inicial:
                print(f"[MULTI-LECTURA] Corrección: {numero_inicial} → {w_num}")

        actualizar_pendiente(msg_id, winner_num, winner_conf, winner_estado, duracion)

        if best_frame is not None and best_cls is not None:
            dest = _clean_dir_for_class(best_cls)
            if dest:
                def _save(fr, d, num, cls_n):
                    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{num}_{cls_n}.jpg"
                    cv2.imwrite(os.path.join(d, name), fr)
                    print(f"[TRAIN] {cls_n}/{num} → {d}")
                threading.Thread(target=_save,
                    args=(best_frame, dest, winner_num, best_cls), daemon=True).start()

        self._best_train_area  = 0
        self._best_train_frame = None
        self._best_train_cls   = None
        with self._tracker_lock:
            self._track_votes          = []
            self._track_numero_inicial = None
            self._track_no_detectado   = None
            self._track_msg_id         = None
        with state_lock:
            STATE["tracking"]          = None
            STATE["tracking_duracion"] = 0
        self._track_ts    = None
        self._track_frame = None

    def _ocr_worker(self):
        while self.running:
            try:
                job = self._ocr_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            img, crop, bbox, conf, cls_name = job

            try:
                numero_leido = leer_numero(crop)
                if not numero_leido:
                    continue

                now = time.time()
                self._votos = [(t, n, c) for t, n, c in self._votos if now - t < VOTO_WINDOW]
                self._votos.append((now, numero_leido, conf))
                ganador, veces = Counter(n for _, n, _ in self._votos).most_common(1)[0]

                n_req = 3 if _es_noche() else N_VOTOS
                if veces < n_req:
                    continue

                numero_leido = ganador
                self._votos  = []

                numero_valido, estado = validar_numero(numero_leido)
                if numero_valido is None:
                    numero_valido, estado = recuperar_ocr(numero_leido)

                # ── DESCONOCIDO ──────────────────────────────────────────────
                if numero_valido is None:
                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fp = os.path.join(DESCONOCIDAS_DIR,
                         f"{ts_str}_DESCONOCIDA_{numero_leido}.jpg")
                    threading.Thread(target=cv2.imwrite,
                        args=(fp, img.copy()), daemon=True).start()
                    with state_lock: STATE["descartadas"] += 1
                    print(f"[DESCONOCIDA] '{numero_leido}'")
                    if self._pending_desc_timer:
                        self._pending_desc_timer.cancel()
                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._pending_desc = (fp, numero_leido, conf, ts_now)
                    def _enviar(datos):
                        alerta_unidad_desconocida(*datos)
                        self._pending_desc = None
                    self._pending_desc_timer = threading.Timer(
                        4.0, _enviar, args=[self._pending_desc])
                    self._pending_desc_timer.daemon = True
                    self._pending_desc_timer.start()
                    continue

                # ── NÚMERO VÁLIDO → TRACKER ───────────────────────────────────
                if self._pending_desc_timer:
                    self._pending_desc_timer.cancel()
                    self._pending_desc_timer = None
                    self._pending_desc = None

                with self._tracker_lock:
                    track_result = self.tracker.update(bbox, numero_valido, estado, conf)

                if track_result == 'TRACKING':
                    with self._tracker_lock:
                        self._track_votes.append((numero_valido, conf, estado))
                        n_acum = len(self._track_votes)
                    if n_acum % 5 == 0:
                        active = self.tracker.active['numero'] if self.tracker.active else '?'
                        print(f"[MULTI-LECTURA] {n_acum} votos | {active}")
                    continue

                # ── NEW ───────────────────────────────────────────────────────
                ts_str     = datetime.now().strftime("%Y%m%d_%H%M%S")
                frame_name = f"{ts_str}_{numero_valido}.jpg"
                frame_path = os.path.join(FRAMES_DIR, frame_name)

                cv2.imwrite(frame_path, img.copy())   # sincrono — Telegram necesita el archivo

                self._track_ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._track_frame = frame_name

                # Thumbnail del frame completo → CROPS_DIR (no se borra con validación)
                # Sirve como imagen persistente en los tiles del dashboard
                thumb_name = f"{ts_str}_{numero_valido}_thumb.jpg"
                thumb_path = os.path.join(CROPS_DIR, thumb_name)
                try:
                    th = cv2.resize(img, (320, 240))
                    cv2.imwrite(thumb_path, th, [cv2.IMWRITE_JPEG_QUALITY, 65])
                except Exception:
                    thumb_name = None

                with self._tracker_lock:
                    self._track_votes          = [(numero_valido, conf, estado)]
                    self._track_numero_inicial = numero_valido
                    self._track_no_detectado   = numero_leido
                    self._track_msg_id         = None

                with state_lock:
                    STATE["numero"] = numero_valido
                    STATE["conf"]   = round(conf, 3)
                    STATE["ts"]     = self._track_ts
                    STATE["total_detecciones"] += 1
                    STATE["tracking"]          = numero_valido
                    STATE["tracking_duracion"] = 0

                print(f"[NEW {estado}] {numero_valido} | conf={conf:.2f} | {cls_name} "
                      f"| {'NOCHE' if _es_noche() else 'DIA'}")

                # Entrada en el deque con URLs (no base64) → menos memoria
                det_entry = {
                    "msg_id":    None,
                    "numero":    numero_valido,
                    "leido":     numero_leido,
                    "estado":    "pendiente",
                    "conf":      round(conf, 3),
                    "ts":        self._track_ts,
                    "cls":       cls_name,
                    "frame_url": f"/frames/{frame_name}",
                    "thumb_url": f"/frames/crops/{thumb_name}" if thumb_name else None,
                }
                with _detecciones_lock:
                    _detecciones_recent.appendleft(det_entry)

                # --- LÍNEA MODIFICADA ---
                msg_id = enviar_a_validador(
                    frame_path, numero_valido, conf, self._track_ts, estado,
                    no_detectado=numero_leido, thumb_name=thumb_name)

                if msg_id:
                    det_entry['msg_id'] = msg_id   # dict mutable → actualiza la ref en el deque

            except Exception as e:
                print(f"[OCR ERROR] {e}")

    def update(self):
        """YOLO corre a su ritmo. HLS usa frame RAW → sin cajas, sin bloqueo."""
        while self.running:
            img = self.vs.get_frame()
            if img is None:
                time.sleep(0.05); continue
            try:
                results = model.predict(img, conf=MODEL_CONF, device=0, verbose=False)

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

                with self._tracker_lock:
                    gone, dur = self.tracker.check_gone()
                if gone:
                    threading.Thread(target=self._cerrar_track,
                        args=(gone, dur), daemon=True).start()

                with self._tracker_lock:
                    if self.tracker.active:
                        with state_lock:
                            STATE["tracking"]          = self.tracker.active['numero']
                            STATE["tracking_duracion"] = self.tracker.get_duration()

                img_snap = img.copy()

                for box in results[0].boxes:
                    conf_b   = float(box.conf[0])
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    bbox     = (x1, y1, x2, y2)
                    cls_id   = int(box.cls[0])
                    cls_name = model.names.get(cls_id, "numero")

                    bw, bh = x2 - x1, y2 - y1
                    if bw / max(bh, 1) > 3.0 or bw / max(bh, 1) < 0.2: continue
                    if bh < 50 or bw < 50: continue

                    h_img, w_img = img.shape[:2]
                    crop = img[max(0, y1-10):min(h_img, y2+10),
                               max(0, x1-20):min(w_img, x2+20)]

                    if _es_lateral(cls_name) or _es_trasero(cls_name):
                        area = bw * bh
                        if area > self._best_train_area:
                            self._best_train_area  = area
                            self._best_train_frame = img_snap
                            self._best_train_cls   = cls_name

                    try:
                        self._ocr_queue.put_nowait(
                            (img_snap, crop.copy(), bbox, conf_b, cls_name))
                    except queue.Full:
                        pass

            except Exception as e:
                print(f"[YOLO ERROR] {e}"); time.sleep(0.1)

    def stop(self):
        self.running = False


# ── Iniciar hilos ─────────────────────────────────────────────────────────────

stream    = VideoStream()
inference = InferenceStream(stream)
threading.Thread(target=stream.update,    daemon=True).start()
threading.Thread(target=inference.update, daemon=True).start()

print("[INFO] Hilos iniciados.")
print("[INFO] HLS: productor+escritor separados — stdin.write() aislado, video fluido.")
print("[INFO] Thumbnails en CROPS_DIR (persistentes, sin borrar al validar).")


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route('/')
def index(): return HOME

@app.route('/livevideo')
def livevideo(): return DASHBOARD

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
    return jsonify({"pending": pending, "approved": db_query(limit=15)})

@app.route('/api/stats')
def api_stats(): return jsonify(db_stats())

@app.route('/api/unidades')
def api_unidades():
    with _unidades_lock:
        return jsonify({"pg_conectada": _pg_connected,
                        "total": len(UNIDADES), "unidades": sorted(UNIDADES)})

@app.route('/health')
def health():
    with state_lock:
        return jsonify({"status": "ok", "yolo_fps": STATE["yolo_fps"],
            "pipeline": STATE["pipeline"], "hls": hls_active,
            "pg_conectada": _pg_connected, "unidades_pg": len(UNIDADES),
            "descartadas": STATE["descartadas"], "tracking": STATE["tracking"],
            "tracking_duracion": STATE["tracking_duracion"]})


if __name__ == '__main__':
    print(f"[INFO] http://{FLASK_HOST}:{FLASK_PORT}")
    try:
        app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo...")
        with hls_lock: hls_stop()
        stream.stop(); inference.stop()