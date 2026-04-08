# camara2_test.py
from ultralytics import YOLO
from flask import Flask, Response
import cv2, time, os, threading

RTSP       = "rtsp://admin:PatioCCA_@192.168.10.4:554/cam/realmonitor?channel=1&subtype=0"
ENGINE     = "/home/cisa/Documents/ProyectoIA/best.engine"
SAVE_DIR   = "/tmp/test_cam2/"
CONF_MIN   = 0.40
CONF_FONDO = 0.65
PUERTO     = 5004  # distinto a 5001/5002/5003

os.makedirs(SAVE_DIR, exist_ok=True)

app         = Flask(__name__)
last_frame  = None
frame_lock  = threading.Lock()

def inference_loop():
    global last_frame
    model = YOLO(ENGINE, task="detect")
    cap   = cv2.VideoCapture(RTSP)

    print("Esperando detecciones... (Ctrl+C para terminar)")
    print(f"[INFO] Ignorando detecciones < {CONF_FONDO} (ruido de fondo)")
    print(f"[INFO] Video en: http://100.101.67.41:{PUERTO}/video")
    print("-" * 50)

    frame_n = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        results     = model.predict(frame, conf=CONF_MIN, verbose=False)
        detecciones = results[0].boxes
        annotated   = results[0].plot()

        # Actualizar frame para streaming
        with frame_lock:
            last_frame = annotated.copy()

        if len(detecciones) > 0:
            for r in detecciones:
                nombre = model.names[int(r.cls)]
                conf   = float(r.conf)

                if conf < CONF_FONDO:
                    continue

                emoji = "✅" if conf >= 0.70 else "⚠️"
                print(f"{emoji} {nombre} — {conf:.2f}  ← POSIBLE BUS")
                path = f"{SAVE_DIR}det_{frame_n:05d}.jpg"
                cv2.imwrite(path, annotated)
                print(f"   → frame guardado: {path}")

        frame_n += 1

@app.route("/video")
def video():
    def generar():
        while True:
            with frame_lock:
                if last_frame is None:
                    time.sleep(0.05)
                    continue
                _, jpeg = cv2.imencode(".jpg", last_frame)
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" +
                   jpeg.tobytes() + b"\r\n")
            time.sleep(1/15)  # ~15fps
    return Response(generar(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/")
def index():
    return '<html><body style="background:#000;margin:0"><img src="/video" style="width:100%"></body></html>'

if __name__ == "__main__":
    t = threading.Thread(target=inference_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)