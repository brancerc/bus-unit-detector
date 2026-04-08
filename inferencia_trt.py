"""
inferencia_trt.py — Inferencia TensorRT 10.x para OCR de números de bus
========================================================================
Compatible con TensorRT 10.3 (Jetson Orin, JetPack 6.x).
La API de TRT 10 reemplazó get_binding_shape → get_tensor_shape,
binding_is_input → get_tensor_mode, etc.

Reemplaza leer_numero() en pipe_stream_v2.py.
Interface idéntica: recibe crop (numpy BGR), devuelve string o None.
"""

import cv2
import numpy as np

IMG_W, IMG_H = 128, 48

# ── Intento de carga TensorRT ─────────────────────────────────────────────────
try:
    import tensorrt as trt
    import pycuda.driver  as cuda
    import pycuda.autoinit
    _TRT_AVAILABLE = True
    _TRT_VERSION   = tuple(int(x) for x in trt.__version__.split('.')[:2])
except ImportError:
    _TRT_AVAILABLE = False
    _TRT_VERSION   = (0, 0)
    print("[OCR-TRT] TensorRT/pycuda no disponibles — usando fallback ONNX")


class OcrEngine:
    """
    Motor de inferencia OCR con soporte TRT 8.x y TRT 10.x.
    Detecta la versión automáticamente y usa la API correcta.
    Cae a ONNX Runtime si TensorRT no está disponible (desarrollo en PC).
    """

    def __init__(self, engine_path: str):
        self._mode = None
        if _TRT_AVAILABLE and engine_path.endswith('.engine'):
            self._load_trt(engine_path)
        else:
            onnx_path = engine_path.replace('.engine', '.onnx')
            self._load_onnx(onnx_path)

    # ── Carga TensorRT ────────────────────────────────────────────────────────

    def _load_trt(self, path: str):
        logger = trt.Logger(trt.Logger.WARNING)
        with open(path, 'rb') as f, trt.Runtime(logger) as rt:
            self._engine  = rt.deserialize_cuda_engine(f.read())
        self._context = self._engine.create_execution_context()
        self._stream  = cuda.Stream()

        # TRT 10.x usa get_tensor_name / get_tensor_shape / get_tensor_mode
        # TRT 8.x usa num_bindings / get_binding_shape / binding_is_input
        if _TRT_VERSION >= (10, 0):
            self._load_buffers_trt10()
        else:
            self._load_buffers_trt8()

        self._mode = 'trt'
        print(f"[OCR-TRT] Engine cargado (TRT {trt.__version__}): {path}")

    def _load_buffers_trt10(self):
        """API TensorRT 10.x — usa nombres de tensores."""
        self._input_name  = None
        self._output_name = None
        self._inputs_trt  = []
        self._outputs_trt = []
        self._bindings    = []

        n = self._engine.num_io_tensors
        for i in range(n):
            name  = self._engine.get_tensor_name(i)
            shape = self._engine.get_tensor_shape(name)
            dtype = trt.nptype(self._engine.get_tensor_dtype(name))
            mode  = self._engine.get_tensor_mode(name)
            size  = 1
            for s in shape:
                size *= abs(s)   # abs por si hay dims dinámicas (-1)

            host_mem   = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self._bindings.append(int(device_mem))

            if mode == trt.TensorIOMode.INPUT:
                self._input_name = name
                self._inputs_trt.append({'host': host_mem, 'device': device_mem,
                                         'name': name, 'shape': shape})
                self._context.set_input_shape(name, shape)
            else:
                self._output_name = name
                self._outputs_trt.append({'host': host_mem, 'device': device_mem,
                                          'name': name, 'shape': shape})

    def _load_buffers_trt8(self):
        """API TensorRT 8.x — usa índices de binding."""
        self._inputs_trt  = []
        self._outputs_trt = []
        self._bindings    = []

        for i in range(self._engine.num_bindings):
            shape = self._engine.get_binding_shape(i)
            dtype = trt.nptype(self._engine.get_binding_dtype(i))
            size  = trt.volume(shape)

            host_mem   = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self._bindings.append(int(device_mem))

            entry = {'host': host_mem, 'device': device_mem}
            if self._engine.binding_is_input(i):
                self._inputs_trt.append(entry)
            else:
                self._outputs_trt.append(entry)

    # ── Fallback ONNX Runtime ─────────────────────────────────────────────────

    def _load_onnx(self, path: str):
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(path,
                providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
            self._mode = 'onnx'
            print(f"[OCR-ONNX] Modelo cargado: {path}")
        except Exception as e:
            raise RuntimeError(f"No se pudo cargar modelo OCR: {e}")

    # ── Preprocesamiento ──────────────────────────────────────────────────────

    def _preprocess(self, crop_bgr: np.ndarray) -> np.ndarray | None:
        h, w = crop_bgr.shape[:2]
        if h < 5 or w < 5:
            return None
        gray    = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (IMG_W, IMG_H), interpolation=cv2.INTER_CUBIC)
        norm    = resized.astype(np.float32) / 255.0
        return norm[np.newaxis, np.newaxis, :, :]   # (1, 1, H, W)

    # ── Inferencia TRT 10.x ───────────────────────────────────────────────────

    def _infer_trt10(self, x: np.ndarray) -> np.ndarray:
        inp = self._inputs_trt[0]
        out = self._outputs_trt[0]

        np.copyto(inp['host'], x.ravel())
        cuda.memcpy_htod_async(inp['device'], inp['host'], self._stream)

        # TRT 10: execute_async_v3 con nombres de tensores
        self._context.execute_async_v3(self._stream.handle)

        cuda.memcpy_dtoh_async(out['host'], out['device'], self._stream)
        self._stream.synchronize()
        return out['host'].reshape(4, 10)

    # ── Inferencia TRT 8.x ───────────────────────────────────────────────────

    def _infer_trt8(self, x: np.ndarray) -> np.ndarray:
        inp = self._inputs_trt[0]
        out = self._outputs_trt[0]

        np.copyto(inp['host'], x.ravel())
        cuda.memcpy_htod_async(inp['device'], inp['host'], self._stream)
        self._context.execute_async_v2(self._bindings, self._stream.handle, None)
        cuda.memcpy_dtoh_async(out['host'], out['device'], self._stream)
        self._stream.synchronize()
        return out['host'].reshape(4, 10)

    # ── Inferencia ONNX ───────────────────────────────────────────────────────

    def _infer_onnx(self, x: np.ndarray) -> np.ndarray:
        out = self._session.run(None, {'input': x})[0]
        return out[0]   # (4, 10)

    # ── API pública ───────────────────────────────────────────────────────────

    def leer(self, crop_bgr: np.ndarray) -> str | None:
        """
        Recibe crop BGR (numpy), devuelve string de 3-4 dígitos o None.
        Números que empiezan con '0' → strip (0538 → '538').
        Devuelve None si la confianza de algún dígito < umbral.
        """
        x = self._preprocess(crop_bgr)
        if x is None:
            return None

        if self._mode == 'trt':
            if _TRT_VERSION >= (10, 0):
                logits = self._infer_trt10(x)
            else:
                logits = self._infer_trt8(x)
        else:
            logits = self._infer_onnx(x)

        def softmax(z):
            e = np.exp(z - z.max())
            return e / e.sum()

        digits = []
        for i in range(4):
            probs = softmax(logits[i])
            pred  = int(np.argmax(probs))
            conf  = float(probs[pred])
            if conf < 0.40:
                return None
            digits.append(pred)

        num_str = ''.join(map(str, digits)).lstrip('0') or '0'

        if len(num_str) < 3 or len(num_str) > 4:
            return None

        return num_str


if __name__ == '__main__':
    print(f"TensorRT disponible: {_TRT_AVAILABLE}")
    if _TRT_AVAILABLE:
        import tensorrt as trt
        print(f"Versión: {trt.__version__}")
        print(f"API detectada: {'TRT 10.x' if _TRT_VERSION >= (10,0) else 'TRT 8.x'}")
