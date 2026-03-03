from flask import Flask, Response, jsonify, render_template_string
import cv2
import os
import time
import re
import easyocr
from ultralytics import YOLO

app = Flask(__name__)

# --- CONFIG RTSP ---
RTSP_URL = "rtsp://admin:PatioCCA_@192.168.10.2:554/cam/realmonitor?channel=1&subtype=1"

# Force TCP
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

# --- MODELS ---
detector = YOLO("best.engine", task="detect")
reader = easyocr.Reader(["en"], gpu=True)

# --- STATE (ultimo numero detectado) ---
STATE = {
    "numero": None,
    "conf": None,
    "ts": None
}

# --- TUNING ---
TARGET_MIN_LEN = 3
TARGET_MAX_LEN = 4
COOLDOWN_SECONDS = 1.0
MIN_OCR_CONF = 0.20  # baja si te esta tirando muchos falsos negativos

_last_sent_num = None
_last_sent_ts = 0.0

digits_re = re.compile(r"(\d{3,4})")

HTML_NUMERO = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Ultimo numero</title>
    <meta http-equiv="refresh" content="1">
    <style>
      body { font-family: Arial, sans-serif; background:#0b0b0b; color:#f2f2f2; }
      .wrap { max-width: 780px; margin: 50px auto; padding: 24px; border: 1px solid #333; border-radius: 12px; }
      .num { font-size: 88px; font-weight: 900; letter-spacing: 2px; margin: 18px 0; }
      .meta { color: #bdbdbd; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <h1>CISA - Ultimo numero detectado</h1>
      <div class="num">{{ numero if numero else "--" }}</div>
      <div class="meta">Conf: {{ conf if conf else "--" }}</div>
      <div class="meta">Timestamp: {{ ts if ts else "--" }}</div>
      <p><a href="/video">Ver video</a> | <a href="/api/numero">API JSON</a></p>
    </div>
  </body>
</html>
"""

def normalize_text(s: str) -> str:
    s = (s or "").strip()
    # correcciones tipicas OCR
    s = s.replace("O", "0").replace("o", "0")
    s = s.replace("I", "1").replace("l", "1")
    # solo numeros
    s = re.sub(r"[^0-9]", "", s)
    return s

def pick_best_ocr(crop):
    # detail=1 => [bbox, text, conf]
    res = reader.readtext(crop, detail=1, paragraph=False)
    if not res:
        return None, 0.0
    best = max(res, key=lambda x: x[2])
    text = best[1]
    conf = float(best[2])
    return text, conf

def gen_frames():
    global _last_sent_num, _last_sent_ts

    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)

    while True:
        success, frame = cap.read()
        if not success:
            break

        # IA
        results = detector.predict(frame, conf=0.60, device=0, verbose=False)

        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])

                # clamp
                x1 = max(0, x1); y1 = max(0, y1)
                x2 = min(frame.shape[1], x2); y2 = min(frame.shape[0], y2)

                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                raw_text, ocr_conf = pick_best_ocr(crop)
                if not raw_text:
                    continue

                if ocr_conf < MIN_OCR_CONF:
                    continue

                cleaned = normalize_text(raw_text)
                m = digits_re.search(cleaned)
                if not m:
                    continue

                numero = m.group(1)
                if not (TARGET_MIN_LEN <= len(numero) <= TARGET_MAX_LEN):
                    continue

                now = time.time()

                # anti repeticion / cooldown
                if numero == _last_sent_num and (now - _last_sent_ts) < COOLDOWN_SECONDS:
                    pass
                else:
                    STATE["numero"] = numero
                    STATE["conf"] = round(ocr_conf, 3)
                    STATE["ts"] = now
                    _last_sent_num = numero
                    _last_sent_ts = now

                # overlay
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame,
                    "ID: " + numero,
                    (x1, max(0, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 0),
                    2
                )

        # encode web
        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")

    cap.release()

@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video")
def video_page():
    return "<h1>Monitoreo Metrobús CISA (Sub-Stream)</h1><img src='/video_feed' style='width:80%;'>"

@app.route("/")
def index():
    # pagina principal -> numero
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STATE["ts"])) if STATE["ts"] else None
    return render_template_string(HTML_NUMERO, numero=STATE["numero"], conf=STATE["conf"], ts=ts)

@app.route("/numero")
def numero_page():
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STATE["ts"])) if STATE["ts"] else None
    return render_template_string(HTML_NUMERO, numero=STATE["numero"], conf=STATE["conf"], ts=ts)

@app.route("/api/numero")
def numero_api():
    return jsonify(STATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, threaded=True)