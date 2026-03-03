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
    PIPELINE, HLS_DIR, FRAMES_DIR, DESCONOCIDAS_DIR, CROPS_DIR, CLEAN_DIR, DB_PATH,
    PG_CONFIG, MODEL_PATH, MODEL_CONF, COOLDOWN_SEG, N_VOTOS, VOTO_WINDOW,
    OCR_TARGET_H, OCR_MIN_SIZE, OCR_MAX_DIGITS, OCR_MIN_DIGITS,
    LEVENSHTEIN_MAX, HLS_TIMEOUT, HLS_RESOLUTION, HLS_FPS,
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

print(f"[INFO] Templates cargados desde {TEMPLATE_DIR}")


# ==============================================================================
# Tracker simple por IoU
# ==============================================================================

class SimpleTracker:
    """
    Rastrea la unidad activa en camara usando solapamiento de cajas (IoU).
    Costo computacional: ~0 (4 operaciones aritmeticas por frame).
    No necesita Kalman, no necesita SORT, no necesita scipy.

    Flujo:
      1. YOLO detecta -> OCR lee -> PG valida -> tracker.update(bbox, numero)
      2. Si es el MISMO bus (IoU alto o mismo numero) -> retorna TRACKING -> no hace nada
      3. Si es un bus NUEVO -> cierra el track anterior -> retorna NEW -> alerta + guarda
      4. Cada frame sin deteccion -> tracker.check_gone() verifica si el bus se fue
    """

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
            'numero':     numero,
            'estado':     estado,
            'conf':       conf,
            'bbox':       bbox,
            'first_seen': now,
            'last_seen':  now,
        }
        return 'NEW'

    def check_gone(self):
        if self.active is None:
            return None, 0
        elapsed = time.time() - self.active['last_seen']
        if elapsed > self.GONE_TIMEOUT:
            duration = self.active['last_seen'] - self.active['first_seen']
            track = self.active.copy()
            self.active = None
            return track, round(duration, 1)
        return None, 0

    def get_duration(self):
        if self.active is None:
            return 0
        return round(time.time() - self.active['first_seen'], 1)

    def is_tracking(self, numero):
        return self.active is not None and self.active['numero'] == numero


# ==============================================================================
# PostgreSQL
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
        cur.close()
        con.close()

        with _unidades_lock:
            if nuevas != UNIDADES:
                agregadas  = nuevas - UNIDADES
                eliminadas = UNIDADES - nuevas
                if agregadas:
                    print(f"[PG] Unidades nuevas detectadas: {sorted(agregadas)}")
                if eliminadas:
                    print(f"[PG] Unidades removidas: {sorted(eliminadas)}")
                UNIDADES = nuevas

        _pg_connected = True
        print(f"[PG] Conectado — {len(nuevas)} unidades activas")
        return True
    except Exception as e:
        _pg_connected = False
        print(f"[PG ERROR] {e}")
        return False


def _refresh_unidades_loop():
    while True:
        cargar_unidades_pg()
        time.sleep(PG_REFRESH_INTERVAL)


print("=" * 60)
print("[PG] Conectando a PostgreSQL...")
if cargar_unidades_pg():
    print(f"[PG] Base de datos ACTIVA — {len(UNIDADES)} unidades cargadas")
else:
    print("[PG] FALLO CONEXION — Detecciones DESCARTADAS hasta que PG responda")
print("=" * 60)

threading.Thread(target=_refresh_unidades_loop, daemon=True).start()


