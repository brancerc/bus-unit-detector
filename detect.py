import cv2
import easyocr
import csv
import os
from ultralytics import YOLO

class GeneradorVideoMetrobus:
    def __init__(self, model_path):
        # Módulo 1: Inicialización (TensorRT + OCR)
        self.detector = YOLO(model_path, task='detect')
        self.reader = easyocr.Reader(['en'], gpu=True)
        self.registro_global = set()
        
        # Crear carpeta para los videos resultantes
        self.output_folder = "videos_procesados"
        os.makedirs(self.output_folder, exist_ok=True)
        print(f"🚀 Sistema listo. Los videos se guardarán en: {self.output_folder}/")

    def preprocesar_bn(self, frame):
        """Aplica B&N y CLAHE para resaltar los bordes de los números."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8)) # Aumentamos contraste
        bn = clahe.apply(gray)
        return cv2.cvtColor(bn, cv2.COLOR_GRAY2BGR)

    def solo_numeros(self, texto):
        """Filtro estricto: descarta si hay letras o símbolos."""
        limpio = texto.strip()
        return limpio.isdigit() and len(limpio) > 0

    def procesar_video(self, ruta_video, writer_csv):
        nombre_archivo = os.path.basename(ruta_video)
        cap = cv2.VideoCapture(ruta_video)
        
        # Configuración del Video de Salida
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        output_path = os.path.join(self.output_folder, f"PROCESADO_{nombre_archivo}")
        
        # Usamos el codec mp4v para que se pueda ver en cualquier PC
        out_video = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))

        print(f"🎥 Procesando: {nombre_archivo}...")

        while cap.isOpened():
            success, frame = cap.read()
            if not success: break

            # 1. Pasar a B&N para facilitar la detección de bordes
            frame_bn = self.preprocesar_bn(frame)

            # 2. Inferencia con el modelo .engine
            results = self.detector.predict(frame_bn, conf=0.35, device=0, verbose=False)

            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    
                    # Recorte del área de los números
                    crop = frame_bn[y1:y2, x1:x2]
                    
                    if crop.size > 0:
                        ocr_res = self.reader.readtext(crop)
                        for (_, text, prob) in ocr_res:
                            # 3. Validar que sea un número puro
                            if self.solo_numeros(text):
                                # Dibujar en el video (Cuadro verde y Texto)
                                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 3)
                                cv2.putText(frame, f"UNIDAD: {text}", (x1, y1 - 15),
                                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
                                
                                # Registrar en el CSV si es nuevo
                                if text not in self.registro_global:
                                    writer_csv.writerow([nombre_archivo, text, f"{prob:.2f}"])
                                    self.registro_global.add(text)
                                    print(f"✅ Detectado: {text}")

            # Escribir el frame anotado en el archivo de video
            out_video.write(frame)

        cap.release()
        out_video.release()

    def run(self, carpeta_imgs):
        archivos = [f for f in os.listdir(carpeta_imgs) if f.lower().endswith(('.mp4', '.avi', '.mov'))]
        
        with open("registro_final.csv", mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Archivo", "Numero_Detectado", "Confianza"])

            for vid in archivos:
                self.procesar_video(os.path.join(carpeta_imgs, vid), writer)

        print("🏁 Proceso completado. Revisa la carpeta 'videos_procesados'.")

if __name__ == "__main__":
    # Brando, asegúrate de que 'best.engine' esté en la misma carpeta
    app = GeneradorVideoMetrobus("best.engine")
    app.run("imgs")