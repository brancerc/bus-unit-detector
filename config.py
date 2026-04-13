"""
CISA - Configuración centralizada
Todas las constantes, credenciales, rutas y parámetros en un solo lugar.
Modificar este archivo para cambios de IPs, tokens, rutas, etc.
"""
import os

# ==============================================================================
# TELEGRAM
# ==============================================================================
TG_TOKEN        = "8450858285:AAFNh2MIYuZsCR7LNvE-KlYPTlUEEaX7YPo"
TG_CHAT_ID      = "-1003996142767"
TG_VALIDADOR_ID = "6854120172"

# ==============================================================================
# CAMARA / RTSP
# ==============================================================================
RTSP_URL = "rtsp://admin:PatioCCA_@192.168.10.2:554/cam/realmonitor?channel=1&subtype=1"

PIPELINE = (
f"rtspsrc location={RTSP_URL} latency=100 ! "
"rtph264depay ! h264parse ! nvv4l2decoder ! "
"nvvidconv ! video/x-raw, format=BGRx ! "
"videoconvert ! video/x-raw, format=BGR ! appsink"
)


# TEMPORAL — reemplazar pipeline Cam 2 con video local
CAMERA_PIPELINES = {
    1: PIPELINE,
    2: (
        "filesrc location=/tmp/pdeteccion.mp4 ! "
        "qtdemux ! h265parse ! nvv4l2decoder ! "
        "nvvidconv ! video/x-raw,format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! "
        "appsink sync=false max-buffers=1 drop=true"
    ),
}

# ==============================================================================
# RUTAS
# ==============================================================================
HLS_DIR            = "/tmp/hls"
FRAMES_DIR         = "/media/cisa/JETSON_SD/cisa_frames"
DESCONOCIDAS_DIR   = os.path.join(FRAMES_DIR, "unidades_desconocidas")
CROPS_DIR          = os.path.join(FRAMES_DIR, "crops")
CLEAN_DIR          = os.path.join(FRAMES_DIR, "clean")
CLEAN_LATERAL_DIR  = os.path.join(CLEAN_DIR, "numeroslaterales")
CLEAN_TRASERO_DIR  = os.path.join(CLEAN_DIR, "numerostraseros")
REVISAR_DIR        = os.path.join(FRAMES_DIR, "revisar")
DB_PATH            = "/home/cisa/Documents/ProyectoIA/detecciones.db"

for d in [HLS_DIR, FRAMES_DIR, DESCONOCIDAS_DIR, CROPS_DIR,
          CLEAN_DIR, CLEAN_LATERAL_DIR, CLEAN_TRASERO_DIR, REVISAR_DIR]:
    os.makedirs(d, exist_ok=True)

# ==============================================================================
# POSTGRESQL
# ==============================================================================
PG_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "postgres",
    "user":     "postgres",
    "password": "",
}

# ==============================================================================
# MODELO / DETECCIÓN — parámetros base (Cam 1)
# Los parámetros por cámara se definen en pipe_stream_v2.py
# ==============================================================================
MODEL_PATH      = "best.engine"
MODEL_CONF      = 0.70      # Confianza base (Cam 1). Cam 2 usa 0.60
COOLDOWN_SEG    = 2.0
N_VOTOS         = 2
VOTO_WINDOW     = 2.0
OCR_TARGET_H    = 160
OCR_MIN_SIZE    = 15
OCR_MAX_DIGITS  = 4
OCR_MIN_DIGITS  = 3
LEVENSHTEIN_MAX = 1
ID_PUERTA       = 1         # Cam 1. Cam 2 usa puerta_id=2

# ==============================================================================
# HLS STREAMING
# ==============================================================================
HLS_TIMEOUT    = 15
HLS_RESOLUTION = (704, 480)
HLS_FPS        = 15

# ==============================================================================
# SERVIDOR FLASK
# ==============================================================================
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5001

# ==============================================================================
# REFRESCO DE UNIDADES (segundos)
# ==============================================================================
PG_REFRESH_INTERVAL = 60
