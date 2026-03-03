import cv2
import numpy as np
import tensorflow as tf
import ultralytics
import torch

print("--- JETSON SYSTEM CHECK ---")

# 1. NumPy/OpenCV Compatibility
print(f"OpenCV version: {cv2.__version__}")
print(f"NumPy version: {np.__version__}")

# 2. TensorFlow GPU Check
tf_gpus = tf.config.list_physical_devices('GPU')
print(f"TensorFlow GPU detected: {'YES' if tf_gpus else 'NO'}")

# 3. PyTorch/Ultralytics GPU Check (Crucial for YOLO)
torch_gpu = torch.cuda.is_available()
print(f"PyTorch/YOLO GPU (CUDA) detected: {'YES' if torch_gpu else 'NO'}")
if torch_gpu:
    print(f"  Device Name: {torch.cuda.get_device_name(0)}")

# 4. Final Verdict
if tf_gpus and torch_gpu:
    print("\nVERDICT: Environment is CLEAN and GPU-Ready! 🚀")
else:
    print("\nVERDICT: Setup connected, but GPU missing. Check JetPack drivers.")