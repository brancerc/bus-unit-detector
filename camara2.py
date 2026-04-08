"""
CISA - Visor Camara 2
Stream HLS de la camara 192.168.10.4 (H.265/HEVC)
ffmpeg maneja captura + transcodificacion directo — sin GStreamer ni Python intermedio.
Puerto 5002.

Uso:
  source venv/bin/activate
  python3 camara2.py

Acceso:
  http://192.168.40.49:5002
"""

import subprocess
import threading
import time
import glob
import os
from flask import Flask, send_file, make_response, jsonify

# ==============================================================================
# Configuracion
# ==============================================================================
CAM2_RTSP  = "rtsp://admin:PatioCCA_@192.168.10.4:554/cam/realmonitor?channel=1&subtype=1"
HLS_DIR    = "/tmp/hls_cam2"
FLASK_PORT = 5002
HLS_TIMEOUT = 30   # segundos sin clientes antes de detener ffmpeg

os.makedirs(HLS_DIR, exist_ok=True)

app = Flask(__name__)

# ==============================================================================
# ffmpeg — captura RTSP H.265 y genera HLS H.264 directamente
# ==============================================================================
ffmpeg_proc   = None
ffmpeg_active = False
ffmpeg_lock   = threading.Lock()
last_ping     = 0


def ffmpeg_start():
    global ffmpeg_proc, ffmpeg_active
    if ffmpeg_active:
        return

    # Limpiar segmentos anteriores
    for f in glob.glob(os.path.join(HLS_DIR, "*")):
        try: os.remove(f)
        except: pass

    print("[CAM2] Iniciando ffmpeg...")
    cmd = [
        'ffmpeg', '-y',
        '-rtsp_transport', 'tcp',
        '-i', CAM2_RTSP,
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-tune', 'zerolatency',
        '-f', 'hls',
        '-hls_time', '1',
        '-hls_list_size', '3',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_segment_type', 'mpegts',
        '-hls_segment_filename', f'{HLS_DIR}/seg%03d.ts',
        f'{HLS_DIR}/stream.m3u8'
    ]
    ffmpeg_proc   = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ffmpeg_active = True
    print("[CAM2] ffmpeg iniciado.")


def ffmpeg_stop():
    global ffmpeg_proc, ffmpeg_active
    if not ffmpeg_active:
        return
    print("[CAM2] Deteniendo ffmpeg (sin clientes).")
    try:
        ffmpeg_proc.terminate()
        ffmpeg_proc.wait(timeout=5)
    except:
        ffmpeg_proc.kill()
    ffmpeg_proc   = None
    ffmpeg_active = False


def ffmpeg_watchdog():
    """Detiene ffmpeg si no hay clientes por HLS_TIMEOUT segundos."""
    while True:
        time.sleep(5)
        with ffmpeg_lock:
            if ffmpeg_active and (time.time() - last_ping) > HLS_TIMEOUT:
                ffmpeg_stop()
            # Reiniciar si ffmpeg murio inesperadamente
            elif ffmpeg_active and ffmpeg_proc and ffmpeg_proc.poll() is not None:
                print("[CAM2] ffmpeg termino inesperadamente. Reiniciando...")
                ffmpeg_active_prev = ffmpeg_active
                ffmpeg_proc_prev   = ffmpeg_proc
                ffmpeg_active = False
                ffmpeg_proc   = None
                if ffmpeg_active_prev:
                    ffmpeg_start()

threading.Thread(target=ffmpeg_watchdog, daemon=True).start()


# ==============================================================================
# Flask
# ==============================================================================

HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Camara 2 — CISA</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background: #0d1117; color: #fff;
      font-family: sans-serif;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      min-height: 100vh; gap: 12px;
    }
    h1 { font-size: 1.1rem; color: #58a6ff; }
    video {
      width: 100%; max-width: 860px;
      border-radius: 8px; border: 2px solid #21262d;
      background: #000;
    }
    #status { font-size: 0.82rem; color: #8b949e; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
</head>
<body>
  <h1>📷 Camara 2 &mdash; 192.168.10.4</h1>
  <video id="video" autoplay muted playsinline controls></video>
  <p id="status">Iniciando stream...</p>

  <script>
    const video  = document.getElementById('video');
    const status = document.getElementById('status');
    const src    = '/hls/stream.m3u8';

    // Mantener vivo el stream mientras la pagina esta abierta
    setInterval(() => fetch('/ping').catch(() => {}), 5000);

    fetch('/start').then(() => {
      // Dar tiempo a ffmpeg para generar el primer segmento
      setTimeout(iniciarHLS, 3000);
    });

    function iniciarHLS() {
      if (Hls.isSupported()) {
        const hls = new Hls({
          lowLatencyMode: true,
          maxBufferLength: 4,
          liveSyncDurationCount: 2
        });
        hls.loadSource(src);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          video.play();
          status.textContent = 'Transmitiendo — Camara 2 (H.265 → H.264)';
        });
        hls.on(Hls.Events.ERROR, (e, d) => {
          if (d.fatal) {
            status.textContent = 'Reconectando...';
            setTimeout(iniciarHLS, 3000);
          }
        });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        video.play();
        status.textContent = 'Transmitiendo (HLS nativo)';
      } else {
        status.textContent = 'Error: navegador no soporta HLS';
      }
    }
  </script>
</body>
</html>"""


@app.route('/')
def index():
    return HTML


@app.route('/start')
def start():
    global last_ping
    with ffmpeg_lock:
        last_ping = time.time()
        ffmpeg_start()
    return jsonify({"ok": True})


@app.route('/ping')
def ping():
    global last_ping
    with ffmpeg_lock:
        last_ping = time.time()
        if not ffmpeg_active:
            ffmpeg_start()
    return jsonify({"ok": True})


@app.route('/hls/<path:filename>')
def hls_files(filename):
    filepath = os.path.join(HLS_DIR, filename)
    if not os.path.exists(filepath):
        return '', 404
    mt = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    resp = make_response(send_file(filepath, mimetype=mt))
    if filename.endswith('.m3u8'):
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma']        = 'no-cache'
        resp.headers['Expires']       = '0'
    return resp


@app.route('/health')
def health():
    m3u8 = os.path.exists(os.path.join(HLS_DIR, 'stream.m3u8'))
    return jsonify({
        "camara":  "192.168.10.4",
        "ffmpeg":  ffmpeg_active,
        "stream":  m3u8,
    })


# ==============================================================================
# Main
# ==============================================================================
if __name__ == '__main__':
    print(f"[INFO] Visor Camara 2 en http://0.0.0.0:{FLASK_PORT}")
    print(f"[INFO] Camara: {CAM2_RTSP.replace('PatioCCA_', '****')}")
    print(f"[INFO] HLS dir: {HLS_DIR}")
    print(f"[INFO] ffmpeg arranca cuando alguien abre el navegador")
    app.run(host='0.0.0.0', port=FLASK_PORT, threaded=True, use_reloader=False)