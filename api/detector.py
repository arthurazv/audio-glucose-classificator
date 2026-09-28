"""
api/detector.py

Detector do aparelho glicosímetro (YOLO / OpenCV fallback).
Localiza a bounding box do aparelho para isolar fundos, mãos e mesas.
"""

import os
from typing import Tuple

try:
    import cv2
    OPENCV_DISPONIVEL = True
except ImportError:
    cv2 = None
    OPENCV_DISPONIVEL = False

try:
    import numpy as np
    NUMPY_DISPONIVEL = True
except ImportError:
    np = None
    NUMPY_DISPONIVEL = False

try:
    from ultralytics import YOLO
    YOLO_DISPONIVEL = True
except ImportError:
    YOLO_DISPONIVEL = False

YOLO_DEFAULT_MODEL = os.path.join(os.path.dirname(__file__), "..", "models", "yolo11s.pt")
YOLO_NANO_MODEL = os.path.join(os.path.dirname(__file__), "..", "models", "yolo11n.pt")

class GlucometerDetector:
    def __init__(self, model_path: str = YOLO_DEFAULT_MODEL):
        self.model = None
        if YOLO_DISPONIVEL:
            try:
                # Tenta carregar o modelo YOLO11s local ou faz download automático
                self.model = YOLO(model_path)
            except Exception:
                try:
                    self.model = YOLO(YOLO_NANO_MODEL)
                except Exception:
                    self.model = None

    def detect(self, imagem_input) -> Tuple[int, int, int, int, float]:
        """
        Detecta a bounding box do aparelho glicosímetro na imagem.
        Retorna: (x1, y1, x2, y2, confidence)
        """
        if imagem_input is None:
            return 0, 0, 0, 0, 0.0

        if hasattr(imagem_input, 'size') and not hasattr(imagem_input, 'shape'):
            w, h = imagem_input.size
            if self.model is not None:
                try:
                    results = self.model(imagem_input, verbose=False)
                    for r in results:
                        if len(r.boxes) > 0:
                            best_box = max(r.boxes, key=lambda b: float(b.conf[0]))
                            coords = best_box.xyxy[0].cpu().numpy()
                            return int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3]), float(best_box.conf[0])
                except Exception:
                    pass
            return int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9), 0.50

        if hasattr(imagem_input, 'size') and imagem_input.size == 0:
            return 0, 0, 0, 0, 0.0

        h, w = imagem_input.shape[:2]

        if self.model is not None:
            try:
                results = self.model(imagem_input, verbose=False)
                for r in results:
                    boxes = r.boxes
                    if len(boxes) > 0:
                        best_box = max(boxes, key=lambda b: float(b.conf[0]))
                        coords = best_box.xyxy[0].cpu().numpy()
                        conf = float(best_box.conf[0])
                        return int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3]), conf
            except Exception:
                pass

        if OPENCV_DISPONIVEL and NUMPY_DISPONIVEL:
            try:
                cinza = cv2.cvtColor(imagem_input, cv2.COLOR_BGR2GRAY) if len(imagem_input.shape) == 3 else imagem_input
                blurred = cv2.GaussianBlur(cinza, (9, 9), 0)
                edges = cv2.Canny(blurred, 30, 150)
                kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
                closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
                contornos, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contornos:
                    area_min = (h * w) * 0.05
                    candidatos = [c for c in contornos if cv2.contourArea(c) >= area_min]
                    if candidatos:
                        maior = max(candidatos, key=cv2.contourArea)
                        x, y, bw, bh = cv2.boundingRect(maior)
                        return x, y, x + bw, y + bh, 0.75
            except Exception:
                pass

        margin_x, margin_y = int(w * 0.1), int(h * 0.1)
        return margin_x, margin_y, w - margin_x, h - margin_y, 0.50
