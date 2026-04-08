#!/bin/bash
# Graba Cam 2 en segmentos de 1 hora para fácil manejo
# Guarda en JETSON_SD con timestamp

OUTDIR="/media/cisa/JETSON_SD/cam2_grabacion"
RTSP="rtsp://admin:PatioCCA_@192.168.10.4:554/cam/realmonitor?channel=1&subtype=1"
DURACION=3600   # 1 hora por segmento
DIAS=2          # días a grabar
TOTAL_SEGS=$((DIAS * 24))

mkdir -p "$OUTDIR"
echo "[CAM2-REC] Iniciando grabación — $TOTAL_SEGS segmentos de 1h"
echo "[CAM2-REC] Destino: $OUTDIR"
echo "[CAM2-REC] Espacio disponible: $(df -h /media/cisa/JETSON_SD | tail -1 | awk '{print $4}')"

for i in $(seq 1 $TOTAL_SEGS); do
    TS=$(date '+%Y%m%d_%H%M%S')
    OUTFILE="$OUTDIR/cam2_${TS}.mp4"
    echo "[CAM2-REC] Segmento $i/$TOTAL_SEGS → $OUTFILE"

    # ffmpeg directo desde RTSP H.265 → recodifica a H.264 para compatibilidad
    # -c:v copy si quieres mantener H.265 (menor CPU pero menos compatible)
    ffmpeg -y \
        -rtsp_transport tcp \
        -i "$RTSP" \
        -t $DURACION \
        -c:v copy \
        -an \
        "$OUTFILE" \
        2>/dev/null

    # Verificar que el archivo se creó y tiene tamaño razonable
    if [ -f "$OUTFILE" ]; then
        SIZE=$(du -h "$OUTFILE" | cut -f1)
        echo "[CAM2-REC] ✓ $OUTFILE ($SIZE)"
    else
        echo "[CAM2-REC] ✗ Error en segmento $i — reintentando en 5s"
        sleep 5
    fi

    # Espacio restante cada 6 segmentos (cada 6h)
    if [ $((i % 6)) -eq 0 ]; then
        AVAIL=$(df -h /media/cisa/JETSON_SD | tail -1 | awk '{print $4}')
        echo "[CAM2-REC] Espacio disponible: $AVAIL"
    fi
done

echo "[CAM2-REC] Grabación completada. Archivos en $OUTDIR"
ls -lh "$OUTDIR"
