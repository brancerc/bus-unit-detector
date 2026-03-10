import os

# ==============================================================================
# TELEGRAM
# ==============================================================================
TG_TOKEN   = "8450858285:AAFNh2MIYuZsCR7LNvE-KlYPTlUEEaX7YPo"
TG_CHAT_ID = "-5249532175"

# ==============================================================================
# CAMARA / RTSP
# ==============================================================================
RTSP_URL  = "rtsp://admin:PatioCCA_@192.168.10.2:554/cam/realmonitor?channel=1&subtype=1"
PIPELINE  = (
    f"rtspsrc location={RTSP_URL} "
    "protocols=tcp latency=200 ! "
    "rtph264depay ! h264parse ! nvv4l2decoder ! "
    "nvvidconv ! video/x-raw,format=BGRx ! "
    "videoconvert ! video/x-raw,format=BGR ! "
    "appsink emit-signals=True max-buffers=1 drop=True sync=False"
)

# ==============================================================================
# RUTAS DE ALMACENAMIENTO
# ==============================================================================
HLS_DIR          = "/tmp/hls"
FRAMES_DIR       = "/media/cisa/JETSON_SD/cisa_frames"
DESCONOCIDAS_DIR = os.path.join(FRAMES_DIR, "unidades_desconocidas")
CROPS_DIR        = os.path.join(FRAMES_DIR, "crops")
CLEAN_DIR        = os.path.join(FRAMES_DIR, "clean")
CLEAN_LATERAL_DIR  = os.path.join(CLEAN_DIR, "numeroslaterales")
CLEAN_TRASERO_DIR  = os.path.join(CLEAN_DIR, "numerostraseros")
DB_PATH          = "/home/cisa/Documents/ProyectoIA/detecciones.db"

for d in [HLS_DIR, FRAMES_DIR, DESCONOCIDAS_DIR, CROPS_DIR,
          CLEAN_DIR, CLEAN_LATERAL_DIR, CLEAN_TRASERO_DIR]:
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
# MODELO / DETECCION (Mejoras de Votación)
# ==============================================================================
MODEL_PATH      = "best.engine"
MODEL_CONF      = 0.70  
COOLDOWN_SEG    = 2.0
N_VOTOS         = 3      # Subido de 2 para mayor estabilidad
VOTO_WINDOW     = 4.0    # Ventana amplia para promediar lecturas
OCR_TARGET_H    = 160    
OCR_MIN_SIZE    = 15
OCR_MAX_DIGITS  = 4
OCR_MIN_DIGITS  = 3
LEVENSHTEIN_MAX = 1
ID_PUERTA       = 1

# ==============================================================================
# HLS / FLASK
# ==============================================================================
HLS_TIMEOUT    = 15
HLS_RESOLUTION = (704, 480)
HLS_FPS        = 15
FLASK_HOST     = "0.0.0.0"
FLASK_PORT     = 5001
PG_REFRESH_INTERVAL = 60