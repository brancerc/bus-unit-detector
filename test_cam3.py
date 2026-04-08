"""
Test temporal: reproduce testc3.mp4 con detección YOLO + OCR CNN
Acceder en http://100.101.67.41:5099
"""
import cv2
import time
import threading
from flask import Flask, Response
from ultralytics import YOLO

app = Flask(__name__)

MODEL_PATH = "/home/cisa/Documents/ProyectoIA/best.engine"
VIDEO_PATH = "/tmp/testc4.mp4"

print("[TEST] Cargando YOLO...")
model = YOLO(MODEL_PATH, task="detect")
print(f"[TEST] Clases: {model.names}")

# Intentar cargar CNN OCR
ocr_engine = None
try:
    import sys
    sys.path.insert(0, "/home/cisa/Documents/ProyectoIA")
    from inferencia_trt import OcrEngine
    ocr_engine = OcrEngine("/home/cisa/Documents/ProyectoIA/ocr_cnn.engine")
    print("[TEST] CNN OCR cargado")
except Exception as e:
    print(f"[TEST] Sin CNN OCR: {e}")

_latest = None
_lock = threading.Lock()
_stats = {"total": 0, "detecciones": 0, "lecturas": []}


def process_video():
    global _latest
    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        print("[TEST] ERROR: no se pudo abrir el video")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    delay = 1.0 / fps
    print(f"[TEST] Video: {int(cap.get(3))}x{int(cap.get(4))} @ {fps:.1f}fps")

    while True:
        t0 = time.time()
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop
            continue

        _stats["total"] += 1
        display = frame.copy()

        results = model.predict(frame, conf=0.30, device=0, verbose=False)

        for box in results[0].boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cls_id = int(box.cls[0])
            cls_name = model.names.get(cls_id, "?")
            bw, bh = x2 - x1, y2 - y1

            # Dibujar bbox
            color = (0, 255, 0)
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 2)
            label = f"{cls_name} {conf:.2f} [{bw}x{bh}]"
            cv2.putText(display, label, (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            _stats["detecciones"] += 1

            # OCR si bbox suficiente
            if bh >= 15 and bw >= 15:
                h_img, w_img = frame.shape[:2]
                crop = frame[max(0, y1-10):min(h_img, y2+10),
                             max(0, x1-20):min(w_img, x2+20)]

                numero = None
                if ocr_engine:
                    try:
                        numero = ocr_engine.leer(crop)
                    except:
                        pass

                if numero:
                    cv2.putText(display, f"OCR: {numero}", (x1, y2 + 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                    _stats["lecturas"].append(numero)
                    print(f"[DET] {cls_name} conf={conf:.2f} OCR={numero} bbox={bw}x{bh}")
                else:
                    cv2.putText(display, "OCR: ---", (x1, y2 + 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                    print(f"[DET] {cls_name} conf={conf:.2f} OCR=NONE bbox={bw}x{bh}")

        # Info overlay
        cv2.putText(display, f"Frame {_stats['total']} | Det: {_stats['detecciones']}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        with _lock:
            _latest = display

        elapsed = time.time() - t0
        if elapsed < delay:
            time.sleep(delay - elapsed)


def gen_mjpeg():
    while True:
        with _lock:
            frame = _latest
        if frame is None:
            time.sleep(0.05)
            continue
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
        time.sleep(0.04)


@app.route('/')
def index():
    return '''<html><body style="background:#000;margin:0">
    <img src="/stream" style="width:100%;height:100vh;object-fit:contain">
    </body></html>'''

@app.route('/stream')
def stream():
    return Response(gen_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stats')
def stats():
    from flask import jsonify
    return jsonify(_stats)


threading.Thread(target=process_video, daemon=True).start()

if __name__ == '__main__':
    print("[TEST] http://100.101.67.41:5099")
    app.run(host='0.0.0.0', port=5099, threaded=True)