# ==============================================================================
# SQLite
# ==============================================================================

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
            estado    TEXT    DEFAULT 'VERIFICADO',
            duracion  REAL   DEFAULT 0
        )
    """)
    try:
        con.execute("ALTER TABLE detecciones ADD COLUMN duracion REAL DEFAULT 0")
        print("[DB] Columna 'duracion' agregada a tabla existente.")
    except sqlite3.OperationalError:
        pass
    con.commit(); con.close()
    print("[DB] SQLite lista.")


def db_insert(numero, confianza, frame_path=None, estado="VERIFICADO", duracion=0):
    now = datetime.now()
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT INTO detecciones (numero,confianza,fecha,hora,ts,frame,estado,duracion) VALUES (?,?,?,?,?,?,?,?)",
            (numero, round(confianza, 3),
             now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
             now.strftime("%Y-%m-%d %H:%M:%S"), frame_path, estado, round(duracion, 1)))
        con.commit(); con.close()
    except Exception as e:
        print(f"[DB ERROR] {e}")


def db_update_duracion(numero, ts, duracion):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "UPDATE detecciones SET duracion=? WHERE numero=? AND ts=?",
            (round(duracion, 1), numero, ts))
        con.commit(); con.close()
    except Exception as e:
        print(f"[DB ERROR] update duracion: {e}")


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
        print(f"[DB ERROR] {e}"); return {"total": 0, "hoy": 0, "ultima": None}


init_db()


# ==============================================================================
# Validacion contra PostgreSQL
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

    with _unidades_lock:
        unidades_snapshot = UNIDADES.copy()

    if not unidades_snapshot:
        print(f"[DESCARTADO] '{leido}' — lista PG vacia")
        return None, None

    if leido in unidades_snapshot:
        print(f"[VERIFICADO] '{leido}' existe en PostgreSQL")
        return leido, "VERIFICADO"

    candidatos = [u for u in unidades_snapshot if len(u) == len(leido)]
    mejor, mejor_dist = None, 999
    for u in candidatos:
        d = levenshtein(leido, u)
        if d < mejor_dist:
            mejor_dist = d
            mejor = u

    if mejor_dist <= LEVENSHTEIN_MAX:
        print(f"[CORREGIDO] OCR '{leido}' -> BD '{mejor}' (dist={mejor_dist})")
        return mejor, "CORREGIDO"

    print(f"[DESCARTADO] '{leido}' sin match en PG (mejor: {mejor}, dist={mejor_dist})")
    return None, None


# ==============================================================================
# Tesseract OCR
# ==============================================================================
print("[INFO] Tesseract OCR listo.")


def leer_numero(crop):
    try:
        h, w = crop.shape[:2]
        if h < OCR_MIN_SIZE or w < OCR_MIN_SIZE:
            return None

        scale = max(1, OCR_TARGET_H // h)
        crop_up = cv2.resize(crop, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(crop_up, cv2.COLOR_BGR2GRAY)

        resultados = []

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
        g1 = clahe.apply(gray)
        _, g1 = cv2.threshold(g1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        g2 = cv2.adaptiveThreshold(gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 8)

        g3 = cv2.bitwise_not(g1)

        config = '--psm 7 -c tessedit_char_whitelist=0123456789'
        for img_proc in [g1, g2, g3]:
            texto = pytesseract.image_to_string(img_proc, config=config).strip()
            limpio = re.sub(r'[^0-9]', '', texto)[:OCR_MAX_DIGITS]
            if len(limpio) >= OCR_MIN_DIGITS:
                resultados.append(limpio)

        if not resultados:
            config8 = '--psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(g1, config=config8).strip()
            limpio = re.sub(r'[^0-9]', '', texto)[:OCR_MAX_DIGITS]
            if len(limpio) >= OCR_MIN_DIGITS:
                resultados.append(limpio)

        if not resultados:
            return None

        return Counter(resultados).most_common(1)[0][0]

    except Exception as e:
        print(f"[OCR ERROR] {e}"); return None


# ==============================================================================
# Modelo TensorRT
# ==============================================================================
print("[INFO] Cargando modelo TensorRT...")
model = YOLO(MODEL_PATH, task="detect")
print("[INFO] Modelo cargado.")


# ==============================================================================
# Estado global
# ==============================================================================
STATE = {
    "numero": None, "conf": None, "ts": None,
    "fps": 0.0, "pipeline": False, "total_detecciones": 0,
    "unidades_registradas": 0, "pg_conectada": False,
    "descartadas": 0,
    "tracking": None,
    "tracking_duracion": 0,
}
state_lock = threading.Lock()


# ==============================================================================
# HLS bajo demanda
# ==============================================================================
hls_lock      = threading.Lock()
hls_last_ping = 0
hls_proc      = None
hls_active    = False


def hls_start():
    global hls_proc, hls_active
    if hls_active:
        return
    print("[HLS] Iniciando stream bajo demanda...")
    for f in glob.glob(os.path.join(HLS_DIR, "*")):
        try: os.remove(f)
        except OSError: pass

    w, h = HLS_RESOLUTION
    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(HLS_FPS),
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
        resized = cv2.resize(frame, HLS_RESOLUTION)
        hls_proc.stdin.write(resized.tobytes())
    except BrokenPipeError:
        hls_stop()
    except Exception:
        pass


# ==============================================================================
# Hilo 1: Captura de video
# ==============================================================================

class VideoStream:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.running = True
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
        self.running = False
        if self.cap:
            self.cap.release()


# ==============================================================================
# Hilo 2: Inferencia + OCR + Tracking
# ==============================================================================

class InferenceStream:
    def __init__(self, vs):
        self.lock = threading.Lock()
        self.result = None
        self.running = True
        self.vs = vs
        self.fps = 0.0
        self._cnt = 0
        self._t = time.time()
        self._votos = []
        self._last_ts = 0.0  # Cooldown entre detecciones
        # TODO: Habilitar tracking cuando el sistema este estable
        # self.tracker = SimpleTracker()
        # self._track_ts = None
        # self._track_frame = None
        self._pending_desc = None
        self._pending_desc_timer = None

    # def _cerrar_track(self, track, duracion):
    #     if track and self._track_ts:
    #         db_update_duracion(track['numero'], self._track_ts, duracion)
    #         print(f"[TRACK FIN] {track['numero']} salio de camara | Duracion: {duracion}s")
    #         with state_lock:
    #             STATE["tracking"] = None
    #             STATE["tracking_duracion"] = 0
    #         self._track_ts = None
    #         self._track_frame = None

    def update(self):
        while self.running:
            img = self.vs.get_frame()
            if img is None:
                time.sleep(0.05); continue
            try:
                results   = model.predict(img, conf=MODEL_CONF, device=0, verbose=False)
                annotated = results[0].plot()

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

                # TODO: Tracking deshabilitado temporalmente
                # gone_track, gone_dur = self.tracker.check_gone()
                # if gone_track:
                #     self._cerrar_track(gone_track, gone_dur)
                # if self.tracker.active:
                #     with state_lock:
                #         STATE["tracking"] = self.tracker.active['numero']
                #         STATE["tracking_duracion"] = self.tracker.get_duration()

                for box in results[0].boxes:
                    conf = float(box.conf[0])
                    now  = time.time()
                    if (now - self._last_ts) < COOLDOWN_SEG:
                        continue

                    x1, y1, x2, y2 = map(int, box.xyxy[0])

                    pad_x, pad_y = 20, 10
                    h_img, w_img = img.shape[:2]
                    crop = img[max(0, y1-pad_y):min(h_img, y2+pad_y),
                               max(0, x1-pad_x):min(w_img, x2+pad_x)]

                    numero_leido = leer_numero(crop)
                    if not numero_leido:
                        continue

                    self._votos = [(t, n, c) for t, n, c in self._votos
                                   if time.time() - t < VOTO_WINDOW]
                    self._votos.append((time.time(), numero_leido, conf))

                    conteo = Counter(n for _, n, _ in self._votos)
                    numero_ganador, veces = conteo.most_common(1)[0]

                    if veces < N_VOTOS:
                        cv2.putText(annotated, f"? {numero_leido} ({veces}/{N_VOTOS})",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 165, 0), 2)
                        continue

                    numero_leido = numero_ganador
                    self._votos = []

                    numero_valido, estado = validar_numero(numero_leido)

                    # -- DESCONOCIDO --
                    if numero_valido is None:
                        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

                        desc_frame_name = f"{ts_str}_DESCONOCIDA_{numero_leido}.jpg"
                        desc_frame_path = os.path.join(DESCONOCIDAS_DIR, desc_frame_name)
                        desc_frame_save = annotated.copy()
                        cv2.putText(desc_frame_save, f"DESCONOCIDA: {numero_leido}",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
                        cv2.imwrite(desc_frame_path, desc_frame_save)

                        desc_crop_name = f"{ts_str}_DESCONOCIDA_{numero_leido}_crop.jpg"
                        cv2.imwrite(os.path.join(DESCONOCIDAS_DIR, desc_crop_name), crop)

                        clean_name = f"{ts_str}_DESCONOCIDA_{numero_leido}_clean.jpg"
                        cv2.imwrite(os.path.join(CLEAN_DIR, clean_name), img)

                        with state_lock:
                            STATE["descartadas"] += 1

                        print(f"[DESCONOCIDA] '{numero_leido}' guardada en unidades_desconocidas/")

                        if self._pending_desc_timer:
                            self._pending_desc_timer.cancel()

                        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._pending_desc = (desc_frame_path, numero_leido, conf, ts_now)

                        def _enviar_si_no_cancelada(datos):
                            fp, num, cnf, ts = datos
                            print(f"[DESCONOCIDA -> Telegram] '{num}' sin correccion tras 4s")
                            alerta_unidad_desconocida(fp, num, cnf, ts)
                            self._pending_desc = None

                        self._pending_desc_timer = threading.Timer(
                            4.0, _enviar_si_no_cancelada, args=[self._pending_desc])
                        self._pending_desc_timer.daemon = True
                        self._pending_desc_timer.start()

                        cv2.putText(annotated, f"X {numero_leido}",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
                        continue

                    # -- NUMERO VALIDO --
                    self._last_ts = time.time()

                    # Cancelar alerta DESCONOCIDA pendiente
                    if self._pending_desc_timer:
                        self._pending_desc_timer.cancel()
                        self._pending_desc_timer = None
                        if self._pending_desc:
                            print(f"[DEBOUNCE] Cancelada alerta DESCONOCIDA '{self._pending_desc[1]}' -> lectura correcta: {numero_valido}")
                            self._pending_desc = None

                    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

                    # Frame anotado (para Telegram)
                    frame_name = f"{ts_str}_{numero_valido}.jpg"
                    frame_path = os.path.join(FRAMES_DIR, frame_name)
                    frame_save = annotated.copy()
                    cv2.putText(frame_save, numero_valido,
                        (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
                    cv2.imwrite(frame_path, frame_save)

                    # Crop limpio
                    crop_name = f"{ts_str}_{numero_valido}_crop.jpg"
                    cv2.imwrite(os.path.join(CROPS_DIR, crop_name), crop)

                    # Frame limpio (para reentrenamiento)
                    clean_name = f"{ts_str}_{numero_valido}_clean.jpg"
                    cv2.imwrite(os.path.join(CLEAN_DIR, clean_name), img)

                    with state_lock:
                        STATE["numero"] = numero_valido
                        STATE["conf"]   = round(conf, 3)
                        STATE["ts"]     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        STATE["total_detecciones"] += 1

                    db_insert(numero_valido, conf, frame_name, estado)
                    print(f"[{estado}] {numero_valido} | Conf: {conf:.2f} | Frame: {frame_name}")

                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    threading.Thread(
                        target=alerta_unidad_detectada,
                        args=(frame_path, numero_valido, conf, ts_now),
                        daemon=True
                    ).start()

                    cv2.putText(annotated, numero_valido,
                        (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

                # -- Overlay --
                cv2.putText(annotated, f"FPS: {self.fps:.1f}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(annotated, datetime.now().strftime("%H:%M:%S"),
                    (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

                pg_color = (0, 255, 0) if _pg_connected else (0, 0, 255)
                pg_text  = f"PG: {len(UNIDADES)} uds" if _pg_connected else "PG: DESCONECTADA"
                cv2.putText(annotated, pg_text,
                    (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, pg_color, 2)

                # TODO: Overlay de tracking deshabilitado
                # if self.tracker.active:
                #     dur = self.tracker.get_duration()
                #     track_text = f"TRACK: {self.tracker.active['numero']} | {dur:.0f}s en camara"
                #     cv2.putText(annotated, track_text,
                #         (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                with self.lock: self.result = annotated
                with hls_lock: hls_push_frame(annotated)

            except Exception as e:
                print(f"[ERROR] Inferencia: {e}"); time.sleep(0.1)

    def get_result(self):
        with self.lock:
            return self.result.copy() if self.result is not None else None

    def stop(self):
        self.running = False


# ==============================================================================
# Iniciar hilos
# ==============================================================================
stream    = VideoStream()
inference = InferenceStream(stream)

threading.Thread(target=stream.update,    daemon=True).start()
threading.Thread(target=inference.update, daemon=True).start()
print("[INFO] Hilos iniciados. HLS arrancara solo cuando abras /livevideo")


# ==============================================================================
# Rutas Flask
# ==============================================================================

@app.route('/')
def index():
    return HOME

@app.route('/livevideo')
def livevideo():
    return DASHBOARD

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
    if not os.path.exists(filepath):
        return '', 404
    mt = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    response = make_response(send_file(filepath, mimetype=mt))
    if filename.endswith('.m3u8'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route('/frames/<path:filename>')
def serve_frame(filename):
    filepath = os.path.join(FRAMES_DIR, filename)
    if not os.path.exists(filepath):
        return '', 404
    return send_file(filepath, mimetype='image/jpeg')

@app.route('/api/estado')
def api_estado():
    with state_lock:
        return jsonify(dict(STATE))

@app.route('/api/detecciones')
def api_detecciones():
    fecha = request.args.get('fecha')
    limit = int(request.args.get('limit', 50))
    return jsonify(db_query(fecha=fecha, limit=limit))

@app.route('/api/stats')
def api_stats():
    return jsonify(db_stats())

@app.route('/api/unidades')
def api_unidades():
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
            "descartadas": STATE["descartadas"],
            "tracking": STATE["tracking"],
            "tracking_duracion": STATE["tracking_duracion"]
        })


# ==============================================================================
# Main
# ==============================================================================
if __name__ == '__main__':
    print(f"[INFO] Servidor en http://{FLASK_HOST}:{FLASK_PORT}")
    print(f"[INFO] PostgreSQL: {'CONECTADA' if _pg_connected else 'DESCONECTADA'} — {len(UNIDADES)} unidades")
    print(f"[INFO] Modo: VALIDACION ESTRICTA — solo acepta unidades en PG")
    print(f"[INFO] Tracker: IoU + numero (costo: ~0 CPU)")
    try:
        app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[INFO] Deteniendo...")
        with hls_lock:
            hls_stop()
        stream.stop()
        inference.stop()