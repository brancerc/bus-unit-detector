📊 Bus Unit Detector

Real-time edge AI system for vehicle identification on NVIDIA Jetson Orin Nano.

🎯 What it does:
- Detects bus unit numbers from IP camera streams in <50ms
- YOLO + Tesseract OCR running 100% on-device (no cloud)
- 24+ FPS processing, PostgreSQL validation, Telegram alerts
- Production deployment with auto-recovery

🔧 Tech stack:
- PyTorch/TensorRT for inference optimization
- GStreamer for hardware video decode (NVDEC)
- OpenCV multi-strategy OCR preprocessing
- Flask REST APIs + HLS streaming
- systemd for reliable service management

This project showcases full-stack edge AI development: from model training 
(Edge Impulse) to GPU optimization to production deployment.

#EdgeAI #ComputerVision #JetsonNano #PyTorch #OpenCV #Python

github.com/brancerc/bus-unit-detector
