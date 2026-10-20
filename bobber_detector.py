"""Optional motion quality signal. This module cannot confirm a bite."""
import cv2
import numpy as np
from capture import WindowCapture


class BobberMotionDetector:
    def __init__(self, window, config):
        self.config = config
        self.capture = WindowCapture(window)
        self.previous = None
        self.position = None
        self.displacement = (0.0, 0.0)
        self.confidence = 0.0
        self._continuous = 0

    def detect(self):
        if not self.config.enabled:
            return 0.0
        frame, _ = self.capture.capture(self.config.roi)
        if frame is None: return 0.0
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        if self.previous is None or self.previous.shape != gray.shape:
            self.previous = gray
            return 0.0
        mask = cv2.absdiff(gray, self.previous) > 25
        self.previous = gray
        fraction = float(mask.mean())
        self._continuous = self._continuous + 1 if fraction >= self.config.motion_threshold else 0
        ys, xs = np.nonzero(mask)
        if xs.size:
            centroid = (float(xs.mean()), float(ys.mean()))
            if self.position:
                self.displacement = (centroid[0]-self.position[0], centroid[1]-self.position[1])
            self.position = centroid
        # Whole-ROI changes (camera movement) are excluded from useful bobber evidence.
        self.confidence = min(1.0, fraction / max(.001, self.config.motion_threshold)) * min(1, self._continuous / 3)
        if fraction > .60: self.confidence = 0.0
        return self.confidence

    def close(self):
        self.capture.close()

    def release_resources(self):
        self.previous = None
        self.capture.close()
