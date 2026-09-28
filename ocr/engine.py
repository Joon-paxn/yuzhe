"""
ocr/engine.py
-------------
OCR 引擎抽象与实现。

设计：
- OcrEngineBase: 引擎抽象基类，定义 recognize / is_available 接口
- RapidOcrEngine: 基于 RapidOCR 的实现，兼容新版 (rapidocr>=3.x) 与旧版
  (rapidocr_onnxruntime) 两种 API，自动适配返回格式
- NoOcrEngine: 降级实现，未安装 OCR 库时使用，is_available=False，不影响现有逻辑
- create_ocr_engine: 工厂函数，按可用性自动选择引擎

引擎只负责「把图像变成文字」，不解释文字含义 (由 parser 负责)。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np

from logger import get_logger
from .models import OcrResult, OcrTextLine


class OcrEngineBase(ABC):
    """OCR 引擎抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎名称"""

    @abstractmethod
    def is_available(self) -> bool:
        """引擎是否可用 (已安装且初始化成功)"""

    @abstractmethod
    def recognize(self, frame: np.ndarray) -> OcrResult:
        """对 BGR ndarray 执行识别，返回 OcrResult"""


class NoOcrEngine(OcrEngineBase):
    """降级引擎：未安装 OCR 库时使用，始终返回空结果"""

    def __init__(self, reason: str = "未安装 OCR 库") -> None:
        self._reason = reason
        self._log = get_logger()

    @property
    def name(self) -> str:
        return "无 (降级)"

    def is_available(self) -> bool:
        return False

    def recognize(self, frame: np.ndarray) -> OcrResult:
        return OcrResult(success=False, error=self._reason)


class RapidOcrEngine(OcrEngineBase):
    """
    基于 RapidOCR 的引擎实现。

    兼容两种包 / API：
      1. 新版 rapidocr (>=3.x): from rapidocr import RapidOCR
         engine(img) -> RapidOCROutput (含 .txts / .boxes / .scores)
      2. 旧版 rapidocr_onnxruntime: from rapidocr_onnxruntime import RapidOCR
         engine(img) -> (result, elapse)，result 为 [[box, text, score], ...] 或 (None, None)

    首次调用会下载模型 (由 RapidOCR 自行处理)。
    """

    def __init__(self) -> None:
        self._log = get_logger()
        self._engine = None
        self._api_mode: str = "new"        # "new" 或 "old"
        self._init_error: Optional[str] = None
        self._available: bool = False
        self._lazy_loaded: bool = False

    @property
    def name(self) -> str:
        return "RapidOCR"

    def is_available(self) -> bool:
        if not self._available and not self._lazy_loaded:
            self._lazy_init()
        return self._available

    def _lazy_init(self) -> None:
        """延迟初始化：首次调用时尝试导入并创建引擎实例"""
        self._lazy_loaded = True
        # 优先尝试新版 rapidocr 包
        try:
            from rapidocr import RapidOCR  # type: ignore
            self._engine = RapidOCR()
            self._api_mode = "new"
            self._available = True
            self._log.info("OCR 引擎已加载 (rapidocr 新版 API)")
            return
        except ImportError:
            pass
        except Exception as e:
            self._init_error = f"rapidocr 初始化失败: {e}"

        # 回退到旧版 rapidocr_onnxruntime 包
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            self._engine = RapidOCR()
            self._api_mode = "old"
            self._available = True
            self._log.info("OCR 引擎已加载 (rapidocr_onnxruntime 旧版 API)")
            return
        except ImportError:
            self._init_error = (
                "未安装 OCR 库。请运行: pip install rapidocr onnxruntime "
                "(或 pip install rapidocr-onnxruntime)"
            )
        except Exception as e:
            self._init_error = f"rapidocr_onnxruntime 初始化失败: {e}"

        self._available = False
        self._log.warn(f"OCR 引擎不可用: {self._init_error}")

    def recognize(self, frame: np.ndarray) -> OcrResult:
        if not self._available and not self._lazy_loaded:
            self._lazy_init()
        if not self._available or self._engine is None:
            return OcrResult(success=False, error=self._init_error or "OCR 引擎不可用")

        if frame is None or frame.size == 0:
            return OcrResult(success=False, error="输入图像为空")

        t0 = time.monotonic()
        try:
            lines: List[OcrTextLine] = []
            if self._api_mode == "new":
                lines = self._recognize_new(frame)
            else:
                lines = self._recognize_old(frame)
            full_text = " ".join(ln.text for ln in lines if ln.text)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return OcrResult(lines=lines, full_text=full_text, success=True, elapsed_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self._log.error(f"OCR 识别异常: {e}")
            return OcrResult(success=False, error=str(e), elapsed_ms=elapsed_ms)

    def _recognize_new(self, frame: np.ndarray) -> List[OcrTextLine]:
        """新版 API: engine(img) -> RapidOCROutput (.txts / .boxes / .scores)

        注意：boxes 可能是 numpy ndarray，不能用 `or` 做空值判断
        (会触发 "truth value of an array is ambiguous")，须用 is None / len。
        """
        output = self._engine(frame)
        txts = getattr(output, "txts", None)
        if txts is None:
            return []
        boxes = getattr(output, "boxes", None)
        scores = getattr(output, "scores", None)
        lines: List[OcrTextLine] = []
        for i, txt in enumerate(txts):
            text = str(txt).strip()
            if not text:
                continue
            conf = 0.0
            if scores is not None and i < len(scores):
                conf = float(scores[i])
            bbox = (0, 0, 0, 0)
            if boxes is not None and i < len(boxes):
                bbox = self._box_to_bbox(boxes[i])
            lines.append(OcrTextLine(text=text, confidence=conf, bbox=bbox))
        return lines

    def _recognize_old(self, frame: np.ndarray) -> List[OcrTextLine]:
        """旧版 API: engine(img) -> (result, elapse)；result=[[box, text, score], ...]"""
        result, _elapse = self._engine(frame)
        if not result:
            return []
        lines: List[OcrTextLine] = []
        for item in result:
            # item: [box_pts, text, score]
            if not item or len(item) < 3:
                continue
            box_pts, text, score = item[0], item[1], item[2]
            text = str(text).strip()
            if not text:
                continue
            conf = float(score) if score is not None else 0.0
            bbox = self._box_to_bbox(box_pts)
            lines.append(OcrTextLine(text=text, confidence=conf, bbox=bbox))
        return lines

    @staticmethod
    def _box_to_bbox(box) -> tuple:
        """将 RapidOCR 的多边形 box (4 点) 转为 (x, y, w, h)"""
        try:
            pts = np.array(box, dtype=np.float32).reshape(-1, 2)
            x_min, y_min = pts[:, 0].min(), pts[:, 1].min()
            x_max, y_max = pts[:, 0].max(), pts[:, 1].max()
            return (int(x_min), int(y_min), int(x_max - x_min), int(y_max - y_min))
        except Exception:
            return (0, 0, 0, 0)


def create_ocr_engine() -> OcrEngineBase:
    """
    工厂函数：按可用性自动选择 OCR 引擎。
    未安装 RapidOCR 时返回 NoOcrEngine (降级)，保证主程序不报错。
    """
    engine = RapidOcrEngine()
    if engine.is_available():
        return engine
    reason = "未安装 OCR 库 (rapidocr / rapidocr-onnxruntime)"
    return NoOcrEngine(reason=reason)
